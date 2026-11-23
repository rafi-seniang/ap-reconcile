"""Formatted 4-sheet Excel reconciliation report writer for AP reviewers and CFO sign-off."""

from decimal import Decimal
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.reconcile.schemas import DocStatus, ReconciliationReport

_HEADER_FILL = PatternFill("solid", fgColor="1B4965")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
_SECTION_FONT = Font(bold=True, color="1B4965", size=11)
_BOLD_FONT = Font(bold=True, size=10)
_IDR_FORMAT = "#,##0"

_STATUS_FILLS: dict[str, PatternFill] = {
    DocStatus.MATCHED.value: PatternFill("solid", fgColor="D8F3DC"),
    DocStatus.MATCHED_WITHIN_TOLERANCE.value: PatternFill("solid", fgColor="E9F5DB"),
    DocStatus.AMOUNT_MISMATCH.value: PatternFill("solid", fgColor="FFE5D9"),
    DocStatus.MISSING_IN_ERP.value: PatternFill("solid", fgColor="FFF3CD"),
    DocStatus.MISSING_IN_PDF.value: PatternFill("solid", fgColor="FFF3CD"),
    DocStatus.DUPLICATE_IN_ERP.value: PatternFill("solid", fgColor="F8D7DA"),
    DocStatus.DUPLICATE_IN_PDF.value: PatternFill("solid", fgColor="F8D7DA"),
    DocStatus.NEEDS_REVIEW.value: PatternFill("solid", fgColor="FDE2E4"),
}


def _to_number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _style_header_row(ws: Worksheet, headers: list[str]) -> None:
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(1, col_idx)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"


def _autofit_columns(ws: Worksheet, max_width: int = 52) -> None:
    for col_idx in range(1, ws.max_column + 1):
        longest = 10
        for row_idx in range(1, ws.max_row + 1):
            val = ws.cell(row_idx, col_idx).value
            if val is not None:
                longest = max(longest, min(len(str(val)) + 3, max_width))
        ws.column_dimensions[get_column_letter(col_idx)].width = longest


def _write_control_totals_block(ws: Worksheet, report: ReconciliationReport, row_cursor: int) -> int:
    ct = report.control_totals
    rows: list[tuple[str, str | int | float | None, str | None]] = [
        ("Supplier PDF File", report.pdf_filename, None),
        ("ERP Export File", report.erp_filename, None),
        ("PDF Documents Extracted", ct.pdf_document_count, _IDR_FORMAT),
        ("ERP Entries Loaded", ct.erp_entry_count, _IDR_FORMAT),
        ("Unique Reconciled Documents", ct.reconciled_unique_docs, _IDR_FORMAT),
        ("", "", None),
        ("BATCH CONTROL TOTALS (IDR)", "", None),
        ("Raw PDF Invoices Total", _to_number(ct.raw_pdf_invoices_total), _IDR_FORMAT),
        ("Raw PDF Credit Notes Total (signed)", _to_number(ct.raw_pdf_credit_notes_total), _IDR_FORMAT),
        ("Raw PDF Net Total", _to_number(ct.raw_pdf_net_total), _IDR_FORMAT),
        ("Raw ERP Invoices Total", _to_number(ct.raw_erp_invoices_total), _IDR_FORMAT),
        ("Raw ERP Credit Notes Total (signed)", _to_number(ct.raw_erp_credit_notes_total), _IDR_FORMAT),
        ("Raw ERP Net Total (Posted + Parked)", _to_number(ct.raw_erp_net_total), _IDR_FORMAT),
        ("  - ERP Posted Net Total", _to_number(ct.erp_posted_net_total), _IDR_FORMAT),
        ("  - ERP Parked Net Total", _to_number(ct.erp_parked_net_total), _IDR_FORMAT),
        ("Net Difference (PDF Net - ERP Net)", _to_number(ct.net_difference), _IDR_FORMAT),
        ("Total Explained Variance (sum by status)", _to_number(ct.total_explained_difference), _IDR_FORMAT),
        ("Unexplained Variance (must be 0)", _to_number(ct.unexplained_variance), _IDR_FORMAT),
    ]
    bold_labels = {
        "Raw PDF Net Total",
        "Raw ERP Net Total (Posted + Parked)",
        "Net Difference (PDF Net - ERP Net)",
        "Unexplained Variance (must be 0)",
    }
    for label, val, num_fmt in rows:
        c1 = ws.cell(row_cursor, 1, label)
        c2 = ws.cell(row_cursor, 2, val)
        if label.startswith("BATCH CONTROL") or label in bold_labels:
            c1.font = _SECTION_FONT if label.startswith("BATCH") else _BOLD_FONT
            c2.font = _BOLD_FONT
        if num_fmt and isinstance(val, (int, float)):
            c2.number_format = num_fmt
        row_cursor += 1
    return row_cursor


def _write_status_breakdown_block(ws: Worksheet, report: ReconciliationReport, row_cursor: int) -> int:
    ct = report.control_totals
    ws.cell(row_cursor, 1, "VARIANCE BREAKDOWN BY STATUS").font = _SECTION_FONT
    row_cursor += 1
    for col_idx, title in enumerate(("Status", "Doc Count", "Signed Variance (IDR)"), start=1):
        cell = ws.cell(row_cursor, col_idx, title)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    row_cursor += 1

    for status in DocStatus:
        count = ct.status_counts.get(status.value, 0)
        variance = ct.explained_by_status.get(status.value, Decimal("0"))
        c_st = ws.cell(row_cursor, 1, status.value)
        ws.cell(row_cursor, 2, count)
        c_var = ws.cell(row_cursor, 3, _to_number(variance))
        c_var.number_format = _IDR_FORMAT
        if status.value in _STATUS_FILLS:
            c_st.fill = _STATUS_FILLS[status.value]
        row_cursor += 1
    return row_cursor


def _write_usage_block(ws: Worksheet, report: ReconciliationReport, row_cursor: int) -> None:
    if report.usage is None:
        return
    u = report.usage
    ws.cell(row_cursor, 1, "EXTRACTION RUNTIME & COST").font = _SECTION_FONT
    row_cursor += 1
    total_in = u.input_tokens + u.retry_input_tokens
    total_out = u.output_tokens + u.retry_output_tokens
    cost_str = f"${u.estimated_cost_usd:.5f}" if u.estimated_cost_usd is not None else "n/a"
    usage_rows = [
        ("Provider / Primary Model", f"{u.provider} / {u.model}"),
        ("Retry Model", u.retry_model or "none"),
        (
            "Primary Calls / Retry Calls / Cached Pages",
            f"{u.primary_calls} / {u.retry_calls} / {u.cached_pages}",
        ),
        ("Total Input / Output Tokens", f"{total_in:,} / {total_out:,}"),
        ("Estimated Cost (USD)", cost_str),
        ("Extraction Latency (seconds)", f"{u.latency_ms / 1000:.2f} s"),
    ]
    for k, v in usage_rows:
        ws.cell(row_cursor, 1, k)
        ws.cell(row_cursor, 2, v)
        row_cursor += 1


def _build_summary_sheet(ws: Worksheet, report: ReconciliationReport) -> None:
    ws["A1"] = "AP Transaction Reconciliation - Executive Summary & Batch Control Totals"
    ws["A1"].font = Font(bold=True, size=13, color="1B4965")
    cursor = _write_control_totals_block(ws, report, row_cursor=3)
    cursor = _write_status_breakdown_block(ws, report, row_cursor=cursor + 1)
    _write_usage_block(ws, report, row_cursor=cursor + 1)
    _autofit_columns(ws)


def _build_reconciliation_sheet(ws: Worksheet, report: ReconciliationReport) -> None:
    headers = [
        "Doc No (PDF)",
        "Doc No (ERP)",
        "Type",
        "PDF Pages",
        "ERP Refs",
        "ERP Status",
        "PO / Ref (PDF)",
        "PO / Ref (ERP)",
        "PDF Total (IDR)",
        "ERP Total (IDR)",
        "Difference (IDR)",
        "Status",
        "Reason Codes",
        "Plain-Language Explanation",
        "Suggested Action",
    ]
    _style_header_row(ws, headers)

    for doc in report.documents:
        ws.append(
            [
                doc.pdf_doc_number_raw or "",
                doc.erp_doc_number_raw or "",
                doc.document_type,
                ", ".join(str(p) for p in doc.pdf_pages),
                ", ".join(doc.erp_refs),
                doc.erp_doc_status or "",
                doc.pdf_po_or_ref or "",
                doc.erp_po_or_ref or "",
                _to_number(doc.pdf_total_signed),
                _to_number(doc.erp_total_signed),
                _to_number(doc.difference_signed),
                doc.status.value,
                ", ".join(doc.reason_codes),
                doc.plain_explanation,
                doc.suggested_action,
            ]
        )
        r = ws.max_row
        for col_idx in (9, 10, 11):
            ws.cell(r, col_idx).number_format = _IDR_FORMAT
        status_cell = ws.cell(r, 12)
        if doc.status.value in _STATUS_FILLS:
            status_cell.fill = _STATUS_FILLS[doc.status.value]
        for col_idx in (14, 15):
            ws.cell(r, col_idx).alignment = Alignment(wrap_text=True, vertical="top")

    _autofit_columns(ws, max_width=56)


def _build_line_discrepancies_sheet(ws: Worksheet, report: ReconciliationReport) -> None:
    headers = [
        "Doc No",
        "Discrepancy Type",
        "Item Code",
        "PDF Page",
        "PDF Line",
        "PDF Description",
        "PDF Qty",
        "PDF Unit Price",
        "PDF Disc %",
        "PDF Line Amount",
        "ERP Ref",
        "ERP Description",
        "ERP Qty",
        "ERP Unit Price",
        "ERP Disc %",
        "ERP Line Amount",
        "Amount Difference",
        "Detail",
    ]
    _style_header_row(ws, headers)

    for item in report.line_discrepancies:
        ws.append(
            [
                item.doc_number_raw,
                item.discrepancy_type,
                item.item_code or "",
                item.pdf_page,
                item.pdf_line_number,
                item.pdf_description or "",
                _to_number(item.pdf_qty),
                _to_number(item.pdf_unit_price),
                _to_number(item.pdf_discount_pct),
                _to_number(item.pdf_line_amount),
                item.erp_ref or "",
                item.erp_description or "",
                _to_number(item.erp_qty),
                _to_number(item.erp_unit_price),
                _to_number(item.erp_discount_pct),
                _to_number(item.erp_line_amount),
                _to_number(item.amount_difference),
                item.detail,
            ]
        )
        r = ws.max_row
        for col_idx in (8, 10, 14, 16, 17):
            ws.cell(r, col_idx).number_format = _IDR_FORMAT
        ws.cell(r, 18).alignment = Alignment(wrap_text=True, vertical="top")

    _autofit_columns(ws, max_width=48)


def _build_audit_sheet(ws: Worksheet, report: ReconciliationReport) -> None:
    headers = [
        "Doc No (Norm)",
        "Doc No (Raw)",
        "Source Side",
        "Level",
        "Location (PDF Page / ERP Sheet!Row)",
        "Field or Item",
        "Raw Printed / Booked Value",
        "Signed Numeric Value (IDR)",
    ]
    _style_header_row(ws, headers)

    for ref in report.audit_trail:
        ws.append(
            [
                ref.doc_number_norm,
                ref.doc_number_raw,
                ref.source_side,
                ref.level,
                ref.location,
                ref.field_or_item,
                ref.raw_value,
                _to_number(ref.numeric_value),
            ]
        )
        ws.cell(ws.max_row, 8).number_format = _IDR_FORMAT

    _autofit_columns(ws, max_width=50)


def write_excel_report(report: ReconciliationReport, output_path: Path | str) -> Path:
    """Write Executive_Summary, Reconciliation_Report, Line_Discrepancies, and Audit_Trail sheets."""
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()
    ws_summary = wb.active
    assert ws_summary is not None
    ws_summary.title = "Executive_Summary"
    _build_summary_sheet(ws_summary, report)

    _build_reconciliation_sheet(wb.create_sheet("Reconciliation_Report"), report)
    _build_line_discrepancies_sheet(wb.create_sheet("Line_Discrepancies"), report)
    _build_audit_sheet(wb.create_sheet("Audit_Trail"), report)

    wb.save(out_file)
    return out_file
