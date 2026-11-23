"""Deterministic two-pass line-item matcher handling reordered rows, repeated SKUs, and blank-SKU fees."""

from collections import deque
from decimal import Decimal

from app.llm.parser import normalize_key
from app.schemas.erp import ErpDocument, ErpLine
from app.schemas.invoice import ParsedDocument, ParsedLine
from app.schemas.reconciliation import LineDiscrepancy


def _build_line_match_key(sku_norm: str | None, description: str) -> str | None:
    if sku_norm:
        return f"SKU:{sku_norm}"
    description_norm = normalize_key(description)
    return f"DESC:{description_norm}" if description_norm else None


def _build_exact_line_signature(
    match_key: str | None,
    quantity: Decimal | None,
    unit_price: Decimal | None,
    discount_pct: Decimal | None,
    line_amount: Decimal | None,
) -> tuple[str, Decimal, Decimal, Decimal, Decimal] | None:
    if (
        match_key is None
        or quantity is None
        or unit_price is None
        or discount_pct is None
        or line_amount is None
    ):
        return None
    return (match_key, abs(quantity), abs(unit_price), discount_pct, abs(line_amount))


def _compute_line_pair_cost(pdf_line: ParsedLine, erp_line: ErpLine) -> tuple[int, Decimal, Decimal, Decimal]:
    """Lower cost means closer match; identical fields first, then smallest numeric deltas."""
    price_delta = abs(
        (abs(pdf_line.unit_price) if pdf_line.unit_price is not None else Decimal("0")) - erp_line.unit_price
    )
    qty_delta = abs(
        (abs(pdf_line.quantity) if pdf_line.quantity is not None else Decimal("0")) - erp_line.quantity_abs
    )
    discount_delta = abs(
        (pdf_line.discount_pct if pdf_line.discount_pct is not None else Decimal("0")) - erp_line.discount_pct
    )
    changed_field_count = int(price_delta != 0) + int(qty_delta != 0) + int(discount_delta != 0)
    return (changed_field_count, price_delta, qty_delta, discount_delta)


def _classify_paired_line_discrepancy(
    document_number_raw: str,
    document_number_norm: str,
    pdf_line: ParsedLine,
    erp_line: ErpLine,
    sign: Decimal,
) -> LineDiscrepancy | None:
    pdf_qty = abs(pdf_line.quantity) if pdf_line.quantity is not None else Decimal("0")
    pdf_price = abs(pdf_line.unit_price) if pdf_line.unit_price is not None else Decimal("0")
    pdf_discount = pdf_line.discount_pct if pdf_line.discount_pct is not None else Decimal("0")
    pdf_amount = abs(pdf_line.line_amount) if pdf_line.line_amount is not None else Decimal("0")

    difference_notes: list[str] = []
    discrepancy_types: list[str] = []
    if pdf_qty != erp_line.quantity_abs:
        discrepancy_types.append("LINE_QTY_MISMATCH")
        difference_notes.append(f"qty PDF {pdf_qty:g} vs ERP {erp_line.quantity_abs:g}")
    if pdf_price != erp_line.unit_price:
        discrepancy_types.append("LINE_UNIT_PRICE_MISMATCH")
        difference_notes.append(f"unit price PDF {pdf_price:,.0f} vs ERP {erp_line.unit_price:,.0f}")
    if pdf_discount != erp_line.discount_pct:
        discrepancy_types.append("LINE_DISCOUNT_MISMATCH")
        difference_notes.append(f"discount PDF {pdf_discount:g}% vs ERP {erp_line.discount_pct:g}%")
    if pdf_amount != erp_line.line_amount_abs and not discrepancy_types:
        discrepancy_types.append("LINE_AMOUNT_MISMATCH")
        difference_notes.append(f"line amount PDF {pdf_amount:,.0f} vs ERP {erp_line.line_amount_abs:,.0f}")

    if not discrepancy_types:
        return None

    return LineDiscrepancy(
        doc_number_raw=document_number_raw,
        doc_number_norm=document_number_norm,
        discrepancy_type="+".join(discrepancy_types),
        item_code=pdf_line.item_code_raw or erp_line.item_code_raw,
        pdf_page=pdf_line.source_page,
        pdf_line_number=pdf_line.line_number,
        pdf_description=pdf_line.description,
        pdf_qty=pdf_qty * sign,
        pdf_unit_price=pdf_price,
        pdf_discount_pct=pdf_discount,
        pdf_line_amount=pdf_amount * sign,
        erp_ref=f"{erp_line.sheet}!R{erp_line.row}",
        erp_description=erp_line.description,
        erp_qty=erp_line.quantity_signed,
        erp_unit_price=erp_line.unit_price,
        erp_discount_pct=erp_line.discount_pct,
        erp_line_amount=erp_line.line_amount_signed,
        amount_difference=(pdf_amount - erp_line.line_amount_abs) * sign,
        detail=", ".join(difference_notes),
    )


def _match_exact_lines_pass(
    pdf_lines: list[ParsedLine], erp_lines: list[ErpLine]
) -> tuple[set[int], set[int]]:
    """Lock exact matches first in O(n) so repeated SKUs on multi-page invoices pair deterministically."""
    erp_buckets_by_signature: dict[tuple[str, Decimal, Decimal, Decimal, Decimal], deque[int]] = {}
    for erp_index, erp_line in enumerate(erp_lines):
        match_key = _build_line_match_key(erp_line.item_code_norm, erp_line.description)
        signature = _build_exact_line_signature(
            match_key, erp_line.quantity_abs, erp_line.unit_price, erp_line.discount_pct, erp_line.line_amount_abs
        )
        if signature is not None:
            erp_buckets_by_signature.setdefault(signature, deque()).append(erp_index)

    matched_pdf_indices: set[int] = set()
    matched_erp_indices: set[int] = set()
    for pdf_index, pdf_line in enumerate(pdf_lines):
        match_key = _build_line_match_key(pdf_line.item_code_norm, pdf_line.description)
        signature = _build_exact_line_signature(
            match_key, pdf_line.quantity, pdf_line.unit_price, pdf_line.discount_pct, pdf_line.line_amount
        )
        if signature is not None:
            bucket = erp_buckets_by_signature.get(signature)
            if bucket:
                erp_index = bucket.popleft()
                matched_pdf_indices.add(pdf_index)
                matched_erp_indices.add(erp_index)
    return matched_pdf_indices, matched_erp_indices


def _match_remaining_lines_by_key_pass(
    document_number_raw: str,
    document_number_norm: str,
    pdf_lines: list[ParsedLine],
    erp_lines: list[ErpLine],
    matched_pdf_indices: set[int],
    matched_erp_indices: set[int],
    sign: Decimal,
) -> list[LineDiscrepancy]:
    """Pair remaining same-key lines by smallest numeric difference using bucketed key lookup."""
    erp_indices_by_match_key: dict[str, list[int]] = {}
    for erp_index, erp_line in enumerate(erp_lines):
        if erp_index in matched_erp_indices:
            continue
        match_key = _build_line_match_key(erp_line.item_code_norm, erp_line.description)
        if match_key is not None:
            erp_indices_by_match_key.setdefault(match_key, []).append(erp_index)

    candidate_pairs: list[tuple[tuple[int, Decimal, Decimal, Decimal], int, int]] = []
    for pdf_index, pdf_line in enumerate(pdf_lines):
        if pdf_index in matched_pdf_indices:
            continue
        pdf_key = _build_line_match_key(pdf_line.item_code_norm, pdf_line.description)
        if pdf_key is None:
            continue
        for erp_index in erp_indices_by_match_key.get(pdf_key, ()):
            candidate_pairs.append(
                (_compute_line_pair_cost(pdf_line, erp_lines[erp_index]), pdf_index, erp_index)
            )

    # Order same-key candidates by smallest discrepancy cost so closest lines pair first.
    candidate_pairs.sort(key=lambda item: item[0])
    discrepancies: list[LineDiscrepancy] = []
    for _, pdf_index, erp_index in candidate_pairs:
        if pdf_index in matched_pdf_indices or erp_index in matched_erp_indices:
            continue
        matched_pdf_indices.add(pdf_index)
        matched_erp_indices.add(erp_index)
        discrepancy = _classify_paired_line_discrepancy(
            document_number_raw, document_number_norm, pdf_lines[pdf_index], erp_lines[erp_index], sign
        )
        if discrepancy is not None:
            discrepancies.append(discrepancy)
    return discrepancies


def _build_extra_pdf_line_discrepancy(
    document_number_raw: str, document_number_norm: str, pdf_line: ParsedLine, sign: Decimal
) -> LineDiscrepancy:
    pdf_amount = abs(pdf_line.line_amount) if pdf_line.line_amount is not None else Decimal("0")
    return LineDiscrepancy(
        doc_number_raw=document_number_raw,
        doc_number_norm=document_number_norm,
        discrepancy_type="EXTRA_LINE_IN_PDF",
        item_code=pdf_line.item_code_raw,
        pdf_page=pdf_line.source_page,
        pdf_line_number=pdf_line.line_number,
        pdf_description=pdf_line.description,
        pdf_qty=(abs(pdf_line.quantity) * sign) if pdf_line.quantity is not None else None,
        pdf_unit_price=abs(pdf_line.unit_price) if pdf_line.unit_price is not None else None,
        pdf_discount_pct=pdf_line.discount_pct,
        pdf_line_amount=pdf_amount * sign,
        amount_difference=pdf_amount * sign,
        detail=(
            f"Line {pdf_line.line_number} on PDF Page {pdf_line.source_page} "
            f"({pdf_line.item_code_raw or 'no SKU'}: {pdf_line.description}, IDR {pdf_amount:,.0f}) "
            f"is not booked in ERP"
        ),
    )


def _build_extra_erp_line_discrepancy(
    document_number_raw: str, document_number_norm: str, erp_line: ErpLine
) -> LineDiscrepancy:
    return LineDiscrepancy(
        doc_number_raw=document_number_raw,
        doc_number_norm=document_number_norm,
        discrepancy_type="EXTRA_LINE_IN_ERP",
        item_code=erp_line.item_code_raw,
        erp_ref=f"{erp_line.sheet}!R{erp_line.row}",
        erp_description=erp_line.description,
        erp_qty=erp_line.quantity_signed,
        erp_unit_price=erp_line.unit_price,
        erp_discount_pct=erp_line.discount_pct,
        erp_line_amount=erp_line.line_amount_signed,
        amount_difference=-erp_line.line_amount_signed,
        detail=(
            f"ERP line {erp_line.sheet}!R{erp_line.row} "
            f"({erp_line.item_code_raw or 'no SKU'}: {erp_line.description}, "
            f"IDR {erp_line.line_amount_abs:,.0f}) is absent from the supplier PDF"
        ),
    )


def match_document_lines(pdf_document: ParsedDocument, erp_document: ErpDocument) -> list[LineDiscrepancy]:
    """Match PDF lines to ERP lines: Pass 1 exact multiset match, Pass 2 bucketed min-diff per key."""
    document_number_raw = pdf_document.doc_number_raw or erp_document.doc_number_raw
    document_number_norm = pdf_document.doc_number_norm or erp_document.doc_number_norm
    sign = Decimal("-1") if erp_document.document_type == "credit_note" else Decimal("1")

    matched_pdf_indices, matched_erp_indices = _match_exact_lines_pass(pdf_document.lines, erp_document.lines)
    discrepancies = _match_remaining_lines_by_key_pass(
        document_number_raw,
        document_number_norm,
        pdf_document.lines,
        erp_document.lines,
        matched_pdf_indices,
        matched_erp_indices,
        sign,
    )

    for pdf_index, pdf_line in enumerate(pdf_document.lines):
        if pdf_index not in matched_pdf_indices:
            discrepancies.append(
                _build_extra_pdf_line_discrepancy(document_number_raw, document_number_norm, pdf_line, sign)
            )

    for erp_index, erp_line in enumerate(erp_document.lines):
        if erp_index not in matched_erp_indices:
            discrepancies.append(
                _build_extra_erp_line_discrepancy(document_number_raw, document_number_norm, erp_line)
            )

    return discrepancies
