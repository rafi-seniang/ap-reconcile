"""Unit tests for deterministic R1-R4 IDR arithmetic and ROUND_HALF_UP edge cases."""

from decimal import Decimal

import pytest

from app.engine.math_rules import (
    compute_dpp_nilai_lain,
    compute_line_amount,
    compute_ppn,
    compute_total,
    failed_core_arithmetic_checks,
    verify_pdf_document_math,
)
from app.llm.parser import parse_assembled_document
from app.schemas.invoice import AssembledDocument, AssembledLine, PrintedLine


@pytest.mark.parametrize(
    ("qty", "price", "disc", "expected"),
    [
        # Exact .5 cases from September 2026 batch where banker's rounding (ROUND_HALF_EVEN) would diverge.
        (
            Decimal("65"),
            Decimal("4750"),
            Decimal("5"),
            Decimal("293313"),
        ),  # 293,312.50 -> 293,313 (0141 KLP-NO3)
        (
            Decimal("27"),
            Decimal("18500"),
            Decimal("2.5"),
            Decimal("487013"),
        ),  # 487,012.50 -> 487,013 (0144 GNT-KTR)
        (
            Decimal("25"),
            Decimal("3250"),
            Decimal("5"),
            Decimal("77188"),
        ),  # 77,187.50 -> 77,188 (0150 STP-NO10)
        (
            Decimal("7"),
            Decimal("18500"),
            Decimal("2.5"),
            Decimal("126263"),
        ),  # 126,262.50 -> 126,263 (0153 GNT-KTR)
    ],
)
def test_r1_line_amount_exact_half_up(qty: Decimal, price: Decimal, disc: Decimal, expected: Decimal) -> None:
    assert compute_line_amount(qty, price, disc) == expected


def test_r3_dpp_exact_half_up_from_inv_0147() -> None:
    # 15,529,890 * 11 / 12 = 14,235,732.50 -> must round half-up to 14,235,733.
    dpp = compute_dpp_nilai_lain(Decimal("15529890"))
    assert dpp == Decimal("14235733")
    assert compute_ppn(dpp) == Decimal("1708288")
    assert compute_total(Decimal("15529890"), Decimal("1708288")) == Decimal("17238178")


def test_r3_r4_two_step_vs_one_step_ppn_difference_from_inv_0143() -> None:
    # PDF 2-step (11/12 * 12% = 1,749,258) differs by Rp 1 from ERP 1-step (11% = 1,749,259).
    dpp = compute_dpp_nilai_lain(Decimal("15902350"))
    ppn = compute_ppn(dpp)
    assert dpp == Decimal("14577154")
    assert ppn == Decimal("1749258")
    assert compute_total(Decimal("15902350"), ppn) == Decimal("17651608")


def test_verify_pdf_math_detects_wrong_dpp_without_triggering_core_retry() -> None:
    assembled = AssembledDocument(
        pages=[8],
        document_type="invoice",
        supplier_name="PT Prima Kertas Indonesia",
        doc_number_raw="PKI/INV/2026/09/0148",
        doc_date_raw="14 September 2026",
        due_date_raw="14 Oktober 2026",
        po_number_raw="PO-88221",
        reference_doc_raw=None,
        currency="IDR",
        subtotal_raw="20.484.150",
        dpp_raw="20.484.150",
        ppn_raw="2.458.098",
        grand_total_raw="22.942.248",
        lines=[
            AssembledLine(
                source_page=8,
                printed=PrintedLine(
                    line_number_raw="1",
                    item_code="PK-A4-80-RM",
                    description="Kertas HVS A4",
                    quantity_raw="410",
                    uom_raw="rim",
                    unit_price_raw="49.961",
                    discount_pct_raw="0",
                    line_amount_raw="20.484.150",
                ),
            )
        ],
    )
    # Use an exact line product so line_1_adds_up passes while DPP fails R3.
    assembled.lines[0].printed.quantity_raw = "1"
    assembled.lines[0].printed.unit_price_raw = "20.484.150"
    parsed = parse_assembled_document(assembled)
    parsed.checks.extend(verify_pdf_document_math(parsed))

    check_map = {c.name: c for c in parsed.checks}
    assert check_map["subtotal_adds_up"].passed is True
    assert check_map["total_adds_up"].passed is True
    assert check_map["dpp_matches_r3_11_12"].passed is False
    assert check_map["ppn_matches_r4_12pct"].passed is False
    assert failed_core_arithmetic_checks(parsed) == []


def test_verify_pdf_math_detects_internal_subtotal_error_as_core_failure() -> None:
    assembled = AssembledDocument(
        pages=[9],
        document_type="invoice",
        supplier_name="PT Prima Kertas Indonesia",
        doc_number_raw="PKI/INV/2026/09/0149",
        doc_date_raw="16 September 2026",
        due_date_raw=None,
        po_number_raw="PO-88225",
        reference_doc_raw=None,
        currency="IDR",
        subtotal_raw="24.197.075",
        dpp_raw="22.180.652",
        ppn_raw="2.661.678",
        grand_total_raw="26.858.753",
        lines=[
            AssembledLine(
                source_page=9,
                printed=PrintedLine(
                    line_number_raw="1",
                    item_code="ITEM-1",
                    description="Paper",
                    quantity_raw="1",
                    uom_raw="lot",
                    unit_price_raw="24.097.075",
                    discount_pct_raw=None,
                    line_amount_raw="24.097.075",
                ),
            )
        ],
    )
    parsed = parse_assembled_document(assembled)
    parsed.checks.extend(verify_pdf_document_math(parsed))

    failed = {c.name for c in failed_core_arithmetic_checks(parsed)}
    assert failed == {"subtotal_adds_up"}


def test_verify_pdf_math_accepts_positive_credit_note() -> None:
    assembled = AssembledDocument(
        pages=[16],
        document_type="credit_note",
        supplier_name="PT Prima Kertas Indonesia",
        doc_number_raw="PKI/CN/2026/09/0012",
        doc_date_raw="26 September 2026",
        due_date_raw=None,
        po_number_raw=None,
        reference_doc_raw="PKI/INV/2026/08/0131",
        currency="IDR",
        subtotal_raw="876.000",
        dpp_raw="803.000",
        ppn_raw="96.360",
        grand_total_raw="972.360",
        lines=[
            AssembledLine(
                source_page=16,
                printed=PrintedLine(
                    line_number_raw="1",
                    item_code="RET-01",
                    description="Return item",
                    quantity_raw="1",
                    uom_raw="lot",
                    unit_price_raw="876.000",
                    discount_pct_raw="0",
                    line_amount_raw="876.000",
                ),
            )
        ],
    )
    parsed = parse_assembled_document(assembled)
    parsed.checks.extend(verify_pdf_document_math(parsed))
    assert all(c.passed for c in parsed.checks)
