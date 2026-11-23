"""FastAPI Review Queue endpoint tests with injected fake extractor (no live API calls)."""

from pathlib import Path
from typing import Any

import openpyxl
import pymupdf
from fastapi.testclient import TestClient

from app.api.routes import create_app
from app.extraction.llm_client import TokenCount, UpstreamUnavailable
from app.extraction.schemas import PrintedLine, PrintedPage
from app.extraction.splitter import PdfPagePayload
from app.service import ReconciliationService
from config.settings import Settings


class _StubExtractor:
    provider_name = "stub"
    model_name = "stub-model"

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.calls = 0

    async def extract_page(
        self,
        page: PdfPagePayload,
        total_pages: int,
        retry_feedback: list[str] | None = None,
    ) -> tuple[PrintedPage, TokenCount]:
        self.calls += 1
        if self._fail:
            raise UpstreamUnavailable("Simulated provider outage")
        return (
            PrintedPage(
                doc_number_raw="PKI/INV/2026/09/0001",
                po_number_raw="PO-1",
                subtotal_raw="1.000.000",
                dpp_raw="916.667",
                ppn_raw="110.000",
                grand_total_raw="1.110.000",
                lines=[
                    PrintedLine(
                        line_number_raw="1",
                        item_code="SKU-1",
                        description="Paper",
                        quantity_raw="10",
                        uom_raw="rim",
                        unit_price_raw="100.000",
                        discount_pct_raw="0",
                        line_amount_raw="1.000.000",
                    )
                ],
            ),
            TokenCount(input_tokens=50, output_tokens=30),
        )

    async def close(self) -> None:
        return None


def _make_pdf_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "sample.pdf"
    fitz_mod: Any = pymupdf
    doc = fitz_mod.open()
    page = doc.new_page()
    page.insert_text((72, 72), "PKI/INV/2026/09/0001")
    doc.save(path)
    doc.close()
    return path.read_bytes()


def _make_erp_bytes(tmp_path: Path, broken: bool = False) -> bytes:
    path = tmp_path / ("broken.xlsx" if broken else "valid.xlsx")
    wb = openpyxl.Workbook()
    ws_inv = wb.active
    assert ws_inv is not None
    ws_inv.title = "AP_Invoices"
    ws_inv.append(
        [
            "Entry ID",
            "Vendor",
            "Vendor Inv. No",
            "Doc Date",
            "PO No",
            "Curr",
            "Subtotal",
            "DPP",
            "PPN",
            "Grand Total",
            "Doc Status",
        ]
    )
    ws_inv.append(
        [
            "E-1",
            "Vendor",
            "PKI/INV/2026/09/0001",
            "bad-date" if broken else "2026-09-10",
            "PO-1",
            "IDR",
            1000000,
            916667,
            110000,
            1110000,
            "Posted",
        ]
    )
    ws_lines = wb.create_sheet("AP_Lines")
    ws_lines.append(
        [
            "Entry ID",
            "Line",
            "Item Code",
            "Item Description",
            "Qty",
            "UOM",
            "Unit Price",
            "Disc %",
            "Line Amount",
        ]
    )
    ws_lines.append(["E-1", 1, "SKU-1", "Paper", 10, "rim", 100000, 0, 1000000])
    wb.save(path)
    return path.read_bytes()


def test_api_reconcile_happy_path_and_downloads(tmp_path: Path) -> None:
    stub = _StubExtractor()
    app = create_app(service=ReconciliationService(Settings(), stub, None))

    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/").status_code == 200
        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200

        res = client.post(
            "/api/reconcile",
            files={
                "pdf_file": ("sep.pdf", _make_pdf_bytes(tmp_path), "application/pdf"),
                "erp_file": (
                    "erp.xlsx",
                    _make_erp_bytes(tmp_path),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
        )
        assert res.status_code == 200
        body = res.json()
        run_id = body["run_id"]
        assert body["report"]["control_totals"]["unexplained_variance"] == "0"
        assert body["report"]["documents"][0]["status"] == "MATCHED"

        assert client.get(f"/api/runs/{run_id}").status_code == 200
        page_res = client.get(f"/api/runs/{run_id}/pages/1")
        assert page_res.status_code == 200
        assert page_res.headers["content-type"] == "image/png"

        xlsx_res = client.get(f"/api/runs/{run_id}/download/xlsx")
        assert xlsx_res.status_code == 200
        assert len(xlsx_res.content) > 1000


def test_api_returns_422_with_cell_issues_and_zero_llm_calls_on_broken_erp(tmp_path: Path) -> None:
    stub = _StubExtractor()
    app = create_app(service=ReconciliationService(Settings(), stub, None))

    with TestClient(app) as client:
        res = client.post(
            "/api/reconcile",
            files={
                "pdf_file": ("sep.pdf", _make_pdf_bytes(tmp_path), "application/pdf"),
                "erp_file": (
                    "broken.xlsx",
                    _make_erp_bytes(tmp_path, broken=True),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
        )
        assert res.status_code == 422
        payload = res.json()
        assert payload["error"] == "erp_validation_failed"
        assert payload["issues"][0]["sheet"] == "AP_Invoices"
        assert payload["issues"][0]["column"] == "Doc Date"
        assert stub.calls == 0


def test_api_returns_503_when_upstream_llm_unavailable(tmp_path: Path) -> None:
    stub = _StubExtractor(fail=True)
    app = create_app(service=ReconciliationService(Settings(), stub, None))

    with TestClient(app) as client:
        res = client.post(
            "/api/reconcile",
            files={
                "pdf_file": ("sep.pdf", _make_pdf_bytes(tmp_path), "application/pdf"),
                "erp_file": (
                    "erp.xlsx",
                    _make_erp_bytes(tmp_path),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
        )
        assert res.status_code == 503
        assert res.json()["error"] == "upstream_unavailable"
