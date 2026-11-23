"""ERP workbook loading and fail-fast schema validation service."""

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.worksheet.worksheet import Worksheet

from app.llm.parser import (
    normalize_currency,
    normalize_key,
    parse_date_flexible,
    parse_decimal_amount,
    parse_discount_pct,
)
from app.schemas.erp import ErpDocument, ErpLine, ErpValidationError, ErpValidationIssue, ErpWorkbook

_EXCEL_FORMULA_ERRORS = frozenset({"#REF!", "#N/A", "#VALUE!", "#DIV/0!", "#NAME?", "#NUM!", "#NULL!"})

_INVOICE_REQUIRED_COLUMNS = (
    "entry id",
    "vendor",
    "vendor inv. no",
    "doc date",
    "po no",
    "curr",
    "subtotal",
    "dpp",
    "ppn",
    "grand total",
    "doc status",
)

_CREDIT_NOTE_REQUIRED_COLUMNS = (
    "entry id",
    "vendor",
    "vendor cn no",
    "doc date",
    "original invoice",
    "curr",
    "subtotal",
    "dpp",
    "ppn",
    "grand total",
    "doc status",
)

_LINE_REQUIRED_COLUMNS = (
    "entry id",
    "line",
    "item code",
    "item description",
    "qty",
    "uom",
    "unit price",
    "disc %",
    "line amount",
)


def _normalize_header_name(value: Any) -> str:
    return " ".join(str(value).strip().lower().split()) if value is not None else ""


def _map_required_headers(
    worksheet: Worksheet,
    required_columns: tuple[str, ...],
    issues: list[ErpValidationIssue],
) -> dict[str, int]:
    header_map: dict[str, int] = {}
    for col_index in range(1, worksheet.max_column + 1):
        normalized_name = _normalize_header_name(worksheet.cell(1, col_index).value)
        if normalized_name:
            header_map[normalized_name] = col_index
    for required_col in required_columns:
        if required_col not in header_map:
            issues.append(
                ErpValidationIssue(
                    sheet=worksheet.title,
                    row=1,
                    column=required_col,
                    message=f"missing required column '{required_col}' (found: {list(header_map.keys())})",
                )
            )
    return header_map


def _is_empty_worksheet_row(worksheet: Worksheet, row_index: int, max_column: int) -> bool:
    return all(worksheet.cell(row_index, col_idx).value is None for col_idx in range(1, max_column + 1))


def _cell_to_clean_str(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _is_excel_formula_error(text: str | None) -> bool:
    return text is not None and text.strip().upper() in _EXCEL_FORMULA_ERRORS


def _parse_erp_date_cell(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _cell_to_clean_str(value)
    if text is None or _is_excel_formula_error(text):
        return None
    # ERP string exports use MM/DD/YYYY when slash-delimited and YYYY-MM-DD when dashed.
    return parse_date_flexible(text, prefer_month_first="/" in text)


def _parse_erp_amount_cell(value: Any) -> Decimal | None:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value))
    text = _cell_to_clean_str(value)
    if _is_excel_formula_error(text):
        return None
    return parse_decimal_amount(text)


def _parse_erp_discount_cell(value: Any) -> Decimal | None:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return parse_discount_pct(value)
    return parse_discount_pct(_cell_to_clean_str(value))


def _record_validation_issue(
    issues: list[ErpValidationIssue],
    sheet: str,
    row_index: int,
    column_name: str,
    message: str,
    raw_value: Any = None,
) -> None:
    issues.append(
        ErpValidationIssue(
            sheet=sheet,
            row=row_index,
            column=column_name,
            raw_value=str(raw_value) if raw_value is not None else None,
            message=message,
        )
    )


def _parse_line_number_cell(
    raw_value: Any, sheet: str, row_index: int, issues: list[ErpValidationIssue]
) -> int | None:
    if isinstance(raw_value, int) and not isinstance(raw_value, bool):
        return raw_value
    if raw_value is not None and str(raw_value).strip().isdigit():
        return int(str(raw_value).strip())
    _record_validation_issue(issues, sheet, row_index, "Line", "line number must be an integer", raw_value)
    return None


def _require_amount_cell(
    raw_value: Any,
    sheet: str,
    row_index: int,
    column_name: str,
    error_message: str,
    issues: list[ErpValidationIssue],
) -> Decimal | None:
    parsed_amount = _parse_erp_amount_cell(raw_value)
    if parsed_amount is None:
        _record_validation_issue(issues, sheet, row_index, column_name, error_message, raw_value)
    return parsed_amount


def _parse_erp_line_row(
    worksheet: Worksheet,
    row_index: int,
    header_map: dict[str, int],
    issues: list[ErpValidationIssue],
) -> ErpLine | None:
    entry_id = _cell_to_clean_str(worksheet.cell(row_index, header_map["entry id"]).value)
    item_code = _cell_to_clean_str(worksheet.cell(row_index, header_map["item code"]).value)
    description = _cell_to_clean_str(worksheet.cell(row_index, header_map["item description"]).value) or ""
    uom = _cell_to_clean_str(worksheet.cell(row_index, header_map["uom"]).value)

    row_is_valid = True
    if not entry_id or _is_excel_formula_error(entry_id):
        _record_validation_issue(issues, worksheet.title, row_index, "Entry ID", "missing or invalid Entry ID", entry_id)
        row_is_valid = False
    if _is_excel_formula_error(item_code):
        _record_validation_issue(
            issues, worksheet.title, row_index, "Item Code", "Excel formula error in Item Code", item_code
        )
        row_is_valid = False

    line_number = _parse_line_number_cell(
        worksheet.cell(row_index, header_map["line"]).value, worksheet.title, row_index, issues
    )
    quantity = _require_amount_cell(
        worksheet.cell(row_index, header_map["qty"]).value,
        worksheet.title,
        row_index,
        "Qty",
        "cannot parse quantity as number",
        issues,
    )
    unit_price = _require_amount_cell(
        worksheet.cell(row_index, header_map["unit price"]).value,
        worksheet.title,
        row_index,
        "Unit Price",
        "cannot parse unit price as IDR amount",
        issues,
    )
    discount_raw = worksheet.cell(row_index, header_map["disc %"]).value
    discount_pct = _parse_erp_discount_cell(discount_raw)
    if discount_pct is None:
        _record_validation_issue(
            issues, worksheet.title, row_index, "Disc %", "cannot parse discount (expected 0 to 100)", discount_raw
        )
    line_amount = _require_amount_cell(
        worksheet.cell(row_index, header_map["line amount"]).value,
        worksheet.title,
        row_index,
        "Line Amount",
        "cannot parse line amount as IDR amount",
        issues,
    )

    if (
        not row_is_valid
        or entry_id is None
        or line_number is None
        or quantity is None
        or unit_price is None
        or discount_pct is None
        or line_amount is None
    ):
        return None
    return ErpLine(
        entry_id=entry_id,
        sheet=worksheet.title,
        row=row_index,
        line_number=line_number,
        item_code_raw=item_code,
        item_code_norm=normalize_key(item_code),
        description=description,
        quantity_signed=quantity,
        quantity_abs=abs(quantity),
        uom_raw=uom,
        unit_price=abs(unit_price),
        discount_pct=discount_pct,
        line_amount_signed=line_amount,
        line_amount_abs=abs(line_amount),
    )


def _load_all_erp_lines(
    worksheet: Worksheet,
    header_map: dict[str, int],
    issues: list[ErpValidationIssue],
) -> dict[str, list[ErpLine]]:
    lines_by_entry_id: dict[str, list[ErpLine]] = {}
    for row_index in range(2, worksheet.max_row + 1):
        if _is_empty_worksheet_row(worksheet, row_index, worksheet.max_column):
            continue
        parsed_line = _parse_erp_line_row(worksheet, row_index, header_map, issues)
        if parsed_line is not None:
            lines_by_entry_id.setdefault(parsed_line.entry_id, []).append(parsed_line)
    return lines_by_entry_id


def _validate_header_identity_cells(
    worksheet: Worksheet,
    row_index: int,
    entry_id: str | None,
    doc_number_raw: str | None,
    doc_number_col: str,
    reference_raw: str | None,
    reference_col: str,
    currency_raw: str | None,
    seen_entry_ids: set[str],
    issues: list[ErpValidationIssue],
) -> bool:
    row_is_valid = True
    if not entry_id or _is_excel_formula_error(entry_id):
        _record_validation_issue(issues, worksheet.title, row_index, "Entry ID", "missing or invalid Entry ID", entry_id)
        row_is_valid = False
    elif entry_id in seen_entry_ids:
        _record_validation_issue(
            issues, worksheet.title, row_index, "Entry ID", f"duplicate Entry ID '{entry_id}'", entry_id
        )
        row_is_valid = False
    else:
        seen_entry_ids.add(entry_id)

    if not doc_number_raw or _is_excel_formula_error(doc_number_raw) or not normalize_key(doc_number_raw):
        _record_validation_issue(
            issues, worksheet.title, row_index, doc_number_col, "missing or invalid document number", doc_number_raw
        )
        row_is_valid = False
    if _is_excel_formula_error(reference_raw):
        _record_validation_issue(
            issues, worksheet.title, row_index, reference_col, "Excel formula error in reference column", reference_raw
        )
        row_is_valid = False
    if _is_excel_formula_error(currency_raw):
        _record_validation_issue(
            issues, worksheet.title, row_index, "Curr", "Excel formula error in currency", currency_raw
        )
        row_is_valid = False
    return row_is_valid


def _parse_erp_header_row(
    worksheet: Worksheet,
    row_index: int,
    header_map: dict[str, int],
    is_credit_note: bool,
    seen_entry_ids: set[str],
    lines_by_entry_id: dict[str, list[ErpLine]],
    issues: list[ErpValidationIssue],
) -> ErpDocument | None:
    doc_number_col = "vendor cn no" if is_credit_note else "vendor inv. no"
    reference_col = "original invoice" if is_credit_note else "po no"

    entry_id = _cell_to_clean_str(worksheet.cell(row_index, header_map["entry id"]).value)
    vendor = _cell_to_clean_str(worksheet.cell(row_index, header_map["vendor"]).value) or ""
    doc_number_raw = _cell_to_clean_str(worksheet.cell(row_index, header_map[doc_number_col]).value)
    reference_raw = _cell_to_clean_str(worksheet.cell(row_index, header_map[reference_col]).value)
    currency_raw = _cell_to_clean_str(worksheet.cell(row_index, header_map["curr"]).value)
    doc_status = _cell_to_clean_str(worksheet.cell(row_index, header_map["doc status"]).value) or ""
    posted_by = (
        _cell_to_clean_str(worksheet.cell(row_index, header_map["posted by"]).value)
        if "posted by" in header_map
        else None
    )

    row_is_valid = _validate_header_identity_cells(
        worksheet,
        row_index,
        entry_id,
        doc_number_raw,
        doc_number_col,
        reference_raw,
        reference_col,
        currency_raw,
        seen_entry_ids,
        issues,
    )
    raw_doc_date = worksheet.cell(row_index, header_map["doc date"]).value
    doc_date = _parse_erp_date_cell(raw_doc_date)
    if doc_date is None:
        _record_validation_issue(issues, worksheet.title, row_index, "Doc Date", "cannot parse date", raw_doc_date)
        row_is_valid = False

    subtotal = _require_amount_cell(
        worksheet.cell(row_index, header_map["subtotal"]).value,
        worksheet.title,
        row_index,
        "Subtotal",
        "cannot parse as IDR amount",
        issues,
    )
    dpp = _require_amount_cell(
        worksheet.cell(row_index, header_map["dpp"]).value,
        worksheet.title,
        row_index,
        "DPP",
        "cannot parse as IDR amount",
        issues,
    )
    ppn = _require_amount_cell(
        worksheet.cell(row_index, header_map["ppn"]).value,
        worksheet.title,
        row_index,
        "PPN",
        "cannot parse as IDR amount",
        issues,
    )
    grand_total = _require_amount_cell(
        worksheet.cell(row_index, header_map["grand total"]).value,
        worksheet.title,
        row_index,
        "Grand Total",
        "cannot parse as IDR amount",
        issues,
    )

    if (
        not row_is_valid
        or entry_id is None
        or doc_number_raw is None
        or doc_date is None
        or subtotal is None
        or dpp is None
        or ppn is None
        or grand_total is None
    ):
        return None

    doc_number_norm = normalize_key(doc_number_raw)
    assert doc_number_norm is not None

    # Normalize ERP credit note sign to negative even if an export stores positive numbers.
    return ErpDocument(
        entry_id=entry_id,
        sheet=worksheet.title,
        row=row_index,
        document_type="credit_note" if is_credit_note else "invoice",
        vendor=vendor,
        doc_number_raw=doc_number_raw,
        doc_number_norm=doc_number_norm,
        doc_date=doc_date,
        po_number_raw=None if is_credit_note else reference_raw,
        po_number_norm=None if is_credit_note else normalize_key(reference_raw),
        original_invoice_raw=reference_raw if is_credit_note else None,
        original_invoice_norm=normalize_key(reference_raw) if is_credit_note else None,
        currency=normalize_currency(currency_raw),
        subtotal_signed=-abs(subtotal) if is_credit_note else subtotal,
        dpp_signed=-abs(dpp) if is_credit_note else dpp,
        ppn_signed=-abs(ppn) if is_credit_note else ppn,
        grand_total_signed=-abs(grand_total) if is_credit_note else grand_total,
        doc_status=doc_status,
        posted_by=posted_by,
        lines=lines_by_entry_id.get(entry_id, []),
    )


def _load_erp_header_sheet(
    worksheet: Worksheet,
    header_map: dict[str, int],
    is_credit_note: bool,
    seen_entry_ids: set[str],
    lines_by_entry_id: dict[str, list[ErpLine]],
    issues: list[ErpValidationIssue],
) -> list[ErpDocument]:
    documents: list[ErpDocument] = []
    for row_index in range(2, worksheet.max_row + 1):
        if _is_empty_worksheet_row(worksheet, row_index, worksheet.max_column):
            continue
        parsed_doc = _parse_erp_header_row(
            worksheet, row_index, header_map, is_credit_note, seen_entry_ids, lines_by_entry_id, issues
        )
        if parsed_doc is not None:
            documents.append(parsed_doc)
    return documents


def _read_export_metadata_sheet(workbook: openpyxl.Workbook) -> dict[str, str]:
    export_info: dict[str, str] = {}
    if "Export_Info" not in workbook.sheetnames:
        return export_info
    info_sheet = workbook["Export_Info"]
    for row_index in range(1, info_sheet.max_row + 1):
        key = _cell_to_clean_str(info_sheet.cell(row_index, 1).value)
        value = _cell_to_clean_str(info_sheet.cell(row_index, 2).value)
        if key and value is not None:
            export_info[key] = value
    return export_info


def _open_erp_workbook(source: Path | str | bytes) -> openpyxl.Workbook:
    if isinstance(source, bytes):
        if not source:
            raise ValueError("ERP workbook byte payload is empty")
        return openpyxl.load_workbook(BytesIO(source), data_only=True)
    workbook_path = Path(source)
    if not workbook_path.is_file():
        raise FileNotFoundError(f"ERP workbook not found: {workbook_path}")
    return openpyxl.load_workbook(workbook_path, data_only=True)


def load_erp_workbook(source: Path | str | bytes) -> ErpWorkbook:
    """Load and strictly validate all sheets of an ERP AP Excel export before spending LLM tokens."""
    workbook = _open_erp_workbook(source)
    issues: list[ErpValidationIssue] = []

    for required_sheet in ("AP_Invoices", "AP_Lines"):
        if required_sheet not in workbook.sheetnames:
            issues.append(
                ErpValidationIssue(
                    sheet=required_sheet,
                    message=f"required sheet '{required_sheet}' not found (sheets present: {workbook.sheetnames})",
                )
            )
    if issues:
        raise ErpValidationError(issues)

    invoice_sheet = workbook["AP_Invoices"]
    lines_sheet = workbook["AP_Lines"]
    invoice_header_map = _map_required_headers(invoice_sheet, _INVOICE_REQUIRED_COLUMNS, issues)
    lines_header_map = _map_required_headers(lines_sheet, _LINE_REQUIRED_COLUMNS, issues)

    credit_note_header_map: dict[str, int] = {}
    has_credit_note_sheet = "AP_CreditNotes" in workbook.sheetnames
    if has_credit_note_sheet:
        credit_note_header_map = _map_required_headers(
            workbook["AP_CreditNotes"], _CREDIT_NOTE_REQUIRED_COLUMNS, issues
        )
    if issues:
        raise ErpValidationError(issues)

    lines_by_entry_id = _load_all_erp_lines(lines_sheet, lines_header_map, issues)
    seen_entry_ids: set[str] = set()
    documents = _load_erp_header_sheet(
        invoice_sheet, invoice_header_map, False, seen_entry_ids, lines_by_entry_id, issues
    )
    if has_credit_note_sheet:
        documents.extend(
            _load_erp_header_sheet(
                workbook["AP_CreditNotes"],
                credit_note_header_map,
                True,
                seen_entry_ids,
                lines_by_entry_id,
                issues,
            )
        )

    known_entry_ids = {doc.entry_id for doc in documents}
    for entry_id, orphan_lines in lines_by_entry_id.items():
        if entry_id not in known_entry_ids:
            _record_validation_issue(
                issues,
                "AP_Lines",
                orphan_lines[0].row,
                "Entry ID",
                "line Entry ID does not match any header in AP_Invoices or AP_CreditNotes",
                entry_id,
            )

    if issues:
        raise ErpValidationError(issues)

    return ErpWorkbook(documents=documents, export_info=_read_export_metadata_sheet(workbook))


class ErpService:
    """Service wrapper exposing strict ERP Excel loading and contract validation."""

    def load_and_validate_workbook(self, workbook_source: Path | str | bytes) -> ErpWorkbook:
        """Load and validate an ERP AP Excel export, raising ErpValidationError on any schema issue."""
        return load_erp_workbook(workbook_source)
