"""Deterministic two-pass line-item matcher handling reordered rows, repeated SKUs, and blank-SKU fees."""

from decimal import Decimal

from app.erp.schemas import ErpDocument, ErpLine
from app.extraction.parser import normalize_key
from app.extraction.schemas import ParsedDocument, ParsedLine
from app.reconcile.schemas import LineDiscrepancy


def _line_match_key(sku_norm: str | None, description: str) -> str | None:
    if sku_norm:
        return f"SKU:{sku_norm}"
    desc_norm = normalize_key(description)
    return f"DESC:{desc_norm}" if desc_norm else None


def _exact_signature(
    match_key: str | None,
    qty: Decimal | None,
    unit_price: Decimal | None,
    discount_pct: Decimal | None,
    line_amount: Decimal | None,
) -> tuple[str, Decimal, Decimal, Decimal, Decimal] | None:
    if match_key is None or qty is None or unit_price is None or discount_pct is None or line_amount is None:
        return None
    return (match_key, abs(qty), abs(unit_price), discount_pct, abs(line_amount))


def _pair_cost(pdf_line: ParsedLine, erp_line: ErpLine) -> tuple[int, Decimal, Decimal, Decimal]:
    """Lower cost means closer match; identical fields first, then smallest numeric deltas."""
    price_diff = abs(
        (abs(pdf_line.unit_price) if pdf_line.unit_price is not None else Decimal("0")) - erp_line.unit_price
    )
    qty_diff = abs(
        (abs(pdf_line.quantity) if pdf_line.quantity is not None else Decimal("0")) - erp_line.quantity_abs
    )
    disc_diff = abs(
        (pdf_line.discount_pct if pdf_line.discount_pct is not None else Decimal("0")) - erp_line.discount_pct
    )
    diff_count = int(price_diff != 0) + int(qty_diff != 0) + int(disc_diff != 0)
    return (diff_count, price_diff, qty_diff, disc_diff)


def _classify_pair_discrepancy(
    doc_raw: str,
    doc_norm: str,
    pdf_line: ParsedLine,
    erp_line: ErpLine,
    sign: Decimal,
) -> LineDiscrepancy | None:
    pdf_qty = abs(pdf_line.quantity) if pdf_line.quantity is not None else Decimal("0")
    pdf_price = abs(pdf_line.unit_price) if pdf_line.unit_price is not None else Decimal("0")
    pdf_disc = pdf_line.discount_pct if pdf_line.discount_pct is not None else Decimal("0")
    pdf_amt = abs(pdf_line.line_amount) if pdf_line.line_amount is not None else Decimal("0")

    diffs: list[str] = []
    types: list[str] = []
    if pdf_qty != erp_line.quantity_abs:
        types.append("LINE_QTY_MISMATCH")
        diffs.append(f"qty PDF {pdf_qty:g} vs ERP {erp_line.quantity_abs:g}")
    if pdf_price != erp_line.unit_price:
        types.append("LINE_UNIT_PRICE_MISMATCH")
        diffs.append(f"unit price PDF {pdf_price:,.0f} vs ERP {erp_line.unit_price:,.0f}")
    if pdf_disc != erp_line.discount_pct:
        types.append("LINE_DISCOUNT_MISMATCH")
        diffs.append(f"discount PDF {pdf_disc:g}% vs ERP {erp_line.discount_pct:g}%")
    if pdf_amt != erp_line.line_amount_abs and not types:
        types.append("LINE_AMOUNT_MISMATCH")
        diffs.append(f"line amount PDF {pdf_amt:,.0f} vs ERP {erp_line.line_amount_abs:,.0f}")

    if not types:
        return None

    return LineDiscrepancy(
        doc_number_raw=doc_raw,
        doc_number_norm=doc_norm,
        discrepancy_type="+".join(types),
        item_code=pdf_line.item_code_raw or erp_line.item_code_raw,
        pdf_page=pdf_line.source_page,
        pdf_line_number=pdf_line.line_number,
        pdf_description=pdf_line.description,
        pdf_qty=pdf_qty * sign,
        pdf_unit_price=pdf_price,
        pdf_discount_pct=pdf_disc,
        pdf_line_amount=pdf_amt * sign,
        erp_ref=f"{erp_line.sheet}!R{erp_line.row}",
        erp_description=erp_line.description,
        erp_qty=erp_line.quantity_signed,
        erp_unit_price=erp_line.unit_price,
        erp_discount_pct=erp_line.discount_pct,
        erp_line_amount=erp_line.line_amount_signed,
        amount_difference=(pdf_amt - erp_line.line_amount_abs) * sign,
        detail=", ".join(diffs),
    )


def _match_exact_pass(pdf_lines: list[ParsedLine], erp_lines: list[ErpLine]) -> tuple[set[int], set[int]]:
    """Lock exact matches first so repeated SKUs on multi-page invoices pair deterministically."""
    erp_buckets: dict[tuple[str, Decimal, Decimal, Decimal, Decimal], list[int]] = {}
    for erp_idx, erp_line in enumerate(erp_lines):
        key = _line_match_key(erp_line.item_code_norm, erp_line.description)
        sig = _exact_signature(
            key, erp_line.quantity_abs, erp_line.unit_price, erp_line.discount_pct, erp_line.line_amount_abs
        )
        if sig is not None:
            erp_buckets.setdefault(sig, []).append(erp_idx)

    matched_pdf: set[int] = set()
    matched_erp: set[int] = set()
    for pdf_idx, pdf_line in enumerate(pdf_lines):
        key = _line_match_key(pdf_line.item_code_norm, pdf_line.description)
        sig = _exact_signature(
            key, pdf_line.quantity, pdf_line.unit_price, pdf_line.discount_pct, pdf_line.line_amount
        )
        if sig is not None and erp_buckets.get(sig):
            erp_idx = erp_buckets[sig].pop(0)
            matched_pdf.add(pdf_idx)
            matched_erp.add(erp_idx)
    return matched_pdf, matched_erp


def _match_greedy_pass(
    doc_raw: str,
    doc_norm: str,
    pdf_lines: list[ParsedLine],
    erp_lines: list[ErpLine],
    matched_pdf: set[int],
    matched_erp: set[int],
    sign: Decimal,
) -> list[LineDiscrepancy]:
    """Pair remaining same-key lines by smallest numeric difference and emit field discrepancies."""
    remaining_pdf = [i for i in range(len(pdf_lines)) if i not in matched_pdf]
    remaining_erp = [j for j in range(len(erp_lines)) if j not in matched_erp]
    erp_keys = {
        j: _line_match_key(erp_lines[j].item_code_norm, erp_lines[j].description) for j in remaining_erp
    }

    candidate_pairs: list[tuple[tuple[int, Decimal, Decimal, Decimal], int, int]] = []
    for i in remaining_pdf:
        pdf_key = _line_match_key(pdf_lines[i].item_code_norm, pdf_lines[i].description)
        if pdf_key is None:
            continue
        for j in remaining_erp:
            if erp_keys[j] == pdf_key:
                candidate_pairs.append((_pair_cost(pdf_lines[i], erp_lines[j]), i, j))

    # Order candidate pairs by lowest discrepancy cost so closest lines pair first.
    candidate_pairs.sort(key=lambda item: item[0])
    discrepancies: list[LineDiscrepancy] = []
    for _, i, j in candidate_pairs:
        if i in matched_pdf or j in matched_erp:
            continue
        matched_pdf.add(i)
        matched_erp.add(j)
        diff = _classify_pair_discrepancy(doc_raw, doc_norm, pdf_lines[i], erp_lines[j], sign)
        if diff is not None:
            discrepancies.append(diff)
    return discrepancies


def _build_extra_pdf_discrepancy(
    doc_raw: str, doc_norm: str, line: ParsedLine, sign: Decimal
) -> LineDiscrepancy:
    pdf_amt = abs(line.line_amount) if line.line_amount is not None else Decimal("0")
    return LineDiscrepancy(
        doc_number_raw=doc_raw,
        doc_number_norm=doc_norm,
        discrepancy_type="EXTRA_LINE_IN_PDF",
        item_code=line.item_code_raw,
        pdf_page=line.source_page,
        pdf_line_number=line.line_number,
        pdf_description=line.description,
        pdf_qty=(abs(line.quantity) * sign) if line.quantity is not None else None,
        pdf_unit_price=abs(line.unit_price) if line.unit_price is not None else None,
        pdf_discount_pct=line.discount_pct,
        pdf_line_amount=pdf_amt * sign,
        amount_difference=pdf_amt * sign,
        detail=(
            f"Line {line.line_number} on PDF Page {line.source_page} "
            f"({line.item_code_raw or 'no SKU'}: {line.description}, IDR {pdf_amt:,.0f}) "
            f"is not booked in ERP"
        ),
    )


def _build_extra_erp_discrepancy(doc_raw: str, doc_norm: str, line: ErpLine) -> LineDiscrepancy:
    return LineDiscrepancy(
        doc_number_raw=doc_raw,
        doc_number_norm=doc_norm,
        discrepancy_type="EXTRA_LINE_IN_ERP",
        item_code=line.item_code_raw,
        erp_ref=f"{line.sheet}!R{line.row}",
        erp_description=line.description,
        erp_qty=line.quantity_signed,
        erp_unit_price=line.unit_price,
        erp_discount_pct=line.discount_pct,
        erp_line_amount=line.line_amount_signed,
        amount_difference=-line.line_amount_signed,
        detail=(
            f"ERP line {line.sheet}!R{line.row} "
            f"({line.item_code_raw or 'no SKU'}: {line.description}, "
            f"IDR {line.line_amount_abs:,.0f}) is absent from the supplier PDF"
        ),
    )


def match_document_lines(pdf_doc: ParsedDocument, erp_doc: ErpDocument) -> list[LineDiscrepancy]:
    """Match PDF lines to ERP lines: Pass 1 exact multiset match, Pass 2 greedy min-diff per key."""
    doc_raw = pdf_doc.doc_number_raw or erp_doc.doc_number_raw
    doc_norm = pdf_doc.doc_number_norm or erp_doc.doc_number_norm
    sign = Decimal("-1") if erp_doc.document_type == "credit_note" else Decimal("1")

    matched_pdf, matched_erp = _match_exact_pass(pdf_doc.lines, erp_doc.lines)
    discrepancies = _match_greedy_pass(
        doc_raw, doc_norm, pdf_doc.lines, erp_doc.lines, matched_pdf, matched_erp, sign
    )

    for i, pdf_line in enumerate(pdf_doc.lines):
        if i not in matched_pdf:
            discrepancies.append(_build_extra_pdf_discrepancy(doc_raw, doc_norm, pdf_line, sign))

    for j, erp_line in enumerate(erp_doc.lines):
        if j not in matched_erp:
            discrepancies.append(_build_extra_erp_discrepancy(doc_raw, doc_norm, erp_line))

    return discrepancies
