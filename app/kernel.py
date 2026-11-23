"""Application composition root wiring Settings, LiteLLM extractors, services, and the FastAPI app."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.controllers.guards import InMemoryRateLimiter
from app.controllers.reconciliation_controller import InMemoryRunStore, create_reconciliation_router
from app.llm.client import (
    DiskCachedExtractor,
    FallbackPageExtractor,
    LiteLLMPageExtractor,
    PageExtractor,
)
from app.services.erp_service import ErpService
from app.services.extraction_service import ExtractionService
from app.services.reconciliation_service import ReconciliationService
from app.services.report_service import ReportService
from config.settings import Settings

logger = logging.getLogger(__name__)
_FRONTEND_DIR = Path(__file__).parent / "frontend"


def _wrap_disk_cache(extractor: PageExtractor, cache_dir: str | None) -> PageExtractor:
    return DiskCachedExtractor(extractor, cache_dir) if cache_dir else extractor


def build_page_extractors(settings: Settings) -> tuple[PageExtractor, PageExtractor | None, str | None]:
    """Instantiate primary, optional math-retry, and optional automatic-failover LiteLLM extractors."""
    primary_key = settings.resolve_primary_api_key()
    fallback_model = settings.llm_fallback_model.strip() if settings.llm_fallback_model else None
    fallback_key = settings.resolve_fallback_api_key()

    if fallback_model and not fallback_key:
        logger.info(
            "Fallback model '%s' configured without an API key; running primary model only.", fallback_model
        )
        fallback_model = None

    has_active_fallback = bool(fallback_model and fallback_key and fallback_model != settings.llm_model)
    primary_max_retries = 0 if has_active_fallback else 3

    def build_model_extractor(model_name: str) -> PageExtractor:
        primary_leaf = _wrap_disk_cache(
            LiteLLMPageExtractor(
                model_name=model_name,
                api_key=primary_key,
                api_base=settings.llm_base_url,
                timeout_seconds=settings.llm_timeout_seconds,
                max_output_tokens=settings.llm_max_output_tokens,
                max_retries=primary_max_retries,
            ),
            settings.extraction_cache_dir,
        )
        if not has_active_fallback or fallback_model is None or fallback_key is None:
            return primary_leaf

        fallback_leaf = _wrap_disk_cache(
            LiteLLMPageExtractor(
                model_name=fallback_model,
                api_key=fallback_key,
                api_base=settings.llm_fallback_base_url,
                timeout_seconds=settings.llm_timeout_seconds,
                max_output_tokens=settings.llm_max_output_tokens,
                max_retries=2,
            ),
            settings.extraction_cache_dir,
        )
        return FallbackPageExtractor(primary_leaf, fallback_leaf)

    primary = build_model_extractor(settings.llm_model)
    retry = build_model_extractor(settings.llm_retry_model) if settings.llm_retry_model else None
    active_fallback_name = fallback_model if has_active_fallback else None
    return primary, retry, active_fallback_name


def build_reconciliation_service(
    settings: Settings | None = None,
    *,
    primary_extractor: PageExtractor | None = None,
    retry_extractor: PageExtractor | None = None,
) -> ReconciliationService:
    """Assemble the complete ReconciliationService with either live LiteLLM extractors or injected test fakes."""
    active_settings = settings or Settings()
    fallback_model_name: str | None = None
    if primary_extractor is None:
        primary, retry, fallback_model_name = build_page_extractors(active_settings)
    else:
        primary, retry = primary_extractor, retry_extractor
        if isinstance(primary, FallbackPageExtractor):
            fallback_model_name = primary.fallback_model_name

    return ReconciliationService(
        settings=active_settings,
        erp_service=ErpService(),
        extraction_service=ExtractionService(
            active_settings, primary, retry, fallback_model_name=fallback_model_name
        ),
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
