"""Unit tests for multiset line-item matching with repeated SKUs and reordered ERP lines."""

from decimal import Decimal

from app.erp.schemas import ErpDocument, ErpLine
from app.extraction.schemas import ParsedDocument, ParsedLine
from app.reconcile.line_matcher import match_document_lines


def _pdf_line(
    idx: int, sku: str | None, qty: str, price: str, disc: str, amt: str, desc: str | None = None
) -> ParsedLine:
    return ParsedLine(
        source_page=1,
        line_number=idx,
        item_code_raw=sku,
        item_code_norm=sku.replace("-", "") if sku else None,
        description=desc or f"Item {sku}",
        quantity=Decimal(qty),
        uom_raw="pcs",
        unit_price=Decimal(price),
        discount_pct=Decimal(disc),
        line_amount=Decimal(amt),
    )


def _erp_line(
    row: int, sku: str | None, qty: str, price: str, disc: str, amt: str, desc: str | None = None
) -> ErpLine:
    q = Decimal(qty)
    a = Decimal(amt)
    return ErpLine(
        entry_id="E-1",
        sheet="AP_Lines",
        row=row,
        line_number=row - 1,
        item_code_raw=sku,
        item_code_norm=sku.replace("-", "") if sku else None,
        description=desc or f"Item {sku}",
        quantity_signed=q,
        quantity_abs=abs(q),
        uom_raw="pcs",
        unit_price=Decimal(price),
        discount_pct=Decimal(disc),
        line_amount_signed=a,
        line_amount_abs=abs(a),
    )


def _wrap(pdf_lines: list[ParsedLine], erp_lines: list[ErpLine]) -> tuple[ParsedDocument, ErpDocument]:
    from datetime import date

    pdf_doc = ParsedDocument(
        pages=[1],
        document_type="invoice",
        supplier_name="Supplier",
        doc_number_raw="INV-1",
        doc_number_norm="INV1",
        doc_date=date(2026, 9, 1),
        due_date=None,
        po_number_raw="PO-1",
        po_number_norm="PO1",
        reference_doc_raw=None,
        reference_doc_norm=None,
        currency="IDR",
        subtotal=Decimal("0"),
        dpp=Decimal("0"),
        ppn=Decimal("0"),
        grand_total=Decimal("0"),
        lines=pdf_lines,
    )
    erp_doc = ErpDocument(
        entry_id="E-1",
        sheet="AP_Invoices",
        row=2,
        document_type="invoice",
        vendor="Supplier",
        doc_number_raw="INV-1",
        doc_number_norm="INV1",
        doc_date=date(2026, 9, 1),
        po_number_raw="PO-1",
        po_number_norm="PO1",
        currency="IDR",
        subtotal_signed=Decimal("0"),
        dpp_signed=Decimal("0"),
        ppn_signed=Decimal("0"),
        grand_total_signed=Decimal("0"),
        doc_status="Posted",
        lines=erp_lines,
    )
    return pdf_doc, erp_doc


def test_reordered_repeated_skus_match_with_zero_discrepancies() -> None:
    pdf_lines = [
        _pdf_line(1, "GNT-KTR", "10", "18500", "0", "185000"),
        _pdf_line(2, "AMP-110", "50", "5000", "5", "237500"),
        _pdf_line(3, "GNT-KTR", "7", "18500", "2.5", "126263"),
    ]
    # ERP sorted alphabetically by SKU and reversed order of duplicate GNT-KTR rows.
    erp_lines = [
        _erp_line(2, "AMP-110", "50", "5000", "5", "237500"),
        _erp_line(3, "GNT-KTR", "7", "18500", "2.5", "126263"),
        _erp_line(4, "GNT-KTR", "10", "18500", "0", "185000"),
    ]
    pdf_doc, erp_doc = _wrap(pdf_lines, erp_lines)
    assert match_document_lines(pdf_doc, erp_doc) == []


def test_detects_qty_price_extra_pdf_and_extra_erp_discrepancies() -> None:
    pdf_lines = [
        _pdf_line(1, "AMP-110", "120", "25000", "5", "2850000"),
        _pdf_line(2, "PK-A4", "200", "52500", "0", "10500000"),
        _pdf_line(3, "DLV-JKT", "1", "250000", "0", "250000"),
    ]
    erp_lines = [
        _erp_line(2, "AMP-110", "100", "25000", "5", "2375000"),
        _erp_line(3, "PK-A4", "200", "49500", "0", "9900000"),
        _erp_line(4, "EXTRA-ERP", "1", "100000", "0", "100000"),
    ]
    pdf_doc, erp_doc = _wrap(pdf_lines, erp_lines)
    diffs = match_document_lines(pdf_doc, erp_doc)

    by_sku = {d.item_code: d for d in diffs}
    assert by_sku["AMP-110"].discrepancy_type == "LINE_QTY_MISMATCH"
    assert by_sku["AMP-110"].amount_difference == Decimal("475000")
    assert by_sku["PK-A4"].discrepancy_type == "LINE_UNIT_PRICE_MISMATCH"
    assert by_sku["PK-A4"].amount_difference == Decimal("600000")
    assert by_sku["DLV-JKT"].discrepancy_type == "EXTRA_LINE_IN_PDF"
    assert by_sku["DLV-JKT"].amount_difference == Decimal("250000")
    assert by_sku["EXTRA-ERP"].discrepancy_type == "EXTRA_LINE_IN_ERP"
    assert by_sku["EXTRA-ERP"].amount_difference == Decimal("-100000")


def test_blank_sku_lines_match_by_description_and_catch_price_diff() -> None:
    pdf_lines = [
        _pdf_line(1, None, "1", "250000", "0", "250000", desc="Biaya Pengiriman Jakarta"),
        _pdf_line(2, None, "1", "175000", "0", "175000", desc="Packing Kayu"),
    ]
    erp_lines = [
        _erp_line(2, None, "1", "250000", "0", "250000", desc="Biaya Pengiriman Jakarta"),
        _erp_line(3, None, "1", "150000", "0", "150000", desc="Packing Kayu"),
    ]
    pdf_doc, erp_doc = _wrap(pdf_lines, erp_lines)
    diffs = match_document_lines(pdf_doc, erp_doc)
    assert len(diffs) == 1
    assert diffs[0].discrepancy_type == "LINE_UNIT_PRICE_MISMATCH"
    assert diffs[0].amount_difference == Decimal("25000")
