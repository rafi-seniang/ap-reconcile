"""Formatted 4-sheet Excel and JSON reconciliation report export service."""

import re
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.schemas.reconciliation import DocStatus, ReconciliationReport

_ILLEGAL_XML_CHARS_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_HEADER_FILL = PatternFill("solid", fgColor="1B4965")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
_SECTION_FONT = Font(bold=True, color="1B4965", size=11)
_BOLD_FONT = Font(bold=True, size=10)
_IDR_NUMBER_FORMAT = "#,##0"

_STATUS_CELL_FILLS: dict[str, PatternFill] = {
    DocStatus.MATCHED.value: PatternFill("solid", fgColor="D8F3DC"),
    DocStatus.MATCHED_WITHIN_TOLERANCE.value: PatternFill("solid", fgColor="E9F5DB"),
    DocStatus.AMOUNT_MISMATCH.value: PatternFill("solid", fgColor="FFE5D9"),
    DocStatus.MISSING_IN_ERP.value: PatternFill("solid", fgColor="FFF3CD"),
    DocStatus.MISSING_IN_PDF.value: PatternFill("solid", fgColor="FFF3CD"),
    DocStatus.DUPLICATE_IN_ERP.value: PatternFill("solid", fgColor="F8D7DA"),
    DocStatus.DUPLICATE_IN_PDF.value: PatternFill("solid", fgColor="F8D7DA"),
    DocStatus.NEEDS_REVIEW.value: PatternFill("solid", fgColor="FDE2E4"),
}


def _clean_excel_text(value: str) -> str:
    return _ILLEGAL_XML_CHARS_PATTERN.sub("", value)


def _append_safe_row(worksheet: Worksheet, values: list[Any]) -> int:
    """Append one row while stripping illegal XML control chars and preventing '=' formula evaluation."""
    cleaned_values = [_clean_excel_text(item) if isinstance(item, str) else item for item in values]
    worksheet.append(cleaned_values)
    row_index = worksheet.max_row
    for col_index, raw_item in enumerate(cleaned_values, start=1):
        if isinstance(raw_item, str) and raw_item.startswith("="):
            worksheet.cell(row_index, col_index).data_type = "s"
    return row_index


def _decimal_to_excel_number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _style_header_row(worksheet: Worksheet, headers: list[str]) -> None:
    worksheet.append(headers)
    for col_index in range(1, len(headers) + 1):
        cell = worksheet.cell(1, col_index)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    worksheet.freeze_panes = "A2"


def _autofit_worksheet_columns(worksheet: Worksheet, max_width: int = 52) -> None:
    for col_index in range(1, worksheet.max_column + 1):
        longest_length = 10
        for row_index in range(1, worksheet.max_row + 1):
            value = worksheet.cell(row_index, col_index).value
            if value is not None:
                longest_length = max(longest_length, min(len(str(value)) + 3, max_width))
        worksheet.column_dimensions[get_column_letter(col_index)].width = longest_length


def _write_control_totals_block(
    worksheet: Worksheet, report: ReconciliationReport, start_row: int
) -> int:
    totals = report.control_totals
    summary_rows: list[tuple[str, str | int | float | None, str | None]] = [
        ("Supplier PDF File", report.pdf_filename, None),
        ("ERP Export File", report.erp_filename, None),
        ("PDF Documents Extracted", totals.pdf_document_count, _IDR_NUMBER_FORMAT),
        ("ERP Entries Loaded", totals.erp_entry_count, _IDR_NUMBER_FORMAT),
        ("Unique Reconciled Documents", totals.reconciled_unique_docs, _IDR_NUMBER_FORMAT),
        ("", "", None),
        ("BATCH CONTROL TOTALS (IDR)", "", None),
        ("Raw PDF Invoices Total", _decimal_to_excel_number(totals.raw_pdf_invoices_total), _IDR_NUMBER_FORMAT),
        (
            "Raw PDF Credit Notes Total (signed)",
            _decimal_to_excel_number(totals.raw_pdf_credit_notes_total),
            _IDR_NUMBER_FORMAT,
        ),
        ("Raw PDF Net Total", _decimal_to_excel_number(totals.raw_pdf_net_total), _IDR_NUMBER_FORMAT),
        ("Raw ERP Invoices Total", _decimal_to_excel_number(totals.raw_erp_invoices_total), _IDR_NUMBER_FORMAT),
        (
            "Raw ERP Credit Notes Total (signed)",
            _decimal_to_excel_number(totals.raw_erp_credit_notes_total),
            _IDR_NUMBER_FORMAT,
        ),
        (
            "Raw ERP Net Total (Posted + Parked)",
            _decimal_to_excel_number(totals.raw_erp_net_total),
            _IDR_NUMBER_FORMAT,
        ),
        (
            "  - ERP Posted Net Total",
            _decimal_to_excel_number(totals.erp_posted_net_total),
            _IDR_NUMBER_FORMAT,
        ),
        (
            "  - ERP Parked Net Total",
            _decimal_to_excel_number(totals.erp_parked_net_total),
            _IDR_NUMBER_FORMAT,
        ),
        ("Net Difference (PDF Net - ERP Net)", _decimal_to_excel_number(totals.net_difference), _IDR_NUMBER_FORMAT),
        (
            "Total Explained Variance (sum by status)",
            _decimal_to_excel_number(totals.total_explained_difference),
            _IDR_NUMBER_FORMAT,
        ),
        (
            "Unexplained Variance (must be 0)",
            _decimal_to_excel_number(totals.unexplained_variance),
            _IDR_NUMBER_FORMAT,
        ),
    ]
    bold_labels = {
        "Raw PDF Net Total",
        "Raw ERP Net Total (Posted + Parked)",
        "Net Difference (PDF Net - ERP Net)",
        "Unexplained Variance (must be 0)",
    }
    row_cursor = start_row
    for label, value, number_format in summary_rows:
        label_cell = worksheet.cell(row_cursor, 1, label)
        safe_value = _clean_excel_text(value) if isinstance(value, str) else value
        value_cell = worksheet.cell(row_cursor, 2, safe_value)
        if isinstance(safe_value, str) and safe_value.startswith("="):
            value_cell.data_type = "s"
        if label.startswith("BATCH CONTROL") or label in bold_labels:
            label_cell.font = _SECTION_FONT if label.startswith("BATCH") else _BOLD_FONT
            value_cell.font = _BOLD_FONT
        if number_format and isinstance(value, (int, float)):
            value_cell.number_format = number_format
        row_cursor += 1
    return row_cursor


def _write_status_breakdown_block(
    worksheet: Worksheet, report: ReconciliationReport, start_row: int
) -> int:
    totals = report.control_totals
    row_cursor = start_row
    worksheet.cell(row_cursor, 1, "VARIANCE BREAKDOWN BY STATUS").font = _SECTION_FONT
    row_cursor += 1
    for col_index, title in enumerate(("Status", "Doc Count", "Signed Variance (IDR)"), start=1):
        cell = worksheet.cell(row_cursor, col_index, title)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    row_cursor += 1

    for status in DocStatus:
        count = totals.status_counts.get(status.value, 0)
        variance = totals.explained_by_status.get(status.value, Decimal("0"))
        status_cell = worksheet.cell(row_cursor, 1, status.value)
        worksheet.cell(row_cursor, 2, count)
        variance_cell = worksheet.cell(row_cursor, 3, _decimal_to_excel_number(variance))
        variance_cell.number_format = _IDR_NUMBER_FORMAT
        if status.value in _STATUS_CELL_FILLS:
            status_cell.fill = _STATUS_CELL_FILLS[status.value]
        row_cursor += 1
    return row_cursor


def _write_usage_block(worksheet: Worksheet, report: ReconciliationReport, start_row: int) -> None:
    if report.usage is None:
        return
    usage = report.usage
    row_cursor = start_row
    worksheet.cell(row_cursor, 1, "EXTRACTION RUNTIME & COST").font = _SECTION_FONT
    row_cursor += 1
    total_in = usage.input_tokens + usage.retry_input_tokens
    total_out = usage.output_tokens + usage.retry_output_tokens
    cost_label = f"${usage.estimated_cost_usd:.5f}" if usage.estimated_cost_usd is not None else "n/a"
    usage_rows = [
        ("Provider / Primary Model", f"{usage.provider} / {usage.model}"),
        ("Retry Model", usage.retry_model or "none"),
        ("Fallback Model / Calls", f"{usage.fallback_model or 'none'} ({usage.fallback_calls} calls)"),
        (
            "Primary Calls / Retry Calls / Cached Pages",
            f"{usage.primary_calls} / {usage.retry_calls} / {usage.cached_pages}",
        ),
        ("Total Input / Output Tokens", f"{total_in:,} / {total_out:,}"),
        ("Estimated Cost (USD)", cost_label),
        ("Extraction Latency (seconds)", f"{usage.latency_ms / 1000:.2f} s"),
    ]
    for metric_name, metric_value in usage_rows:
        worksheet.cell(row_cursor, 1, metric_name)
        worksheet.cell(row_cursor, 2, metric_value)
        row_cursor += 1


def _build_executive_summary_sheet(worksheet: Worksheet, report: ReconciliationReport) -> None:
    worksheet["A1"] = "AP Transaction Reconciliation - Executive Summary & Batch Control Totals"
    worksheet["A1"].font = Font(bold=True, size=13, color="1B4965")
    cursor = _write_control_totals_block(worksheet, report, start_row=3)
    cursor = _write_status_breakdown_block(worksheet, report, start_row=cursor + 1)
    _write_usage_block(worksheet, report, start_row=cursor + 1)
    _autofit_worksheet_columns(worksheet)


def _build_reconciliation_report_sheet(worksheet: Worksheet, report: ReconciliationReport) -> None:
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
    _style_header_row(worksheet, headers)

    for doc in report.documents:
        row_idx = _append_safe_row(
            worksheet,
            [
                doc.pdf_doc_number_raw or "",
                doc.erp_doc_number_raw or "",
                doc.document_type,
                ", ".join(str(page) for page in doc.pdf_pages),
                ", ".join(doc.erp_refs),
                doc.erp_doc_status or "",
                doc.pdf_po_or_ref or "",
                doc.erp_po_or_ref or "",
                _decimal_to_excel_number(doc.pdf_total_signed),
                _decimal_to_excel_number(doc.erp_total_signed),
                _decimal_to_excel_number(doc.difference_signed),
                doc.status.value,
                ", ".join(doc.reason_codes),
                doc.plain_explanation,
                doc.suggested_action,
            ],
        )
        for col_idx in (9, 10, 11):
            worksheet.cell(row_idx, col_idx).number_format = _IDR_NUMBER_FORMAT
        if doc.status.value in _STATUS_CELL_FILLS:
            worksheet.cell(row_idx, 12).fill = _STATUS_CELL_FILLS[doc.status.value]
        for col_idx in (14, 15):
            worksheet.cell(row_idx, col_idx).alignment = Alignment(wrap_text=True, vertical="top")

    _autofit_worksheet_columns(worksheet, max_width=56)


def _build_line_discrepancies_sheet(worksheet: Worksheet, report: ReconciliationReport) -> None:
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
    _style_header_row(worksheet, headers)

    for item in report.line_discrepancies:
        row_idx = _append_safe_row(
            worksheet,
            [
                item.doc_number_raw,
                item.discrepancy_type,
                item.item_code or "",
                item.pdf_page,
                item.pdf_line_number,
                item.pdf_description or "",
                _decimal_to_excel_number(item.pdf_qty),
                _decimal_to_excel_number(item.pdf_unit_price),
                _decimal_to_excel_number(item.pdf_discount_pct),
                _decimal_to_excel_number(item.pdf_line_amount),
                item.erp_ref or "",
                item.erp_description or "",
                _decimal_to_excel_number(item.erp_qty),
                _decimal_to_excel_number(item.erp_unit_price),
                _decimal_to_excel_number(item.erp_discount_pct),
                _decimal_to_excel_number(item.erp_line_amount),
                _decimal_to_excel_number(item.amount_difference),
                item.detail,
            ],
        )
        for col_idx in (8, 10, 14, 16, 17):
            worksheet.cell(row_idx, col_idx).number_format = _IDR_NUMBER_FORMAT
        worksheet.cell(row_idx, 18).alignment = Alignment(wrap_text=True, vertical="top")

    _autofit_worksheet_columns(worksheet, max_width=48)


def _build_audit_trail_sheet(worksheet: Worksheet, report: ReconciliationReport) -> None:
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
    _style_header_row(worksheet, headers)

    for ref in report.audit_trail:
        row_idx = _append_safe_row(
            worksheet,
            [
                ref.doc_number_norm,
                ref.doc_number_raw,
                ref.source_side,
                ref.level,
                ref.location,
                ref.field_or_item,
                ref.raw_value,
                _decimal_to_excel_number(ref.numeric_value),
            ],
        )
        worksheet.cell(row_idx, 8).number_format = _IDR_NUMBER_FORMAT

    _autofit_worksheet_columns(worksheet, max_width=50)


def _build_excel_workbook(report: ReconciliationReport) -> openpyxl.Workbook:
    workbook = openpyxl.Workbook()
    summary_sheet = workbook.active
    assert summary_sheet is not None
    summary_sheet.title = "Executive_Summary"
    _build_executive_summary_sheet(summary_sheet, report)
    _build_reconciliation_report_sheet(workbook.create_sheet("Reconciliation_Report"), report)
    _build_line_discrepancies_sheet(workbook.create_sheet("Line_Discrepancies"), report)
    _build_audit_trail_sheet(workbook.create_sheet("Audit_Trail"), report)
    return workbook


def write_excel_report(report: ReconciliationReport, output_path: Path | str) -> Path:
    """Write the 4-sheet formatted Excel workbook to disk."""
    target_path = Path(output_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = _build_excel_workbook(report)
    workbook.save(target_path)
    return target_path


def render_excel_report_bytes(report: ReconciliationReport) -> bytes:
    """Render the 4-sheet formatted Excel workbook directly to in-memory bytes for HTTP downloads."""
    buffer = BytesIO()
    workbook = _build_excel_workbook(report)
    workbook.save(buffer)
    return buffer.getvalue()


class ReportService:
    """Service responsible for writing reconciliation outputs (formatted Excel and JSON)."""

    def write_batch_outputs(self, report: ReconciliationReport, output_dir: Path | str) -> dict[str, Path]:
        """Write reconciliation_report.xlsx and reconciliation_report.json into output_dir."""
        target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        excel_path = write_excel_report(report, target_dir / "reconciliation_report.xlsx")
        json_path = target_dir / "reconciliation_report.json"
        json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return {"excel": excel_path, "json": json_path}

    def render_excel_bytes(self, report: ReconciliationReport) -> bytes:
        """Render the 4-sheet reconciliation Excel report into memory without temporary disk files."""
        return render_excel_report_bytes(report)
