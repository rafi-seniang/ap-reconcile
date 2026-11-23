"""Thin FastAPI controller for the AP Review Queue UI, PDF page previews, and Excel report downloads."""

import tempfile
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.controllers.guards import (
    InMemoryRateLimiter,
    resolve_client_ip,
    validate_uploaded_reconciliation_files,
)
from app.llm.client import UpstreamUnavailable
from app.schemas.erp import ErpValidationError
from app.schemas.reconciliation import ReconciliationReport
from app.services.reconciliation_service import ReconciliationService

_MAX_STORED_RUNS = 10


@dataclass
class StoredReconciliationRun:
    """In-memory snapshot of a completed batch for page previews and Excel download."""

    report: ReconciliationReport
    page_pngs: dict[int, bytes]
    excel_bytes: bytes


class InMemoryRunStore:
    """Bounded FIFO store keeping the most recent reconciliation runs in memory."""

    def __init__(self, max_runs: int = _MAX_STORED_RUNS) -> None:
        self._max_runs = max_runs
        self._runs: OrderedDict[str, StoredReconciliationRun] = OrderedDict()

    def save(self, run: StoredReconciliationRun) -> str:
        run_id = uuid.uuid4().hex[:12]
        self._runs[run_id] = run
        while len(self._runs) > self._max_runs:
            self._runs.popitem(last=False)
        return run_id

    def get_or_404(self, run_id: str) -> StoredReconciliationRun:
        stored = self._runs.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
        return stored


def create_reconciliation_router(
    frontend_dir: Path,
    rate_limiter: InMemoryRateLimiter,
    *,
    max_pdf_mb: int = 25,
    max_erp_mb: int = 10,
) -> APIRouter:
    """Build the FastAPI router delegating business work to ReconciliationService and request guards."""
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def render_workbench_index() -> HTMLResponse:
        return HTMLResponse((frontend_dir / "index.html").read_text(encoding="utf-8"))

    @router.get("/health")
    async def get_engine_health(request: Request) -> dict[str, Any]:
        service: ReconciliationService = request.app.state.reconciliation_service
        return {
            "status": "ok",
            "provider": service.provider_name,
            "model": service.primary_model_name,
            "retry_model": service.retry_model_name,
        }

    @router.post("/api/reconcile", response_class=JSONResponse)
    async def reconcile_uploaded_batch(
        request: Request,
        pdf_file: Annotated[UploadFile, File(...)],
        erp_file: Annotated[UploadFile, File(...)],
    ) -> JSONResponse:
        rate_limiter.enforce(resolve_client_ip(request))
        pdf_name, pdf_bytes, erp_name, erp_bytes = await validate_uploaded_reconciliation_files(
            pdf_file,
            erp_file,
            max_pdf_mb=max_pdf_mb,
            max_erp_mb=max_erp_mb,
        )
        service: ReconciliationService = request.app.state.reconciliation_service

        with tempfile.TemporaryDirectory(prefix="ap_reconcile_") as temp_dir:
            temp_path = Path(temp_dir)
            pdf_temp_path = temp_path / "batch.pdf"
            erp_temp_path = temp_path / "erp.xlsx"
            pdf_temp_path.write_bytes(pdf_bytes)
            erp_temp_path.write_bytes(erp_bytes)

            try:
                result = await service.reconcile(
                    pdf_temp_path,
                    erp_temp_path,
                    pdf_display_name=pdf_name,
                    erp_display_name=erp_name,
                )
            except ErpValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": "erp_validation_failed",
                        "detail": str(exc),
                        "issues": [issue.model_dump() for issue in exc.issues],
                    },
                )
            except UpstreamUnavailable as exc:
                return JSONResponse(
                    status_code=503,
                    content={"error": "upstream_unavailable", "detail": str(exc)},
                )
            except ValueError as exc:
                return JSONResponse(
                    status_code=422,
                    content={"error": "invalid_input", "detail": str(exc)},
                )

        excel_bytes = service.render_excel_bytes(result.report)
        run_store: InMemoryRunStore = request.app.state.run_store
        run_id = run_store.save(
            StoredReconciliationRun(
                report=result.report,
                page_pngs=result.page_pngs,
                excel_bytes=excel_bytes,
            )
        )
        return JSONResponse(
            status_code=200,
            content={"run_id": run_id, "report": result.report.model_dump(mode="json")},
        )

    @router.get("/api/runs/{run_id}", response_class=JSONResponse)
    async def get_reconciliation_run(request: Request, run_id: str) -> JSONResponse:
        stored = request.app.state.run_store.get_or_404(run_id)
        return JSONResponse(content={"run_id": run_id, "report": stored.report.model_dump(mode="json")})

    @router.get("/api/runs/{run_id}/pages/{page_number}")
    async def get_reconciliation_page_image(request: Request, run_id: str, page_number: int) -> Response:
        stored = request.app.state.run_store.get_or_404(run_id)
        png_bytes = stored.page_pngs.get(page_number)
        if png_bytes is None:
            raise HTTPException(status_code=404, detail=f"Page {page_number} not found for run '{run_id}'.")
        return Response(content=png_bytes, media_type="image/png")

    @router.get("/api/runs/{run_id}/download/xlsx")
    async def download_reconciliation_excel(request: Request, run_id: str) -> Response:
        stored = request.app.state.run_store.get_or_404(run_id)
        return Response(
            content=stored.excel_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="reconciliation_{run_id}.xlsx"'},
        )

    return router
