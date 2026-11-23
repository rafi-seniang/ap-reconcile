"""End-to-end reconciliation service coordinating validation, extraction, math-gate retry, and reporting."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.erp.excel_loader import load_erp_workbook
from app.extraction.assembler import assemble_documents
from app.extraction.llm_client import PageExtractor, TokenCount, build_extractors, estimate_cost_usd
from app.extraction.parser import parse_assembled_document
from app.extraction.schemas import ExtractedPage, ExtractionUsage, ParsedDocument
from app.extraction.splitter import PdfPagePayload, load_pdf_pages
from app.reconcile.engine import reconcile_batch
from app.reconcile.math_rules import failed_core_arithmetic_checks, verify_pdf_document_math
from app.reconcile.schemas import ReconciliationReport
from app.reporting.csv_writer import write_csv_and_json_reports
from app.reporting.excel_writer import write_excel_report
from config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationRunResult:
    """Output bundle returned by ReconciliationService for CLI, API, and evaluation scripts."""

    report: ReconciliationReport
    page_pngs: dict[int, bytes]
    output_files: dict[str, Path] = field(default_factory=dict)


def _parse_and_verify(pages: list[ExtractedPage]) -> list[ParsedDocument]:
    parsed_docs: list[ParsedDocument] = []
    for assembled in assemble_documents(pages):
        parsed = parse_assembled_document(assembled)
        parsed.checks.extend(verify_pdf_document_math(parsed))
        parsed_docs.append(parsed)
    return parsed_docs


def _tally_tokens(
    results: list[tuple[ExtractedPage, TokenCount]],
) -> tuple[list[ExtractedPage], int, int, int, int]:
    pages: list[ExtractedPage] = []
    calls = 0
    cached = 0
    in_tokens = 0
    out_tokens = 0
    for ext_page, tokens in results:
        pages.append(ext_page)
        if tokens.from_cache:
            cached += 1
        else:
            calls += 1
            in_tokens += tokens.input_tokens
            out_tokens += tokens.output_tokens
    return pages, calls, cached, in_tokens, out_tokens


def _write_outputs(report: ReconciliationReport, output_dir: Path | str | None) -> dict[str, Path]:
    if output_dir is None:
        return {}
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {"excel": write_excel_report(report, out_path / "reconciliation_report.xlsx")}
    files.update(write_csv_and_json_reports(report, out_path))
    return files


class ReconciliationService:
    """Orchestrates strict ERP loading, concurrent multimodal PDF extraction, 1x math-gate retry, and tie-out."""

    def __init__(
        self,
        settings: Settings,
        primary_extractor: PageExtractor,
        retry_extractor: PageExtractor | None = None,
    ) -> None:
        self._settings = settings
        self._primary = primary_extractor
        self._retry = retry_extractor

    @classmethod
    def from_settings(cls, settings: Settings) -> "ReconciliationService":
        primary, retry = build_extractors(settings)
        return cls(settings=settings, primary_extractor=primary, retry_extractor=retry)

    async def close(self) -> None:
        await self._primary.close()
        if self._retry is not None:
            await self._retry.close()

    async def _extract_primary_pages(
        self,
        page_payloads: list[PdfPagePayload],
        semaphore: asyncio.Semaphore,
    ) -> tuple[list[ExtractedPage], int, int, int, int]:
        total_pages = len(page_payloads)

        async def run_one(payload: PdfPagePayload) -> tuple[ExtractedPage, TokenCount]:
            async with semaphore:
                printed, tokens = await self._primary.extract_page(payload, total_pages)
                return ExtractedPage(page_number=payload.page_number, printed=printed), tokens

        results = await asyncio.gather(*(run_one(p) for p in page_payloads))
        return _tally_tokens(list(results))

    async def _retry_one_document(
        self,
        retry_extractor: PageExtractor,
        doc: ParsedDocument,
        failed_core: list[str],
        payload_by_num: dict[int, PdfPagePayload],
        total_pages: int,
        semaphore: asyncio.Semaphore,
    ) -> tuple[ParsedDocument, int, int, int, int]:
        async def call_page(page_num: int) -> tuple[ExtractedPage, TokenCount]:
            async with semaphore:
                printed, tok = await retry_extractor.extract_page(
                    payload_by_num[page_num],
                    total_pages,
                    retry_feedback=failed_core,
                )
                return ExtractedPage(page_number=page_num, printed=printed), tok

        retried = await asyncio.gather(*(call_page(p) for p in doc.pages))
        retried_pages, r_calls, r_cached, r_in, r_out = _tally_tokens(list(retried))
        candidates = _parse_and_verify(retried_pages)
        if len(candidates) == 1:
            candidate = candidates[0]
            old_failures = len(failed_core_arithmetic_checks(doc))
            new_failures = len(failed_core_arithmetic_checks(candidate))
            if new_failures < old_failures or (
                new_failures == old_failures and doc.has_parse_errors and not candidate.has_parse_errors
            ):
                logger.info(
                    "Math-gate retry improved %s (pages %s): %s -> %s core check failures",
                    candidate.doc_number_raw or doc.doc_number_raw,
                    doc.pages,
                    old_failures,
                    new_failures,
                )
                return candidate, r_calls, r_cached, r_in, r_out
        return doc, r_calls, r_cached, r_in, r_out

    async def _retry_failed_documents(
        self,
        parsed_docs: list[ParsedDocument],
        payload_by_num: dict[int, PdfPagePayload],
        total_pages: int,
        semaphore: asyncio.Semaphore,
    ) -> tuple[int, int, int, int]:
        if self._retry is None:
            return 0, 0, 0, 0

        retry_jobs: list[tuple[int, ParsedDocument, list[str]]] = []
        for idx, doc in enumerate(parsed_docs):
            core_failures = failed_core_arithmetic_checks(doc)
            if core_failures:
                reasons = [f"{c.name}: {c.detail}" for c in core_failures]
                logger.info(
                    "Triggering 1x math-gate retry for %s (pages %s): %s",
                    doc.doc_number_raw or f"pages {doc.pages}",
                    doc.pages,
                    reasons,
                )
                retry_jobs.append((idx, doc, reasons))

        if not retry_jobs:
            return 0, 0, 0, 0

        retry_extractor = self._retry
        outcomes = await asyncio.gather(
            *(
                self._retry_one_document(
                    retry_extractor, doc, reasons, payload_by_num, total_pages, semaphore
                )
                for _, doc, reasons in retry_jobs
            )
        )
        retry_calls = 0
        cached_hits = 0
        retry_in = 0
        retry_out = 0
        for (idx, _, _), (best_doc, r_calls, r_cached, r_in, r_out) in zip(retry_jobs, outcomes, strict=True):
            parsed_docs[idx] = best_doc
            retry_calls += r_calls
            cached_hits += r_cached
            retry_in += r_in
            retry_out += r_out
        return retry_calls, cached_hits, retry_in, retry_out

    async def reconcile(
        self,
        pdf_path: Path | str,
        erp_path: Path | str,
        *,
        output_dir: Path | str | None = None,
        pdf_display_name: str | None = None,
        erp_display_name: str | None = None,
    ) -> ReconciliationRunResult:
        """Validate ERP first, extract all PDF pages, retry failed math gates once, and reconcile."""
        # Validate the internal ERP contract before spending any LLM tokens on the PDF.
        erp_workbook = load_erp_workbook(erp_path)
        pdf_file = Path(pdf_path)
        erp_file = Path(erp_path)
        page_payloads = load_pdf_pages(pdf_file, render_dpi=self._settings.pdf_render_dpi)
        payload_by_num = {payload.page_number: payload for payload in page_payloads}

        semaphore = asyncio.Semaphore(self._settings.llm_concurrency)
        started_at = time.perf_counter()

        (
            extracted_pages,
            primary_calls,
            cached_pages,
            primary_in,
            primary_out,
        ) = await self._extract_primary_pages(page_payloads, semaphore)
        parsed_docs = _parse_and_verify(extracted_pages)
        retry_calls, retry_cached, retry_in, retry_out = await self._retry_failed_documents(
            parsed_docs, payload_by_num, len(page_payloads), semaphore
        )
        cached_pages += retry_cached

        latency_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
        usage = ExtractionUsage(
            provider=self._primary.provider_name,
            model=self._primary.model_name,
            retry_model=self._retry.model_name if self._retry is not None else None,
            primary_calls=primary_calls,
            retry_calls=retry_calls,
            cached_pages=cached_pages,
            input_tokens=primary_in,
            output_tokens=primary_out,
            retry_input_tokens=retry_in,
            retry_output_tokens=retry_out,
            estimated_cost_usd=estimate_cost_usd(
                self._settings,
                primary_input_tokens=primary_in,
                primary_output_tokens=primary_out,
                retry_input_tokens=retry_in,
                retry_output_tokens=retry_out,
            ),
            latency_ms=latency_ms,
        )

        report = reconcile_batch(
            parsed_docs,
            erp_workbook,
            pdf_filename=pdf_display_name or pdf_file.name,
            erp_filename=erp_display_name or erp_file.name,
            tolerance_idr=self._settings.tolerance_idr,
            tax_base_error_as_needs_review=self._settings.tax_base_error_as_needs_review,
            usage=usage,
        )
        output_files = _write_outputs(report, output_dir)
        page_pngs = {payload.page_number: payload.png_bytes for payload in page_payloads}
        return ReconciliationRunResult(report=report, page_pngs=page_pngs, output_files=output_files)
