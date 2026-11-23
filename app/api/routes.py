"""FastAPI routes for the AP Review Queue UI, PDF page previews, and Excel report downloads."""

import tempfile
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.erp.schemas import ErpValidationError
from app.extraction.llm_client import UpstreamUnavailable
from app.reconcile.schemas import ReconciliationReport
from app.reporting.excel_writer import write_excel_report
from app.service import ReconciliationService
from config.settings import Settings

_STATIC_DIR = Path(__file__).parent / "static"
_MAX_STORED_RUNS = 10


@dataclass
class _StoredRun:
    report: ReconciliationReport
    page_pngs: dict[int, bytes]
    excel_bytes: bytes


class _RunStore:
    """Bounded in-memory store keeping the most recent reconciliation runs for preview and download."""

    def __init__(self, max_runs: int = _MAX_STORED_RUNS) -> None:
        self._max_runs = max_runs
        self._runs: OrderedDict[str, _StoredRun] = OrderedDict()

    def put(self, run: _StoredRun) -> str:
        run_id = uuid.uuid4().hex[:12]
        self._runs[run_id] = run
        while len(self._runs) > self._max_runs:
            self._runs.popitem(last=False)
        return run_id

    def get(self, run_id: str) -> _StoredRun:
        stored = self._runs.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
        return stored


async def _validate_uploads(pdf_file: UploadFile, erp_file: UploadFile) -> tuple[str, bytes, str, bytes]:
    pdf_name = pdf_file.filename or "supplier_invoices.pdf"
    erp_name = erp_file.filename or "erp_export.xlsx"
    if not pdf_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Supplier document file must have a .pdf extension.")
    if not erp_name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=422, detail="ERP export file must have a .xlsx extension.")

    pdf_bytes = await pdf_file.read()
    erp_bytes = await erp_file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=422, detail="Uploaded PDF file is empty.")
    if not erp_bytes:
        raise HTTPException(status_code=422, detail="Uploaded ERP Excel file is empty.")
    return pdf_name, pdf_bytes, erp_name, erp_bytes


async def _handle_reconcile(
    request: Request,
    pdf_file: Annotated[UploadFile, File(...)],
    erp_file: Annotated[UploadFile, File(...)],
) -> JSONResponse:
    pdf_name, pdf_bytes, erp_name, erp_bytes = await _validate_uploads(pdf_file, erp_file)
    svc: ReconciliationService = request.app.state.service

    with tempfile.TemporaryDirectory(prefix="ap_reconcile_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        pdf_tmp = tmp_path / "batch.pdf"
        erp_tmp = tmp_path / "erp.xlsx"
        pdf_tmp.write_bytes(pdf_bytes)
        erp_tmp.write_bytes(erp_bytes)

        try:
            result = await svc.reconcile(
                pdf_tmp, erp_tmp, pdf_display_name=pdf_name, erp_display_name=erp_name
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

        excel_tmp = tmp_path / "reconciliation_report.xlsx"
        write_excel_report(result.report, excel_tmp)
        excel_bytes = excel_tmp.read_bytes()

    store: _RunStore = request.app.state.run_store
    run_id = store.put(_StoredRun(report=result.report, page_pngs=result.page_pngs, excel_bytes=excel_bytes))
    return JSONResponse(
        status_code=200, content={"run_id": run_id, "report": result.report.model_dump(mode="json")}
    )


def create_app(service: ReconciliationService | None = None) -> FastAPI:
    """Build the FastAPI application, loading the LLM extractor once on startup unless injected."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_service = service is None
        active_service = service if service is not None else ReconciliationService.from_settings(Settings())
        app.state.service = active_service
        app.state.run_store = _RunStore()
        try:
            yield
        finally:
            if owns_service:
                await active_service.close()

    app = FastAPI(
        title="AP Transaction Reconciliation",
        description="Supplier invoice PDF to ERP AP reconciliation engine and review queue.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((_STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        svc: ReconciliationService = request.app.state.service
        return {
            "status": "ok",
            "provider": svc._primary.provider_name,
            "model": svc._primary.model_name,
            "retry_model": svc._retry.model_name if svc._retry is not None else None,
        }

    app.add_api_route("/api/reconcile", _handle_reconcile, methods=["POST"], response_class=JSONResponse)

    @app.get("/api/runs/{run_id}")
    async def get_run(request: Request, run_id: str) -> JSONResponse:
        stored = request.app.state.run_store.get(run_id)
        return JSONResponse(content={"run_id": run_id, "report": stored.report.model_dump(mode="json")})

    @app.get("/api/runs/{run_id}/pages/{page_number}")
    async def get_page_png(request: Request, run_id: str, page_number: int) -> Response:
        stored = request.app.state.run_store.get(run_id)
        png = stored.page_pngs.get(page_number)
        if png is None:
            raise HTTPException(status_code=404, detail=f"Page {page_number} not found for run '{run_id}'.")
        return Response(content=png, media_type="image/png")

    @app.get("/api/runs/{run_id}/download/xlsx")
    async def download_excel(request: Request, run_id: str) -> Response:
        stored = request.app.state.run_store.get(run_id)
        return Response(
            content=stored.excel_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="reconciliation_{run_id}.xlsx"'},
        )

    return app
