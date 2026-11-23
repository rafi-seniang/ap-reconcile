"""Deterministic IDR arithmetic rules R1-R4 using Decimal and ROUND_HALF_UP on positive magnitudes."""

from decimal import ROUND_HALF_UP, Decimal

from app.schemas.invoice import CheckResult, ParsedDocument

_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_ELEVEN = Decimal("11")
_TWELVE = Decimal("12")
_PPN_RATE = Decimal("0.12")
_CORE_ARITHMETIC_CHECK_NAMES = frozenset({"has_doc_number", "has_lines", "subtotal_adds_up", "total_adds_up"})


def round_half_up_idr(value: Decimal) -> Decimal:
    """Round a positive rupiah amount half-up to the nearest whole rupiah (0.5 -> 1)."""
    return abs(value).quantize(_ONE, rounding=ROUND_HALF_UP)


def compute_line_amount(quantity: Decimal, unit_price: Decimal, discount_pct: Decimal) -> Decimal:
    """R1: quantity x unit price x (1 - discount % / 100), rounded half-up to whole rupiah."""
    raw_amount = abs(quantity) * abs(unit_price) * (_ONE - discount_pct / _HUNDRED)
    return round_half_up_idr(raw_amount)


def compute_dpp_nilai_lain(subtotal: Decimal) -> Decimal:
    """R3: DPP Nilai Lain = subtotal x 11/12, rounded half-up to whole rupiah."""
    return round_half_up_idr(abs(subtotal) * _ELEVEN / _TWELVE)


def compute_ppn(dpp: Decimal) -> Decimal:
    """R4: PPN = DPP x 12%, rounded half-up to whole rupiah."""
    return round_half_up_idr(abs(dpp) * _PPN_RATE)


def compute_total(subtotal: Decimal, ppn: Decimal) -> Decimal:
    """R4: Total = subtotal + PPN."""
    return abs(subtotal) + abs(ppn)


def _verify_document_lines(document: ParsedDocument, checks: list[CheckResult]) -> tuple[bool, Decimal]:
    all_lines_valid = True
    line_amount_sum = Decimal("0")
    for line in document.lines:
        if (
            line.quantity is None
            or line.unit_price is None
            or line.discount_pct is None
            or line.line_amount is None
        ):
            all_lines_valid = False
            checks.append(
                CheckResult(
                    name=f"line_{line.line_number}_adds_up",
                    passed=False,
                    detail="incomplete numeric fields on line",
                )
            )
            continue

        expected_line_amount = compute_line_amount(line.quantity, line.unit_price, line.discount_pct)
        actual_line_amount = abs(line.line_amount)
        line_amount_sum += actual_line_amount
        line_passed = expected_line_amount == actual_line_amount
        if not line_passed:
            all_lines_valid = False
        checks.append(
            CheckResult(
                name=f"line_{line.line_number}_adds_up",
                passed=line_passed,
                detail=f"expected {expected_line_amount:,.0f}, printed {actual_line_amount:,.0f}",
            )
        )
    return all_lines_valid, line_amount_sum


def _verify_tax_and_grand_total(document: ParsedDocument, checks: list[CheckResult]) -> None:
    if document.subtotal is not None:
        actual_subtotal = abs(document.subtotal)
        expected_dpp = compute_dpp_nilai_lain(actual_subtotal)
        if document.dpp is not None:
            actual_dpp = abs(document.dpp)
            is_full_subtotal_base = actual_dpp == actual_subtotal and expected_dpp != actual_subtotal
            detail = (
                f"printed DPP equals 100% subtotal ({actual_dpp:,.0f}) instead of 11/12 DPP Nilai Lain ({expected_dpp:,.0f})"
                if is_full_subtotal_base
                else f"expected 11/12 DPP {expected_dpp:,.0f}, printed {actual_dpp:,.0f}"
            )
            checks.append(
                CheckResult(name="dpp_matches_r3_11_12", passed=actual_dpp == expected_dpp, detail=detail)
            )
        else:
            checks.append(
                CheckResult(
                    name="dpp_matches_r3_11_12",
                    passed=False,
                    detail=f"DPP not printed (expected {expected_dpp:,.0f})",
                )
            )

        expected_ppn = compute_ppn(expected_dpp)
        if document.ppn is not None:
            actual_ppn = abs(document.ppn)
            checks.append(
                CheckResult(
                    name="ppn_matches_r4_12pct",
                    passed=actual_ppn == expected_ppn,
                    detail=f"expected PPN {expected_ppn:,.0f} (12% of {expected_dpp:,.0f}), printed {actual_ppn:,.0f}",
                )
            )
        else:
            checks.append(CheckResult(name="ppn_matches_r4_12pct", passed=False, detail="PPN missing"))

    if document.subtotal is not None and document.ppn is not None and document.grand_total is not None:
        expected_total = compute_total(document.subtotal, document.ppn)
        actual_total = abs(document.grand_total)
        checks.append(
            CheckResult(
                name="total_adds_up",
                passed=expected_total == actual_total,
                detail=f"subtotal + PPN = {expected_total:,.0f}, printed total {actual_total:,.0f}",
            )
        )
    else:
        checks.append(
            CheckResult(name="total_adds_up", passed=False, detail="subtotal, PPN, or total missing")
        )


def verify_pdf_document_math(document: ParsedDocument) -> list[CheckResult]:
    """Run all R1-R4 internal consistency checks on a parsed PDF document without altering figures (R7)."""
    checks: list[CheckResult] = [
        CheckResult(
            name="has_doc_number",
            passed=document.doc_number_norm is not None,
            detail=document.doc_number_raw or "",
        ),
        CheckResult(name="has_lines", passed=len(document.lines) > 0, detail=f"{len(document.lines)} lines"),
    ]

    all_lines_valid, line_amount_sum = _verify_document_lines(document, checks)

    if document.subtotal is not None and document.lines and all_lines_valid:
        actual_subtotal = abs(document.subtotal)
        checks.append(
            CheckResult(
                name="subtotal_adds_up",
                passed=line_amount_sum == actual_subtotal,
                detail=f"sum of lines {line_amount_sum:,.0f}, printed subtotal {actual_subtotal:,.0f}",
            )
        )
    elif document.subtotal is None:
        checks.append(CheckResult(name="subtotal_adds_up", passed=False, detail="subtotal missing"))

    _verify_tax_and_grand_total(document, checks)
    return checks


def failed_core_arithmetic_checks(document: ParsedDocument) -> list[CheckResult]:
    """Return checks that indicate either an extraction misread or an internal invoice addition error."""
    return [
        check
        for check in document.checks
        if not check.passed
        and (
            check.name.startswith("parse_")
            or check.name.startswith("line_")
            or check.name in _CORE_ARITHMETIC_CHECK_NAMES
        )
    ]
