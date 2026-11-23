"""Deterministic parsers for Indonesian and English printed amounts, percentages, dates, and keys."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.schemas.invoice import AssembledDocument, CheckResult, ParsedDocument, ParsedLine

_NON_ALNUM_PATTERN = re.compile(r"[^A-Z0-9]")
_GROUPED_INT_PATTERN = re.compile(r"^\d{1,3}([.,])\d{3}(?:\1\d{3})*$")
_GROUPED_DEC_PATTERN = re.compile(r"^(\d+|\d{1,3}([.,])\d{3}(?:\2\d{3})*)([.,])(\d{1,2})$")
_CURRENCY_TOKENS = ("RP.", "RP", "IDR", "USD", "SGD", "EUR", "$")

_INDONESIAN_TO_ENGLISH_MONTH: dict[str, str] = {
    "januari": "January",
    "februari": "February",
    "pebruari": "February",
    "maret": "March",
    "mei": "May",
    "juni": "June",
    "juli": "July",
    "agustus": "August",
    "agu": "Aug",
    "agt": "Aug",
    "ags": "Aug",
    "oktober": "October",
    "okt": "Oct",
    "nop": "Nov",
    "desember": "December",
    "des": "Dec",
}

_ISO_AND_NAMED_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
)

_DAY_FIRST_NUMERIC_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%m/%d/%Y", "%m-%d-%Y")
_MONTH_FIRST_NUMERIC_FORMATS = ("%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")


def normalize_key(raw: str | None) -> str | None:
    """Strip punctuation and case solely for exact-match keys while preserving the raw string alongside."""
    if raw is None:
        return None
    cleaned = _NON_ALNUM_PATTERN.sub("", raw.strip().upper())
    return cleaned or None


def normalize_currency(raw: str | None) -> str:
    """Map Rp variants and blank values to IDR while keeping other ISO currency codes uppercase."""
    if raw is None:
        return "IDR"
    cleaned = raw.strip().rstrip(".").upper()
    if not cleaned or cleaned in ("RP", "IDR"):
        return "IDR"
    return cleaned


def _strip_sign_and_currency(raw_text: str) -> tuple[bool, str] | None:
    working = raw_text.strip()
    if not working:
        return None

    starts_paren = working.startswith("(")
    ends_paren = working.endswith(")")
    if starts_paren != ends_paren:
        return None

    is_negative = False
    if starts_paren and ends_paren:
        is_negative = True
        working = working[1:-1].strip()
    elif working.startswith("-"):
        is_negative = True
        working = working[1:].strip()

    if working.endswith(",-"):
        working = working[:-2].strip()

    upper_text = working.upper()
    for token in _CURRENCY_TOKENS:
        if upper_text.startswith(token):
            working = working[len(token) :].strip()
            break
    upper_text = working.upper()
    for token in _CURRENCY_TOKENS:
        if upper_text.endswith(token):
            working = working[: -len(token)].strip()
            break

    return (is_negative, working) if working else None


def parse_decimal_amount(raw: str | int | float | Decimal | None) -> Decimal | None:
    """Parse IDR/English numeric strings ('1.238.490', '15,619,088', '2,5', '-972360') to exact Decimal."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(str(raw))

    stripped = _strip_sign_and_currency(raw)
    if stripped is None:
        return None
    is_negative, digits = stripped
    parsed_value: Decimal | None = None

    if digits.isdigit():
        parsed_value = Decimal(digits)
    elif _GROUPED_INT_PATTERN.fullmatch(digits):
        parsed_value = Decimal(digits.replace(".", "").replace(",", ""))
    else:
        decimal_match = _GROUPED_DEC_PATTERN.fullmatch(digits)
        # Reject ambiguous strings where grouping mark and decimal mark are identical.
        if decimal_match and decimal_match.group(2) != decimal_match.group(3):
            integer_part = decimal_match.group(1).replace(".", "").replace(",", "")
            try:
                parsed_value = Decimal(f"{integer_part}.{decimal_match.group(4)}")
            except InvalidOperation:
                return None

    if parsed_value is None:
        return None
    return -parsed_value if is_negative else parsed_value


def parse_discount_pct(raw: str | int | float | Decimal | None) -> Decimal | None:
    """Parse discount percentage; blank or '-' means 0%, invalid text returns None for an explicit check failure."""
    if raw is None:
        return Decimal("0")
    if isinstance(raw, str):
        cleaned = raw.strip().rstrip("%").strip()
        if cleaned in ("", "-", "—", "–"):
            return Decimal("0")
        value = parse_decimal_amount(cleaned)
    else:
        value = parse_decimal_amount(raw)
    if value is None or value < Decimal("0") or value > Decimal("100"):
        return None
    return value


def _normalize_indonesian_months(text: str) -> str:
    tokens = text.split()
    return " ".join(_INDONESIAN_TO_ENGLISH_MONTH.get(token.rstrip(".").lower(), token) for token in tokens)


def parse_date_flexible(raw: str | date | datetime | None, prefer_month_first: bool = False) -> date | None:
    """Parse ISO, DD-MM-YYYY, MM/DD/YYYY, and Indonesian/English named-month dates via stdlib strptime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    cleaned_text = " ".join(raw.strip().split())
    if not cleaned_text:
        return None
    candidate = _normalize_indonesian_months(cleaned_text)
    numeric_formats = _MONTH_FIRST_NUMERIC_FORMATS if prefer_month_first else _DAY_FIRST_NUMERIC_FORMATS

    for date_format in (*_ISO_AND_NAMED_DATE_FORMATS, *numeric_formats):
        try:
            return datetime.strptime(candidate, date_format).date()
        except ValueError:
            continue
    return None


def _parse_assembled_line(
    line_index: int, source_page: int, printed_line: Any, checks: list[CheckResult]
) -> ParsedLine:
    """Parse one extracted line item into typed Decimal fields while collecting per-field parse errors."""

    def parse_required_line_number(suffix: str, raw_value: str | None) -> Decimal | None:
        if raw_value is None or not raw_value.strip():
            checks.append(
                CheckResult(name=f"parse_line_{line_index}_{suffix}", passed=False, detail="missing value")
            )
            return None
        parsed_number = parse_decimal_amount(raw_value)
        if parsed_number is None:
            checks.append(
                CheckResult(
                    name=f"parse_line_{line_index}_{suffix}",
                    passed=False,
                    detail=f"cannot parse {raw_value!r} as amount",
                )
            )
        return parsed_number

    discount_pct = parse_discount_pct(printed_line.discount_pct_raw)
    if discount_pct is None:
        checks.append(
            CheckResult(
                name=f"parse_line_{line_index}_discount",
                passed=False,
                detail=f"cannot parse {printed_line.discount_pct_raw!r} as discount %",
            )
        )
    line_number = (
        int(printed_line.line_number_raw.strip())
        if printed_line.line_number_raw and printed_line.line_number_raw.strip().isdigit()
        else line_index
    )
    return ParsedLine(
        source_page=source_page,
        line_number=line_number,
        item_code_raw=printed_line.item_code.strip() if printed_line.item_code else None,
        item_code_norm=normalize_key(printed_line.item_code),
        description=printed_line.description.strip(),
        quantity=parse_required_line_number("qty", printed_line.quantity_raw),
        uom_raw=printed_line.uom_raw.strip() if printed_line.uom_raw else None,
        unit_price=parse_required_line_number("unit_price", printed_line.unit_price_raw),
        discount_pct=discount_pct,
        line_amount=parse_required_line_number("amount", printed_line.line_amount_raw),
    )


def _require_document_amount(
    field_name: str, raw_value: str | None, checks: list[CheckResult], required: bool = True
) -> Decimal | None:
    if raw_value is None or not raw_value.strip():
        if required:
            checks.append(CheckResult(name=f"parse_{field_name}", passed=False, detail="missing value"))
        return None
    parsed_amount = parse_decimal_amount(raw_value)
    if parsed_amount is None:
        checks.append(
            CheckResult(
                name=f"parse_{field_name}",
                passed=False,
                detail=f"cannot parse {raw_value!r} as amount",
            )
        )
    return parsed_amount


def _parse_optional_document_date(
    field_name: str, raw_value: str | None, checks: list[CheckResult]
) -> date | None:
    if raw_value is None or not raw_value.strip():
        return None
    parsed_date = parse_date_flexible(raw_value, prefer_month_first=False)
    if parsed_date is None:
        checks.append(
            CheckResult(
                name=f"parse_{field_name}",
                passed=False,
                detail=f"cannot parse {raw_value!r} as date",
            )
        )
    return parsed_date


def parse_assembled_document(assembled: AssembledDocument) -> ParsedDocument:
    """Convert verbatim strings to typed Decimal/date values, recording every parse failure explicitly."""
    checks: list[CheckResult] = []
    parsed_lines = [
        _parse_assembled_line(index, line_item.source_page, line_item.printed, checks)
        for index, line_item in enumerate(assembled.lines, start=1)
    ]

    return ParsedDocument(
        pages=assembled.pages,
        document_type=assembled.document_type,
        supplier_name=assembled.supplier_name.strip() if assembled.supplier_name else None,
        doc_number_raw=assembled.doc_number_raw.strip() if assembled.doc_number_raw else None,
        doc_number_norm=normalize_key(assembled.doc_number_raw),
        doc_date=_parse_optional_document_date("doc_date", assembled.doc_date_raw, checks),
        due_date=_parse_optional_document_date("due_date", assembled.due_date_raw, checks),
        po_number_raw=assembled.po_number_raw.strip() if assembled.po_number_raw else None,
        po_number_norm=normalize_key(assembled.po_number_raw),
        reference_doc_raw=assembled.reference_doc_raw.strip() if assembled.reference_doc_raw else None,
        reference_doc_norm=normalize_key(assembled.reference_doc_raw),
        currency=normalize_currency(assembled.currency),
        subtotal=_require_document_amount("subtotal", assembled.subtotal_raw, checks, required=True),
        dpp=_require_document_amount("dpp", assembled.dpp_raw, checks, required=False),
        ppn=_require_document_amount("ppn", assembled.ppn_raw, checks, required=True),
        grand_total=_require_document_amount("grand_total", assembled.grand_total_raw, checks, required=True),
        lines=parsed_lines,
        checks=checks,
    )
