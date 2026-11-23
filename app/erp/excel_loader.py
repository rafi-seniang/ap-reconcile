"""Strict upstream ERP workbook loader that collects all schema and cell errors before reconciling."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.worksheet.worksheet import Worksheet

from app.erp.schemas import ErpDocument, ErpLine, ErpValidationError, ErpValidationIssue, ErpWorkbook
from app.extraction.parser import (
    normalize_currency,
    normalize_key,
    parse_date_flexible,
    parse_decimal_amount,
    parse_discount_pct,
)

_EXCEL_ERRORS = frozenset({"#REF!", "#N/A", "#VALUE!", "#DIV/0!", "#NAME?", "#NUM!", "#NULL!"})

_INVOICE_REQUIRED_COLS = (
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

_CREDIT_NOTE_REQUIRED_COLS = (
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

_LINES_REQUIRED_COLS = (
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


def _norm_header(value: Any) -> str:
    return " ".join(str(value).strip().lower().split()) if value is not None else ""


def _map_headers(
    ws: Worksheet, required: tuple[str, ...], issues: list[ErpValidationIssue]
) -> dict[str, int]:
    header_map: dict[str, int] = {}
    for col_idx in range(1, ws.max_column + 1):
        name = _norm_header(ws.cell(1, col_idx).value)
        if name:
            header_map[name] = col_idx
    for req in required:
        if req not in header_map:
            issues.append(
                ErpValidationIssue(
                    sheet=ws.title,
                    row=1,
                    column=req,
                    message=f"missing required column '{req}' (found: {sorted(header_map.keys())})",
                )
            )
    return header_map


def _is_empty_row(ws: Worksheet, row_idx: int, max_col: int) -> bool:
    return all(ws.cell(row_idx, col).value is None for col in range(1, max_col + 1))


def _cell_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_excel_error(text: str | None) -> bool:
    return text is not None and text.strip().upper() in _EXCEL_ERRORS


def _parse_erp_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _cell_str(value)
    if text is None or _is_excel_error(text):
        return None
    # ERP string exports use MM/DD/YYYY when slash-delimited and YYYY-MM-DD when dashed.
    return parse_date_flexible(text, prefer_month_first="/" in text)


def _parse_erp_amount(value: Any) -> Decimal | None:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value))
    text = _cell_str(value)
    if _is_excel_error(text):
        return None
    return parse_decimal_amount(text)


def _parse_erp_discount(value: Any) -> Decimal | None:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return parse_discount_pct(value)
    return parse_discount_pct(_cell_str(value))


def _record_issue(
    issues: list[ErpValidationIssue],
    sheet: str,
    row: int,
    column: str,
    message: str,
    raw_value: Any = None,
) -> None:
    issues.append(
        ErpValidationIssue(
            sheet=sheet,
            row=row,
            column=column,
            raw_value=str(raw_value) if raw_value is not None else None,
            message=message,
        )
    )


def _parse_line_number(
    raw_line: Any, sheet: str, row_idx: int, issues: list[ErpValidationIssue]
) -> int | None:
    if isinstance(raw_line, int) and not isinstance(raw_line, bool):
        return raw_line
    if raw_line is not None and str(raw_line).strip().isdigit():
        return int(str(raw_line).strip())
    _record_issue(issues, sheet, row_idx, "Line", "line number must be an integer", raw_line)
    return None


def _require_cell_amount(
    raw_value: Any,
    sheet: str,
    row_idx: int,
    column: str,
    message: str,
    issues: list[ErpValidationIssue],
) -> Decimal | None:
    parsed = _parse_erp_amount(raw_value)
    if parsed is None:
        _record_issue(issues, sheet, row_idx, column, message, raw_value)
    return parsed


def _parse_line_row(
    ws: Worksheet,
    row_idx: int,
    header_map: dict[str, int],
    issues: list[ErpValidationIssue],
) -> ErpLine | None:
    entry_id = _cell_str(ws.cell(row_idx, header_map["entry id"]).value)
    item_code = _cell_str(ws.cell(row_idx, header_map["item code"]).value)
    desc = _cell_str(ws.cell(row_idx, header_map["item description"]).value) or ""
    uom = _cell_str(ws.cell(row_idx, header_map["uom"]).value)

    row_ok = True
    if not entry_id or _is_excel_error(entry_id):
        _record_issue(issues, ws.title, row_idx, "Entry ID", "missing or invalid Entry ID", entry_id)
        row_ok = False
    if _is_excel_error(item_code):
        _record_issue(issues, ws.title, row_idx, "Item Code", "Excel formula error in Item Code", item_code)
        row_ok = False

    line_number = _parse_line_number(ws.cell(row_idx, header_map["line"]).value, ws.title, row_idx, issues)
    qty = _require_cell_amount(
        ws.cell(row_idx, header_map["qty"]).value,
        ws.title,
        row_idx,
        "Qty",
        "cannot parse quantity as number",
        issues,
    )
    unit_price = _require_cell_amount(
        ws.cell(row_idx, header_map["unit price"]).value,
        ws.title,
        row_idx,
        "Unit Price",
        "cannot parse unit price as IDR amount",
        issues,
    )
    disc = _parse_erp_discount(ws.cell(row_idx, header_map["disc %"]).value)
    if disc is None:
        _record_issue(
            issues,
            ws.title,
            row_idx,
            "Disc %",
            "cannot parse discount (expected 0 to 100)",
            ws.cell(row_idx, header_map["disc %"]).value,
        )
    line_amount = _require_cell_amount(
        ws.cell(row_idx, header_map["line amount"]).value,
        ws.title,
        row_idx,
        "Line Amount",
        "cannot parse line amount as IDR amount",
        issues,
    )

    if (
        not row_ok
        or entry_id is None
        or line_number is None
        or qty is None
        or unit_price is None
        or disc is None
        or line_amount is None
    ):
        return None
    return ErpLine(
        entry_id=entry_id,
        sheet=ws.title,
        row=row_idx,
        line_number=line_number,
        item_code_raw=item_code,
        item_code_norm=normalize_key(item_code),
        description=desc,
        quantity_signed=qty,
        quantity_abs=abs(qty),
        uom_raw=uom,
        unit_price=abs(unit_price),
        discount_pct=disc,
        line_amount_signed=line_amount,
        line_amount_abs=abs(line_amount),
    )


def _load_lines(
    ws: Worksheet,
    header_map: dict[str, int],
    issues: list[ErpValidationIssue],
) -> dict[str, list[ErpLine]]:
    lines_by_entry: dict[str, list[ErpLine]] = {}
    for row_idx in range(2, ws.max_row + 1):
        if _is_empty_row(ws, row_idx, ws.max_column):
            continue
        parsed = _parse_line_row(ws, row_idx, header_map, issues)
        if parsed is not None:
            lines_by_entry.setdefault(parsed.entry_id, []).append(parsed)
    return lines_by_entry


def _validate_header_identity(
    ws: Worksheet,
    row_idx: int,
    entry_id: str | None,
    doc_no_raw: str | None,
    doc_no_col: str,
    ref_raw: str | None,
    ref_col: str,
    curr_raw: str | None,
    seen_entry_ids: set[str],
    issues: list[ErpValidationIssue],
) -> bool:
    row_ok = True
    if not entry_id or _is_excel_error(entry_id):
        _record_issue(issues, ws.title, row_idx, "Entry ID", "missing or invalid Entry ID", entry_id)
        row_ok = False
    elif entry_id in seen_entry_ids:
        _record_issue(issues, ws.title, row_idx, "Entry ID", f"duplicate Entry ID '{entry_id}'", entry_id)
        row_ok = False
    else:
        seen_entry_ids.add(entry_id)

    if not doc_no_raw or _is_excel_error(doc_no_raw) or not normalize_key(doc_no_raw):
        _record_issue(issues, ws.title, row_idx, doc_no_col, "missing or invalid document number", doc_no_raw)
        row_ok = False
    if _is_excel_error(ref_raw):
        _record_issue(issues, ws.title, row_idx, ref_col, "Excel formula error in reference column", ref_raw)
        row_ok = False
    if _is_excel_error(curr_raw):
        _record_issue(issues, ws.title, row_idx, "Curr", "Excel formula error in currency", curr_raw)
        row_ok = False
    return row_ok


def _parse_header_row(
    ws: Worksheet,
    row_idx: int,
    header_map: dict[str, int],
    is_credit_note: bool,
    seen_entry_ids: set[str],
    lines_by_entry: dict[str, list[ErpLine]],
    issues: list[ErpValidationIssue],
) -> ErpDocument | None:
    doc_no_col = "vendor cn no" if is_credit_note else "vendor inv. no"
    ref_col = "original invoice" if is_credit_note else "po no"

    entry_id = _cell_str(ws.cell(row_idx, header_map["entry id"]).value)
    vendor = _cell_str(ws.cell(row_idx, header_map["vendor"]).value) or ""
    doc_no_raw = _cell_str(ws.cell(row_idx, header_map[doc_no_col]).value)
    ref_raw = _cell_str(ws.cell(row_idx, header_map[ref_col]).value)
    curr_raw = _cell_str(ws.cell(row_idx, header_map["curr"]).value)
    status = _cell_str(ws.cell(row_idx, header_map["doc status"]).value) or ""
    posted_by = (
        _cell_str(ws.cell(row_idx, header_map["posted by"]).value) if "posted by" in header_map else None
    )

    row_ok = _validate_header_identity(
        ws, row_idx, entry_id, doc_no_raw, doc_no_col, ref_raw, ref_col, curr_raw, seen_entry_ids, issues
    )
    raw_date = ws.cell(row_idx, header_map["doc date"]).value
    doc_date = _parse_erp_date(raw_date)
    if doc_date is None:
        _record_issue(issues, ws.title, row_idx, "Doc Date", "cannot parse date", raw_date)
        row_ok = False

    sub = _require_cell_amount(
        ws.cell(row_idx, header_map["subtotal"]).value,
        ws.title,
        row_idx,
        "Subtotal",
        "cannot parse as IDR amount",
        issues,
    )
    dpp = _require_cell_amount(
        ws.cell(row_idx, header_map["dpp"]).value,
        ws.title,
        row_idx,
        "DPP",
        "cannot parse as IDR amount",
        issues,
    )
    ppn = _require_cell_amount(
        ws.cell(row_idx, header_map["ppn"]).value,
        ws.title,
        row_idx,
        "PPN",
        "cannot parse as IDR amount",
        issues,
    )
    gt = _require_cell_amount(
        ws.cell(row_idx, header_map["grand total"]).value,
        ws.title,
        row_idx,
        "Grand Total",
        "cannot parse as IDR amount",
        issues,
    )

    if (
        not row_ok
        or entry_id is None
        or doc_no_raw is None
        or doc_date is None
        or sub is None
        or dpp is None
        or ppn is None
        or gt is None
    ):
        return None

    doc_no_norm = normalize_key(doc_no_raw)
    assert doc_no_norm is not None

    # Normalize ERP credit note sign to negative even if an export stores positive amounts.
    return ErpDocument(
        entry_id=entry_id,
        sheet=ws.title,
        row=row_idx,
        document_type="credit_note" if is_credit_note else "invoice",
        vendor=vendor,
        doc_number_raw=doc_no_raw,
        doc_number_norm=doc_no_norm,
        doc_date=doc_date,
        po_number_raw=None if is_credit_note else ref_raw,
        po_number_norm=None if is_credit_note else normalize_key(ref_raw),
        original_invoice_raw=ref_raw if is_credit_note else None,
        original_invoice_norm=normalize_key(ref_raw) if is_credit_note else None,
        currency=normalize_currency(curr_raw),
        subtotal_signed=-abs(sub) if is_credit_note else sub,
        dpp_signed=-abs(dpp) if is_credit_note else dpp,
        ppn_signed=-abs(ppn) if is_credit_note else ppn,
        grand_total_signed=-abs(gt) if is_credit_note else gt,
        doc_status=status,
        posted_by=posted_by,
        lines=lines_by_entry.get(entry_id, []),
    )


def _load_headers(
    ws: Worksheet,
    header_map: dict[str, int],
    is_credit_note: bool,
    seen_entry_ids: set[str],
    lines_by_entry: dict[str, list[ErpLine]],
    issues: list[ErpValidationIssue],
) -> list[ErpDocument]:
    docs: list[ErpDocument] = []
    for row_idx in range(2, ws.max_row + 1):
        if _is_empty_row(ws, row_idx, ws.max_column):
            continue
        parsed = _parse_header_row(
            ws, row_idx, header_map, is_credit_note, seen_entry_ids, lines_by_entry, issues
        )
        if parsed is not None:
            docs.append(parsed)
    return docs


def _read_export_info(wb: openpyxl.Workbook) -> dict[str, str]:
    export_info: dict[str, str] = {}
    if "Export_Info" not in wb.sheetnames:
        return export_info
    ws_info = wb["Export_Info"]
    for row_idx in range(1, ws_info.max_row + 1):
        key = _cell_str(ws_info.cell(row_idx, 1).value)
        val = _cell_str(ws_info.cell(row_idx, 2).value)
        if key and val is not None:
            export_info[key] = val
    return export_info


def load_erp_workbook(path: Path | str) -> ErpWorkbook:
    """Load and strictly validate all sheets of an ERP AP Excel export."""
    workbook_path = Path(path)
    if not workbook_path.is_file():
        raise FileNotFoundError(f"ERP workbook not found: {workbook_path}")

    wb = openpyxl.load_workbook(workbook_path, data_only=True)
    issues: list[ErpValidationIssue] = []

    for required_sheet in ("AP_Invoices", "AP_Lines"):
        if required_sheet not in wb.sheetnames:
            issues.append(
                ErpValidationIssue(
                    sheet=required_sheet,
                    message=f"required sheet '{required_sheet}' not found (sheets present: {wb.sheetnames})",
                )
            )
    if issues:
        raise ErpValidationError(issues)

    ws_inv = wb["AP_Invoices"]
    ws_lines = wb["AP_Lines"]
    inv_map = _map_headers(ws_inv, _INVOICE_REQUIRED_COLS, issues)
    lines_map = _map_headers(ws_lines, _LINES_REQUIRED_COLS, issues)

    cn_map: dict[str, int] = {}
    has_cn_sheet = "AP_CreditNotes" in wb.sheetnames
    if has_cn_sheet:
        cn_map = _map_headers(wb["AP_CreditNotes"], _CREDIT_NOTE_REQUIRED_COLS, issues)
    if issues:
        raise ErpValidationError(issues)

    lines_by_entry = _load_lines(ws_lines, lines_map, issues)
    seen_entry_ids: set[str] = set()
    documents = _load_headers(ws_inv, inv_map, False, seen_entry_ids, lines_by_entry, issues)
    if has_cn_sheet:
        documents.extend(
            _load_headers(wb["AP_CreditNotes"], cn_map, True, seen_entry_ids, lines_by_entry, issues)
        )

    known_entry_ids = {doc.entry_id for doc in documents}
    for entry_id, orphan_lines in lines_by_entry.items():
        if entry_id not in known_entry_ids:
            _record_issue(
                issues,
                "AP_Lines",
                orphan_lines[0].row,
                "Entry ID",
                "line Entry ID does not match any header in AP_Invoices or AP_CreditNotes",
                entry_id,
            )

    if issues:
        raise ErpValidationError(issues)

    return ErpWorkbook(documents=documents, export_info=_read_export_info(wb))
