"""Top-level reconciliation service orchestrating ERP validation, PDF extraction, engine tie-out, and reporting."""

from dataclasses import dataclass, field
from pathlib import Path

from app.engine.reconcile_engine import reconcile_batch
from app.schemas.reconciliation import ReconciliationReport
from app.services.erp_service import ErpService
from app.services.extraction_service import ExtractionService
from app.services.report_service import ReportService
from config.settings import Settings


@dataclass
class ReconciliationRunResult:
    """Output bundle returned by ReconciliationService for CLI, HTTP API, and evaluation scripts."""

    report: ReconciliationReport
    page_pngs: dict[int, bytes]
    output_files: dict[str, Path] = field(default_factory=dict)


def _resolve_display_name(source: Path | str | bytes, display_name: str | None, default_name: str) -> str:
    if display_name:
        return display_name
    if isinstance(source, bytes):
        return default_name
    return Path(source).name


class ReconciliationService:
    """Coordinates fail-fast ERP validation, multimodal PDF extraction, deterministic tie-out, and Excel export."""

    def __init__(
        self,
        settings: Settings,
        erp_service: ErpService,
        extraction_service: ExtractionService,
        report_service: ReportService,
    ) -> None:
        self._settings = settings
        self._erp_service = erp_service
        self._extraction_service = extraction_service
        self._report_service = report_service

    @property
    def provider_name(self) -> str:
        return self._extraction_service.provider_name

    @property
    def primary_model_name(self) -> str:
        return self._extraction_service.primary_model_name

    @property
    def retry_model_name(self) -> str | None:
        return self._extraction_service.retry_model_name

    @property
    def fallback_model_name(self) -> str | None:
        return self._extraction_service.fallback_model_name

    def render_excel_bytes(self, report: ReconciliationReport) -> bytes:
        """Render the reconciliation report to Excel bytes for web download."""
        return self._report_service.render_excel_bytes(report)

    async def close(self) -> None:
        """Release underlying extraction resources."""
        await self._extraction_service.close()

    async def reconcile(
        self,
        pdf_source: Path | str | bytes,
        erp_source: Path | str | bytes,
        *,
        output_dir: Path | str | None = None,
        pdf_display_name: str | None = None,
        erp_display_name: str | None = None,
    ) -> ReconciliationRunResult:
        """Validate ERP first, extract all PDF pages, run deterministic reconciliation, and export outputs."""
        # Fail fast on broken ERP workbooks before spending any LLM tokens on the supplier PDF.
        erp_workbook = self._erp_service.load_and_validate_workbook(erp_source)
        extraction = await self._extraction_service.extract_supplier_pdf(pdf_source)

        report = reconcile_batch(
            extraction.documents,
            erp_workbook,
            pdf_filename=_resolve_display_name(pdf_source, pdf_display_name, "supplier_invoices.pdf"),
            erp_filename=_resolve_display_name(erp_source, erp_display_name, "erp_export.xlsx"),
            tolerance_idr=self._settings.tolerance_idr,
            tax_base_error_as_needs_review=self._settings.tax_base_error_as_needs_review,
            usage=extraction.usage,
        )
        output_files = (
            self._report_service.write_batch_outputs(report, output_dir) if output_dir is not None else {}
        )
        return ReconciliationRunResult(
            report=report,
            page_pngs=extraction.page_pngs,
            output_files=output_files,
        )
