"""Unit tests for multi-page document assembly and duplicate PDF invoice preservation."""

from app.llm.assembler import assemble_documents
from app.schemas.invoice import ExtractedPage, PrintedLine, PrintedPage


def _line(code: str, amount: str) -> PrintedLine:
    return PrintedLine(
        line_number_raw="1",
        item_code=code,
        description=f"Item {code}",
        quantity_raw="1",
        uom_raw="pcs",
        unit_price_raw=amount,
        discount_pct_raw="0",
        line_amount_raw=amount,
    )


def test_assemble_merges_headerless_continuation_page_in_page_order() -> None:
    # Simulate out-of-order concurrent arrival: page 14 arrives before page 13.
    p14 = ExtractedPage(
        page_number=14,
        printed=PrintedPage(
            is_continuation=True,
            doc_number_raw=None,
            subtotal_raw="91.582.382",
            dpp_raw="83.950.517",
            ppn_raw="10.074.062",
            grand_total_raw="101.656.444",
            lines=[_line("SKU-B", "200.000")],
        ),
    )
    p13 = ExtractedPage(
        page_number=13,
        printed=PrintedPage(
            is_continuation=False,
            doc_number_raw="PKI/INV/2026/09/0153",
            po_number_raw="PO-88235",
            lines=[_line("SKU-A", "100.000")],
        ),
    )

    docs = assemble_documents([p14, p13])
    assert len(docs) == 1
    assert docs[0].pages == [13, 14]
    assert docs[0].doc_number_raw == "PKI/INV/2026/09/0153"
    assert docs[0].grand_total_raw == "101.656.444"
    assert [ln.printed.item_code for ln in docs[0].lines] == ["SKU-A", "SKU-B"]


def test_assemble_merges_repeated_header_continuation_when_prior_page_unclosed() -> None:
    p1 = ExtractedPage(
        page_number=1,
        printed=PrintedPage(
            is_continuation=False,
            doc_number_raw="INV/2026/10/0001",
            grand_total_raw=None,
            lines=[_line("SKU-1", "100.000")],
        ),
    )
    p2 = ExtractedPage(
        page_number=2,
        printed=PrintedPage(
            is_continuation=True,
            doc_number_raw="INV/2026/10/0001",
            grand_total_raw="222.000",
            lines=[_line("SKU-2", "100.000")],
        ),
    )
    docs = assemble_documents([p1, p2])
    assert len(docs) == 1
    assert docs[0].pages == [1, 2]
    assert len(docs[0].lines) == 2


def test_assemble_keeps_duplicate_complete_invoice_pages_separate() -> None:
    # Both pages carry the same invoice number AND page 1 already has a grand total -> DUPLICATE_IN_PDF.
    p1 = ExtractedPage(
        page_number=1,
        printed=PrintedPage(
            is_continuation=False,
            doc_number_raw="INV/2026/10/0099",
            subtotal_raw="1.000.000",
            ppn_raw="110.000",
            grand_total_raw="1.110.000",
            lines=[_line("SKU-1", "1.000.000")],
        ),
    )
    p2 = ExtractedPage(
        page_number=2,
        printed=PrintedPage(
            is_continuation=True,  # Even if an LLM mistakenly flags continuation, closed prior total blocks merging.
            doc_number_raw="INV/2026/10/0099",
            subtotal_raw="1.000.000",
            ppn_raw="110.000",
            grand_total_raw="1.110.000",
            lines=[_line("SKU-1", "1.000.000")],
        ),
    )
    docs = assemble_documents([p1, p2])
    assert len(docs) == 2
    assert docs[0].pages == [1]
    assert docs[1].pages == [2]


def test_assemble_merges_repeated_header_when_unclosed_even_if_flag_false() -> None:
    p1 = ExtractedPage(
        page_number=1,
        printed=PrintedPage(
            is_continuation=False,
            doc_number_raw="INV/2026/11/0010",
            grand_total_raw=None,
            lines=[_line("SKU-1", "100.000")],
        ),
    )
    p2 = ExtractedPage(
        page_number=2,
        printed=PrintedPage(
            is_continuation=False,
            doc_number_raw="INV/2026/11/0010",
            grand_total_raw="222.000",
            lines=[_line("SKU-2", "100.000")],
        ),
    )
    docs = assemble_documents([p1, p2])
    assert len(docs) == 1
    assert docs[0].pages == [1, 2]
    assert len(docs[0].lines) == 2


def test_assemble_keeps_blank_page_after_closed_invoice_separate() -> None:
    p1 = ExtractedPage(
        page_number=1,
        printed=PrintedPage(
            is_continuation=False,
            doc_number_raw="INV/2026/11/0011",
            subtotal_raw="100.000",
            ppn_raw="11.000",
            grand_total_raw="111.000",
            lines=[_line("SKU-1", "100.000")],
        ),
    )
    p2_blank = ExtractedPage(page_number=2, printed=PrintedPage())
    docs = assemble_documents([p1, p2_blank])
    assert len(docs) == 2
    assert docs[0].pages == [1]
    assert docs[1].pages == [2]
    assert docs[1].doc_number_raw is None


def test_assemble_backfills_missing_header_fields_from_continuation_and_isolates_leading_blank() -> None:
    p1_blank = ExtractedPage(page_number=1, printed=PrintedPage())
    p2_open = ExtractedPage(
        page_number=2,
        printed=PrintedPage(
            doc_number_raw="INV/2026/10/0300",
            po_number_raw=None,
            lines=[_line("SKU-1", "100.000")],
        ),
    )
    p3_cont = ExtractedPage(
        page_number=3,
        printed=PrintedPage(
            is_continuation=True,
            po_number_raw="PO-88300",
            subtotal_raw="200.000",
            ppn_raw="22.000",
            grand_total_raw="222.000",
            lines=[_line("SKU-2", "100.000")],
        ),
    )
    docs = assemble_documents([p1_blank, p2_open, p3_cont])
    assert len(docs) == 2
    assert docs[0].pages == [1]
    assert docs[1].pages == [2, 3]
    assert docs[1].po_number_raw == "PO-88300"
