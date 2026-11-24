"""PDF extraction service coordinating page rendering, concurrent LLM calls, stitching, and math-gate retries."""

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from app.engine.math_rules import failed_core_arithmetic_checks, verify_pdf_document_math
from app.llm.assembler import assemble_documents
from app.llm.client import PageExtractor, TokenCount, UpstreamUnavailable, estimate_extraction_cost_usd
from app.llm.parser import parse_assembled_document
from app.llm.pdf_splitter import PdfPagePayload, load_pdf_pages
from app.schemas.invoice import ExtractedPage, ExtractionUsage, ParsedDocument
from config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class BatchExtractionOutput:
    """Parsed supplier documents, rendered page PNGs for UI preview, and token/latency usage."""

    documents: list[ParsedDocument]
    page_pngs: dict[int, bytes]
    usage: ExtractionUsage


def _assemble_parse_and_verify_pages(pages: list[ExtractedPage]) -> list[ParsedDocument]:
    parsed_documents: list[ParsedDocument] = []
    for assembled in assemble_documents(pages):
        parsed = parse_assembled_document(assembled)
        parsed.checks.extend(verify_pdf_document_math(parsed))
        parsed_documents.append(parsed)
    return parsed_documents


def _tally_page_extraction_tokens(
    page_results: list[tuple[ExtractedPage, TokenCount]],
) -> tuple[list[ExtractedPage], int, int, int, int, int]:
    extracted_pages: list[ExtractedPage] = []
    api_calls = 0
    fallback_calls = 0
    cached_hits = 0
    input_tokens = 0
    output_tokens = 0
    for extracted_page, tokens in page_results:
        extracted_pages.append(extracted_page)
        if tokens.used_fallback:
            fallback_calls += 1
        if tokens.from_cache:
            cached_hits += 1
        else:
            api_calls += 1
            input_tokens += tokens.input_tokens
            output_tokens += tokens.output_tokens
    return extracted_pages, api_calls, fallback_calls, cached_hits, input_tokens, output_tokens


class ExtractionService:
    """Runs concurrent multimodal extraction over supplier PDF pages with a single math-gate retry."""

    def __init__(
        self,
        settings: Settings,
        primary_extractor: PageExtractor,
        retry_extractor: PageExtractor | None = None,
        *,
        fallback_model_name: str | None = None,
    ) -> None:
        self._settings = settings
        self._primary_extractor = primary_extractor
        self._retry_extractor = retry_extractor
        self._fallback_model_name = fallback_model_name

    @property
    def provider_name(self) -> str:
        return self._primary_extractor.provider_name

    @property
    def primary_model_name(self) -> str:
        return self._primary_extractor.model_name

    @property
    def retry_model_name(self) -> str | None:
        return self._retry_extractor.model_name if self._retry_extractor is not None else None

    @property
    def fallback_model_name(self) -> str | None:
        return self._fallback_model_name

    async def close(self) -> None:
        """Close underlying extractor clients."""
        await self._primary_extractor.close()
        if self._retry_extractor is not None and self._retry_extractor is not self._primary_extractor:
            await self._retry_extractor.close()

    async def _extract_primary_pages(
        self,
        page_payloads: list[PdfPagePayload],
        concurrency_gate: asyncio.Semaphore,
    ) -> tuple[list[ExtractedPage], int, int, int, int, int]:
        total_pages = len(page_payloads)

        async def extract_single_page(payload: PdfPagePayload) -> tuple[ExtractedPage, TokenCount]:
            async with concurrency_gate:
                printed_page, tokens = await self._primary_extractor.extract_page(payload, total_pages)
                return ExtractedPage(page_number=payload.page_number, printed=printed_page), tokens

        results = await asyncio.gather(*(extract_single_page(payload) for payload in page_payloads))
        return _tally_page_extraction_tokens(list(results))

    async def _retry_single_document(
        self,
        retry_extractor: PageExtractor,
        document: ParsedDocument,
        failure_reasons: list[str],
        payload_by_page_number: dict[int, PdfPagePayload],
        total_pages: int,
        concurrency_gate: asyncio.Semaphore,
    ) -> tuple[ParsedDocument, int, int, int, int, int]:
        async def retry_single_page(page_number: int) -> tuple[ExtractedPage, TokenCount]:
            async with concurrency_gate:
                printed_page, tokens = await retry_extractor.extract_page(
                    payload_by_page_number[page_number],
                    total_pages,
                    retry_feedback=failure_reasons,
                )
                return ExtractedPage(page_number=page_number, printed=printed_page), tokens

        try:
            retried_results = await asyncio.gather(*(retry_single_page(page_num) for page_num in document.pages))
        except UpstreamUnavailable as exc:
            logger.warning(
                "Math-gate retry failed for %s (pages %s), keeping primary extraction: %s",
                document.doc_number_raw or f"pages {document.pages}",
                document.pages,
                exc,
            )
            return document, 0, 0, 0, 0, 0

        retried_pages, retry_calls, retry_fallbacks, retry_cached, retry_in, retry_out = (
            _tally_page_extraction_tokens(list(retried_results))
        )
        candidates = _assemble_parse_and_verify_pages(retried_pages)
        if len(candidates) == 1:
            candidate = candidates[0]
            preserves_extracted_structure = (not document.lines or bool(candidate.lines)) and (
                not document.doc_number_norm or bool(candidate.doc_number_norm)
            )
            old_failure_count = len(failed_core_arithmetic_checks(document))
            new_failure_count = len(failed_core_arithmetic_checks(candidate))
            if preserves_extracted_structure and (
                new_failure_count < old_failure_count
                or (
                    new_failure_count == old_failure_count
                    and document.has_parse_errors
                    and not candidate.has_parse_errors
                )
            ):
                logger.info(
                    "Math-gate retry improved %s (pages %s): %s -> %s core check failures",
                    candidate.doc_number_raw or document.doc_number_raw,
                    document.pages,
                    old_failure_count,
                    new_failure_count,
                )
                return candidate, retry_calls, retry_fallbacks, retry_cached, retry_in, retry_out
        return document, retry_calls, retry_fallbacks, retry_cached, retry_in, retry_out

    async def _retry_documents_failing_math_gate(
        self,
        parsed_documents: list[ParsedDocument],
        payload_by_page_number: dict[int, PdfPagePayload],
        total_pages: int,
        concurrency_gate: asyncio.Semaphore,
    ) -> tuple[int, int, int, int, int]:
        if self._retry_extractor is None:
            return 0, 0, 0, 0, 0

        retry_jobs: list[tuple[int, ParsedDocument, list[str]]] = []
        for doc_index, document in enumerate(parsed_documents):
            core_failures = failed_core_arithmetic_checks(document)
            if core_failures:
                failure_reasons = [f"{check.name}: {check.detail}" for check in core_failures]
                logger.info(
                    "Triggering 1x math-gate retry for %s (pages %s): %s",
                    document.doc_number_raw or f"pages {document.pages}",
                    document.pages,
                    failure_reasons,
                )
                retry_jobs.append((doc_index, document, failure_reasons))

        if not retry_jobs:
            return 0, 0, 0, 0, 0

        retry_extractor = self._retry_extractor
        retry_outcomes = await asyncio.gather(
            *(
                self._retry_single_document(
                    retry_extractor,
                    document,
                    reasons,
                    payload_by_page_number,
                    total_pages,
                    concurrency_gate,
                )
                for _, document, reasons in retry_jobs
            )
        )
        total_retry_calls = 0
        total_retry_fallbacks = 0
        total_retry_cached = 0
        total_retry_in = 0
        total_retry_out = 0
        for (doc_index, _, _), (best_doc, calls, fallbacks, cached, in_tok, out_tok) in zip(
            retry_jobs, retry_outcomes, strict=True
        ):
            parsed_documents[doc_index] = best_doc
            total_retry_calls += calls
            total_retry_fallbacks += fallbacks
            total_retry_cached += cached
            total_retry_in += in_tok
            total_retry_out += out_tok
        return total_retry_calls, total_retry_fallbacks, total_retry_cached, total_retry_in, total_retry_out

    async def extract_supplier_pdf(self, pdf_source: Path | str | bytes) -> BatchExtractionOutput:
        """Render a supplier PDF, extract pages concurrently, assemble documents, and run 1x math-gate retry."""
        page_payloads = load_pdf_pages(pdf_source, render_dpi=self._settings.pdf_render_dpi)
        payload_by_page_number = {payload.page_number: payload for payload in page_payloads}
        concurrency_gate = asyncio.Semaphore(self._settings.llm_concurrency)
        started_at = time.perf_counter()

        (
            extracted_pages,
            primary_calls,
            primary_fallbacks,
            cached_pages,
            primary_in_tokens,
            primary_out_tokens,
        ) = await self._extract_primary_pages(page_payloads, concurrency_gate)
        parsed_documents = _assemble_parse_and_verify_pages(extracted_pages)
        retry_calls, retry_fallbacks, retry_cached, retry_in_tokens, retry_out_tokens = (
            await self._retry_documents_failing_math_gate(
                parsed_documents, payload_by_page_number, len(page_payloads), concurrency_gate
            )
        )

        latency_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
        usage = ExtractionUsage(
            provider=self.provider_name,
            model=self.primary_model_name,
            retry_model=self.retry_model_name,
            fallback_model=self.fallback_model_name,
            primary_calls=primary_calls,
            retry_calls=retry_calls,
            fallback_calls=primary_fallbacks + retry_fallbacks,
            cached_pages=cached_pages + retry_cached,
            input_tokens=primary_in_tokens,
            output_tokens=primary_out_tokens,
            retry_input_tokens=retry_in_tokens,
            retry_output_tokens=retry_out_tokens,
            estimated_cost_usd=estimate_extraction_cost_usd(
                self._settings,
                primary_input_tokens=primary_in_tokens,
                primary_output_tokens=primary_out_tokens,
                retry_input_tokens=retry_in_tokens,
                retry_output_tokens=retry_out_tokens,
            ),
            latency_ms=latency_ms,
        )
        page_pngs = {payload.page_number: payload.png_bytes for payload in page_payloads}
        return BatchExtractionOutput(documents=parsed_documents, page_pngs=page_pngs, usage=usage)
