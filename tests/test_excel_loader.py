"""Unit tests for strict upstream ERP Excel contract validation and mixed-format parsing."""

import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from app.schemas.erp import ErpValidationError
from app.services.erp_service import load_erp_workbook


def _write_sample_erp(path: Path) -> Path:
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
            "Posted By",
        ]
    )
    ws_inv.append(
        [
            "E-10001",
            "PT Prima Kertas",
            "PKI/INV/2026/09/0152",
            "09/23/2026",  # US string date
            "PO-88230",
            "IDR",
            14071250,
            12898646,
            1547838,
            "15,619,088",  # Comma-formatted string total
            "Posted",
            "clerk1",
        ]
    )
    ws_inv.append(
        [
            "E-10002",
            "PT Prima Kertas",
            "PKI-INV-2026-09-0155",
            "2026-09-29",  # ISO string date
            "PO-88240",
            "IDR",
            3147490,
            2885199,
            346224,
            3493714,
            "Parked",
            "clerk2",
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
    ws_lines.append(["E-10001", 1, "SKU-1", "Paper A4", 10, "rim", 1407125, 0, 14071250])
    ws_lines.append(["E-10002", 1, "SKU-2", "Paper F4", 10, "rim", 314749, 0, 3147490])
    ws_lines.append(["E-10003", 1, "SKU-1", "Return Paper", -1, "rim", 876000, 0, -876000])

    ws_cn = wb.create_sheet("AP_CreditNotes")
    ws_cn.append(
        [
            "Entry ID",
            "Vendor",
            "Vendor CN No",
            "Doc Date",
            "Original Invoice",
            "Curr",
            "Subtotal",
            "DPP",
            "PPN",
            "Grand Total",
            "Doc Status",
        ]
    )
    ws_cn.append(
        [
            "E-10003",
            "PT Prima Kertas",
            "PKI/CN/2026/09/0012",
            datetime(2026, 9, 26),
            "PKI/INV/2026/08/0131",
            "IDR",
            -876000,
            -803000,
            -96360,
            -972360,
            "Posted",
        ]
    )

    ws_info = wb.create_sheet("Export_Info")
    ws_info.append(["Batch", "September 2026"])

    wb.save(path)
    return path


def test_load_erp_workbook_handles_mixed_dates_and_string_numbers(tmp_path: Path) -> None:
    erp_path = _write_sample_erp(tmp_path / "erp_ok.xlsx")
    wb = load_erp_workbook(erp_path)

    assert len(wb.documents) == 3
    inv1 = wb.documents[0]
    assert inv1.doc_date == date(2026, 9, 23)
    assert inv1.grand_total_signed == Decimal("15619088")
    assert inv1.audit_ref == "AP_Invoices!R2 (E-10001)"
    assert len(inv1.lines) == 1

    inv2 = wb.documents[1]
    assert inv2.doc_date == date(2026, 9, 29)
    assert inv2.doc_number_norm == "PKIINV2026090155"
    assert inv2.doc_status == "Parked"

    cn = wb.documents[2]
    assert cn.document_type == "credit_note"
    assert cn.grand_total_signed == Decimal("-972360")
    assert wb.export_info == {"Batch": "September 2026"}


def test_load_erp_workbook_fails_loudly_on_missing_sheet(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "AP_Invoices"
    path = tmp_path / "missing_sheet.xlsx"
    wb.save(path)

    with pytest.raises(ErpValidationError) as exc_info:
        load_erp_workbook(path)
    assert any(issue.sheet == "AP_Lines" for issue in exc_info.value.issues)


def test_load_erp_workbook_collects_all_bad_cells_and_orphan_lines_in_one_error(tmp_path: Path) -> None:
    path = _write_sample_erp(tmp_path / "erp_broken.xlsx")
    wb = openpyxl.load_workbook(path)
    # Introduce three independent cell errors across sheets plus one orphan line Entry ID.
    wb["AP_Invoices"].cell(2, 4, "32/99/2026")  # bad Doc Date
    wb["AP_Invoices"].cell(2, 10, "TBD")  # bad Grand Total
    wb["AP_Lines"].cell(2, 5, "twelve")  # bad Qty
    wb["AP_Lines"].append(["E-99999", 1, "SKU-X", "Orphan", 1, "pcs", 1000, 0, 1000])
    wb.save(path)

    with pytest.raises(ErpValidationError) as exc_info:
        load_erp_workbook(path)

    issues = exc_info.value.issues
    assert len(issues) == 4
    locations = {(i.sheet, i.row, i.column) for i in issues}
    assert ("AP_Invoices", 2, "Doc Date") in locations
    assert ("AP_Invoices", 2, "Grand Total") in locations
    assert ("AP_Lines", 2, "Qty") in locations
    assert ("AP_Lines", 5, "Entry ID") in locations


def test_load_real_september_2026_erp_workbook_if_present() -> None:
    env_path = os.environ.get("SEP_ERP_PATH")
    real_erp = Path(env_path) if env_path else Path("data/sep2026/ERP_AP_Export_Sep2026.xlsx")
    if not real_erp.is_file():
        pytest.skip("Optional September 2026 ERP export not configured via SEP_ERP_PATH or data/sep2026")

    wb = load_erp_workbook(real_erp)
    assert len(wb.documents) == 16  # 15 AP_Invoices + 1 AP_CreditNotes
    net = sum((d.grand_total_signed for d in wb.documents), Decimal("0"))
    assert net == Decimal("311153348")


def test_load_erp_workbook_rejects_excel_errors_and_duplicate_entry_ids(tmp_path: Path) -> None:
    path = _write_sample_erp(tmp_path / "erp_formula_err.xlsx")
    wb = openpyxl.load_workbook(path)
    wb["AP_Invoices"].cell(2, 3, "#REF!")
    wb["AP_Invoices"].cell(2, 5, "#N/A")
    wb["AP_Invoices"].append(
        [
            "E-10001",
            "PT Prima Kertas",
            "PKI/INV/2026/09/9999",
            "2026-09-28",
            "PO-88299",
            "Rp",
            100000,
            91667,
            11000,
            111000,
            "Posted",
            "clerk1",
        ]
    )
    wb.save(path)

    with pytest.raises(ErpValidationError) as exc_info:
        load_erp_workbook(path)

    locations = {(i.sheet, i.row, i.column) for i in exc_info.value.issues}
    assert ("AP_Invoices", 2, "vendor inv. no") in locations
    assert ("AP_Invoices", 2, "po no") in locations
    assert ("AP_Invoices", 4, "Entry ID") in locations


def test_load_erp_workbook_accepts_float_line_numbers_and_excel_percentage_format(tmp_path: Path) -> None:
    path = _write_sample_erp(tmp_path / "erp_excel_formats.xlsx")
    wb = openpyxl.load_workbook(path)
    ws_lines = wb["AP_Lines"]
    ws_lines.cell(2, 2, 1.0)
    disc_cell = ws_lines.cell(2, 8, 0.05)
    disc_cell.number_format = "0%"
    wb.save(path)

    loaded = load_erp_workbook(path)
    first_line = loaded.documents[0].lines[0]
    assert first_line.line_number == 1
    assert first_line.discount_pct == Decimal("5")
