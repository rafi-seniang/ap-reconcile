"""Deterministic parsers for Indonesian and English printed amounts, percentages, dates, and keys."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.extraction.schemas import AssembledDocument, CheckResult, ParsedDocument, ParsedLine

_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_GROUPED_INT = re.compile(r"^\d{1,3}([.,])\d{3}(?:\1\d{3})*$")
_GROUPED_DEC = re.compile(r"^(\d+|\d{1,3}([.,])\d{3}(?:\2\d{3})*)([.,])(\d{1,2})$")
_CURRENCY_TOKENS = ("RP.", "RP", "IDR", "USD", "SGD", "EUR", "$")

_ID_TO_EN_MONTH: dict[str, str] = {
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

_DMY_FIRST_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%m/%d/%Y", "%m-%d-%Y")
_MDY_FIRST_FORMATS = ("%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")


def normalize_key(raw: str | None) -> str | None:
    """Strip punctuation and case solely for exact-match keys while preserving the raw string alongside."""
    if raw is None:
        return None
    cleaned = _NON_ALNUM.sub("", raw.strip().upper())
    return cleaned or None


def normalize_currency(raw: str | None) -> str:
    """Map Rp variants and blank values to IDR while keeping other ISO currency codes uppercase."""
    if raw is None:
        return "IDR"
    cleaned = raw.strip().rstrip(".").upper()
    if not cleaned or cleaned in ("RP", "IDR"):
        return "IDR"
    return cleaned


def _strip_sign_and_currency(text: str) -> tuple[bool, str] | None:
    work = text.strip()
    if not work:
        return None

    starts_paren = work.startswith("(")
    ends_paren = work.endswith(")")
    if starts_paren != ends_paren:
        return None

    is_negative = False
    if starts_paren and ends_paren:
        is_negative = True
        work = work[1:-1].strip()
    elif work.startswith("-"):
        is_negative = True
        work = work[1:].strip()

    if work.endswith(",-"):
        work = work[:-2].strip()

    upper = work.upper()
    for token in _CURRENCY_TOKENS:
        if upper.startswith(token):
            work = work[len(token) :].strip()
            break
    upper = work.upper()
    for token in _CURRENCY_TOKENS:
        if upper.endswith(token):
            work = work[: -len(token)].strip()
            break

    return (is_negative, work) if work else None


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
    parsed: Decimal | None = None

    if digits.isdigit():
        parsed = Decimal(digits)
    elif _GROUPED_INT.fullmatch(digits):
        parsed = Decimal(digits.replace(".", "").replace(",", ""))
    else:
        dec_match = _GROUPED_DEC.fullmatch(digits)
        # Reject ambiguous strings where grouping mark and decimal mark are the same character.
        if dec_match and dec_match.group(2) != dec_match.group(3):
            integer_part = dec_match.group(1).replace(".", "").replace(",", "")
            try:
                parsed = Decimal(f"{integer_part}.{dec_match.group(4)}")
            except InvalidOperation:
                return None

    if parsed is None:
        return None
    return -parsed if is_negative else parsed


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
    normalized: list[str] = []
    for tok in tokens:
        lower_clean = tok.rstrip(".").lower()
        normalized.append(_ID_TO_EN_MONTH.get(lower_clean, tok))
    return " ".join(normalized)


def parse_date_flexible(raw: str | date | datetime | None, prefer_month_first: bool = False) -> date | None:
    """Parse ISO, DD-MM-YYYY, MM/DD/YYYY, and Indonesian/English named-month dates via stdlib strptime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    text = " ".join(raw.strip().split())
    if not text:
        return None
    candidate = _normalize_indonesian_months(text)
    numeric_formats = _MDY_FIRST_FORMATS if prefer_month_first else _DMY_FIRST_FORMATS

    for fmt in (*_ISO_AND_NAMED_DATE_FORMATS, *numeric_formats):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def _parse_assembled_line(idx: int, item_page: int, printed: Any, checks: list[CheckResult]) -> ParsedLine:
    """Parse one extracted line item into typed Decimal fields while collecting per-field parse errors."""

    def req_line_num(suffix: str, raw: str | None) -> Decimal | None:
        if raw is None or not raw.strip():
            checks.append(
                CheckResult(name=f"parse_line_{idx}_{suffix}", passed=False, detail="missing value")
            )
            return None
        val = parse_decimal_amount(raw)
        if val is None:
            checks.append(
                CheckResult(
                    name=f"parse_line_{idx}_{suffix}",
                    passed=False,
                    detail=f"cannot parse {raw!r} as amount",
                )
            )
        return val

    discount = parse_discount_pct(printed.discount_pct_raw)
    if discount is None:
        checks.append(
            CheckResult(
                name=f"parse_line_{idx}_discount",
                passed=False,
                detail=f"cannot parse {printed.discount_pct_raw!r} as discount %",
            )
        )
    line_num = (
        int(printed.line_number_raw.strip())
        if printed.line_number_raw and printed.line_number_raw.strip().isdigit()
        else idx
    )
    return ParsedLine(
        source_page=item_page,
        line_number=line_num,
        item_code_raw=printed.item_code.strip() if printed.item_code else None,
        item_code_norm=normalize_key(printed.item_code),
        description=printed.description.strip(),
        quantity=req_line_num("qty", printed.quantity_raw),
        uom_raw=printed.uom_raw.strip() if printed.uom_raw else None,
        unit_price=req_line_num("unit_price", printed.unit_price_raw),
        discount_pct=discount,
        line_amount=req_line_num("amount", printed.line_amount_raw),
    )


def _require_doc_amount(
    field_name: str, raw: str | None, checks: list[CheckResult], required: bool = True
) -> Decimal | None:
    if raw is None or not raw.strip():
        if required:
            checks.append(CheckResult(name=f"parse_{field_name}", passed=False, detail="missing value"))
        return None
    value = parse_decimal_amount(raw)
    if value is None:
        checks.append(
            CheckResult(name=f"parse_{field_name}", passed=False, detail=f"cannot parse {raw!r} as amount")
        )
    return value


def _parse_optional_doc_date(field_name: str, raw: str | None, checks: list[CheckResult]) -> date | None:
    if raw is None or not raw.strip():
        return None
    value = parse_date_flexible(raw, prefer_month_first=False)
    if value is None:
        checks.append(
            CheckResult(name=f"parse_{field_name}", passed=False, detail=f"cannot parse {raw!r} as date")
        )
    return value


def parse_assembled_document(assembled: AssembledDocument) -> ParsedDocument:
    """Convert verbatim strings to typed Decimal/date values, recording every parse failure explicitly."""
    checks: list[CheckResult] = []
    parsed_lines = [
        _parse_assembled_line(idx, item.source_page, item.printed, checks)
        for idx, item in enumerate(assembled.lines, start=1)
    ]

    return ParsedDocument(
        pages=assembled.pages,
        document_type=assembled.document_type,
        supplier_name=assembled.supplier_name.strip() if assembled.supplier_name else None,
        doc_number_raw=assembled.doc_number_raw.strip() if assembled.doc_number_raw else None,
        doc_number_norm=normalize_key(assembled.doc_number_raw),
        doc_date=_parse_optional_doc_date("doc_date", assembled.doc_date_raw, checks),
        due_date=_parse_optional_doc_date("due_date", assembled.due_date_raw, checks),
        po_number_raw=assembled.po_number_raw.strip() if assembled.po_number_raw else None,
        po_number_norm=normalize_key(assembled.po_number_raw),
        reference_doc_raw=assembled.reference_doc_raw.strip() if assembled.reference_doc_raw else None,
        reference_doc_norm=normalize_key(assembled.reference_doc_raw),
        currency=normalize_currency(assembled.currency),
        subtotal=_require_doc_amount("subtotal", assembled.subtotal_raw, checks, required=True),
        dpp=_require_doc_amount("dpp", assembled.dpp_raw, checks, required=False),
        ppn=_require_doc_amount("ppn", assembled.ppn_raw, checks, required=True),
        grand_total=_require_doc_amount("grand_total", assembled.grand_total_raw, checks, required=True),
        lines=parsed_lines,
        checks=checks,
    )
