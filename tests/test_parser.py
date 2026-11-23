"""Unit tests for Indonesian/English number, discount, date, and key parsers."""

from datetime import date
from decimal import Decimal

import pytest

from app.extraction.parser import (
    normalize_currency,
    normalize_key,
    parse_assembled_document,
    parse_date_flexible,
    parse_decimal_amount,
    parse_discount_pct,
)
from app.extraction.schemas import AssembledDocument, AssembledLine, PrintedLine


def test_normalize_key_unifies_slashes_hyphens_and_case() -> None:
    assert normalize_key("PKI/INV/2026/09/0142") == "PKIINV2026090142"
    assert normalize_key("pki-inv-2026-09-0142 ") == "PKIINV2026090142"
    assert normalize_key("   ") is None
    assert normalize_key(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.238.490", Decimal("1238490")),
        ("15,619,088", Decimal("15619088")),
        ("Rp 17.651.608", Decimal("17651608")),
        ("Rp 972.360,-", Decimal("972360")),
        ("-972360", Decimal("-972360")),
        ("(972.360)", Decimal("-972360")),
        ("2,5", Decimal("2.5")),
        ("2.5", Decimal("2.5")),
        ("1.234,50", Decimal("1234.50")),
        ("1,234.50", Decimal("1234.50")),
        ("1.110.000 IDR", Decimal("1110000")),
        ("Rp. 972.360 ,-", Decimal("972360")),
        ("USD 1,250.00", Decimal("1250.00")),
        ("$972,360", Decimal("972360")),
        ("0", Decimal("0")),
        ("(1000", None),
        ("ABC", None),
        ("1.234.50", None),
        ("", None),
    ],
)
def test_parse_decimal_amount(raw: str, expected: Decimal | None) -> None:
    assert parse_decimal_amount(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, Decimal("0")),
        ("", Decimal("0")),
        ("-", Decimal("0")),
        ("0%", Decimal("0")),
        ("2,5%", Decimal("2.5")),
        ("10", Decimal("10")),
        ("-5", None),
        ("120", None),
        ("invalid", None),
    ],
)
def test_parse_discount_pct(raw: str | None, expected: Decimal | None) -> None:
    assert parse_discount_pct(raw) == expected


@pytest.mark.parametrize(
    ("raw", "prefer_month_first", "expected"),
    [
        ("2026-09-13", False, date(2026, 9, 13)),
        ("2026/09/13", False, date(2026, 9, 13)),
        ("09/23/2026", True, date(2026, 9, 23)),
        ("23-09-2026", False, date(2026, 9, 23)),
        ("14 September 2026", False, date(2026, 9, 14)),
        ("02 Okt 2026", False, date(2026, 10, 2)),
        ("November 4, 2026", False, date(2026, 11, 4)),
        ("Sep 14, 2026", False, date(2026, 9, 14)),
        ("2026-02-30", False, None),
        ("not-a-date", False, None),
    ],
)
def test_parse_date_flexible(raw: str, prefer_month_first: bool, expected: date | None) -> None:
    assert parse_date_flexible(raw, prefer_month_first=prefer_month_first) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "IDR"),
        ("", "IDR"),
        ("Rp", "IDR"),
        ("Rp.", "IDR"),
        ("idr", "IDR"),
        ("usd", "USD"),
    ],
)
def test_normalize_currency(raw: str | None, expected: str) -> None:
    assert normalize_currency(raw) == expected


def test_parse_assembled_document_records_explicit_parse_errors() -> None:
    assembled = AssembledDocument(
        pages=[1],
        document_type="invoice",
        supplier_name="Vendor X",
        doc_number_raw="INV-001",
        doc_date_raw="not-a-date",
        due_date_raw=None,
        po_number_raw="PO-1",
        reference_doc_raw=None,
        currency="IDR",
        subtotal_raw="1.000",
        dpp_raw=None,
        ppn_raw="BAD",
        grand_total_raw="1.110",
        lines=[
            AssembledLine(
                source_page=1,
                printed=PrintedLine(
                    line_number_raw="1",
                    item_code="SKU-1",
                    description="Widget",
                    quantity_raw="ten",
                    uom_raw="pcs",
                    unit_price_raw="100",
                    discount_pct_raw="150%",
                    line_amount_raw="1.000",
                ),
            )
        ],
    )
    parsed = parse_assembled_document(assembled)
    assert parsed.has_parse_errors is True
    failed_names = {c.name for c in parsed.checks if not c.passed}
    assert {"parse_doc_date", "parse_ppn", "parse_line_1_qty", "parse_line_1_discount"}.issubset(failed_names)
