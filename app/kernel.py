"""Application composition root wiring Settings, LiteLLM extractors, services, and the FastAPI app."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.controllers.guards import InMemoryRateLimiter
from app.controllers.reconciliation_controller import InMemoryRunStore, create_reconciliation_router
from app.llm.client import DiskCachedExtractor, LiteLLMPageExtractor, PageExtractor
from app.services.erp_service import ErpService
from app.services.extraction_service import ExtractionService
from app.services.reconciliation_service import ReconciliationService
from app.services.report_service import ReportService
from config.settings import Settings

_FRONTEND_DIR = Path(__file__).parent / "frontend"


def build_page_extractors(settings: Settings) -> tuple[PageExtractor, PageExtractor | None]:
    """Instantiate primary and optional targeted-retry LiteLLM extractors from Settings."""
    api_key = settings.resolve_api_key()

    def build_one_extractor(model_name: str, *, is_retry_role: bool) -> PageExtractor:
        extractor: PageExtractor = LiteLLMPageExtractor(
            model_name=model_name,
            api_key=api_key,
            api_base=settings.llm_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
            is_retry_role=is_retry_role,
        )
        if settings.extraction_cache_dir:
            extractor = DiskCachedExtractor(extractor, settings.extraction_cache_dir)
        return extractor

    primary = build_one_extractor(settings.llm_model, is_retry_role=False)
    retry = build_one_extractor(settings.llm_retry_model, is_retry_role=True) if settings.llm_retry_model else None
    return primary, retry


def build_reconciliation_service(
    settings: Settings | None = None,
    *,
    primary_extractor: PageExtractor | None = None,
    retry_extractor: PageExtractor | None = None,
) -> ReconciliationService:
    """Assemble the complete ReconciliationService with either live LiteLLM extractors or injected test fakes."""
    active_settings = settings or Settings()
    if primary_extractor is None:
        primary, retry = build_page_extractors(active_settings)
    else:
        primary, retry = primary_extractor, retry_extractor

    return ReconciliationService(
        settings=active_settings,
        erp_service=ErpService(),
        extraction_service=ExtractionService(active_settings, primary, retry),
        report_service=ReportService(),
    )


def create_web_application(
    settings: Settings | None = None,
    *,
    reconciliation_service: ReconciliationService | None = None,
) -> FastAPI:
    """Create the FastAPI web application with static frontend, request guards, and service lifecycle."""
    active_settings = settings or Settings()

    @asynccontextmanager
    async def application_lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_service = reconciliation_service is None
        active_service = (
            reconciliation_service
            if reconciliation_service is not None
            else build_reconciliation_service(active_settings)
        )
        app.state.reconciliation_service = active_service
        app.state.run_store = InMemoryRunStore()
        try:
            yield
        finally:
            if owns_service:
                await active_service.close()

    app = FastAPI(
        title="AP Transaction Reconciliation",
        description="Supplier invoice PDF to ERP AP reconciliation engine and review queue.",
        version="0.1.0",
        lifespan=application_lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")

    rate_limiter = InMemoryRateLimiter(
        max_requests=active_settings.rate_limit_requests_per_window,
        window_seconds=active_settings.rate_limit_window_seconds,
    )
    router = create_reconciliation_router(
        frontend_dir=_FRONTEND_DIR,
        rate_limiter=rate_limiter,
        max_pdf_mb=active_settings.max_pdf_upload_mb,
        max_erp_mb=active_settings.max_erp_upload_mb,
    )
    app.include_router(router)
    return app
