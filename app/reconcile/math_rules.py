"""Deterministic IDR arithmetic rules R1-R4 using Decimal and ROUND_HALF_UP on positive magnitudes."""

from decimal import ROUND_HALF_UP, Decimal

from app.extraction.schemas import CheckResult, ParsedDocument

_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_ELEVEN = Decimal("11")
_TWELVE = Decimal("12")
_PPN_RATE = Decimal("0.12")


def round_half_up_idr(value: Decimal) -> Decimal:
    """Round a positive rupiah amount half-up to the nearest whole rupiah (0.5 -> 1)."""
    return abs(value).quantize(_ONE, rounding=ROUND_HALF_UP)


def compute_line_amount(quantity: Decimal, unit_price: Decimal, discount_pct: Decimal) -> Decimal:
    """R1: quantity x unit price x (1 - discount % / 100), rounded half-up to whole rupiah."""
    raw = abs(quantity) * abs(unit_price) * (_ONE - discount_pct / _HUNDRED)
    return round_half_up_idr(raw)


def compute_dpp_nilai_lain(subtotal: Decimal) -> Decimal:
    """R3: DPP Nilai Lain = subtotal x 11/12, rounded half-up to whole rupiah."""
    return round_half_up_idr(abs(subtotal) * _ELEVEN / _TWELVE)


def compute_ppn(dpp: Decimal) -> Decimal:
    """R4: PPN = DPP x 12%, rounded half-up to whole rupiah."""
    return round_half_up_idr(abs(dpp) * _PPN_RATE)


def compute_total(subtotal: Decimal, ppn: Decimal) -> Decimal:
    """R4: Total = subtotal + PPN."""
    return abs(subtotal) + abs(ppn)


def _verify_lines(doc: ParsedDocument, checks: list[CheckResult]) -> tuple[bool, Decimal]:
    all_lines_valid = True
    line_sum = Decimal("0")
    for line in doc.lines:
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

        expected_line = compute_line_amount(line.quantity, line.unit_price, line.discount_pct)
        actual_line = abs(line.line_amount)
        line_sum += actual_line
        passed = expected_line == actual_line
        if not passed:
            all_lines_valid = False
        checks.append(
            CheckResult(
                name=f"line_{line.line_number}_adds_up",
                passed=passed,
                detail=f"expected {expected_line:,.0f}, printed {actual_line:,.0f}",
            )
        )
    return all_lines_valid, line_sum


def _verify_tax_and_total(doc: ParsedDocument, checks: list[CheckResult]) -> None:
    if doc.subtotal is not None:
        actual_sub = abs(doc.subtotal)
        expected_dpp = compute_dpp_nilai_lain(actual_sub)
        if doc.dpp is not None:
            actual_dpp = abs(doc.dpp)
            is_full_base = actual_dpp == actual_sub and expected_dpp != actual_sub
            detail = (
                f"printed DPP equals 100% subtotal ({actual_dpp:,.0f}) instead of 11/12 DPP Nilai Lain ({expected_dpp:,.0f})"
                if is_full_base
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
        if doc.ppn is not None:
            actual_ppn = abs(doc.ppn)
            checks.append(
                CheckResult(
                    name="ppn_matches_r4_12pct",
                    passed=actual_ppn == expected_ppn,
                    detail=f"expected PPN {expected_ppn:,.0f} (12% of {expected_dpp:,.0f}), printed {actual_ppn:,.0f}",
                )
            )
        else:
            checks.append(CheckResult(name="ppn_matches_r4_12pct", passed=False, detail="PPN missing"))

    if doc.subtotal is not None and doc.ppn is not None and doc.grand_total is not None:
        expected_total = compute_total(doc.subtotal, doc.ppn)
        actual_total = abs(doc.grand_total)
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


def verify_pdf_document_math(doc: ParsedDocument) -> list[CheckResult]:
    """Run all R1-R4 internal consistency checks on a parsed PDF document without altering figures (R7)."""
    checks: list[CheckResult] = [
        CheckResult(
            name="has_doc_number", passed=doc.doc_number_norm is not None, detail=doc.doc_number_raw or ""
        ),
        CheckResult(name="has_lines", passed=len(doc.lines) > 0, detail=f"{len(doc.lines)} lines"),
    ]

    all_lines_valid, line_sum = _verify_lines(doc, checks)

    if doc.subtotal is not None and doc.lines and all_lines_valid:
        actual_sub = abs(doc.subtotal)
        checks.append(
            CheckResult(
                name="subtotal_adds_up",
                passed=line_sum == actual_sub,
                detail=f"sum of lines {line_sum:,.0f}, printed subtotal {actual_sub:,.0f}",
            )
        )
    elif doc.subtotal is None:
        checks.append(CheckResult(name="subtotal_adds_up", passed=False, detail="subtotal missing"))

    _verify_tax_and_total(doc, checks)
    return checks


def failed_core_arithmetic_checks(doc: ParsedDocument) -> list[CheckResult]:
    """Return checks that indicate either an extraction misread or an internal invoice addition error."""
    core_names = {"has_doc_number", "has_lines", "subtotal_adds_up", "total_adds_up"}
    return [
        check
        for check in doc.checks
        if not check.passed
        and (check.name.startswith("parse_") or check.name.startswith("line_") or check.name in core_names)
    ]
