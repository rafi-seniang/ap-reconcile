"""Assemble page-level extractions into logical supplier documents without merging duplicate invoices."""

from app.extraction.parser import normalize_key
from app.extraction.schemas import AssembledDocument, AssembledLine, ExtractedPage


def _is_continuation_of(page: ExtractedPage, current: AssembledDocument) -> bool:
    """Join only when the previous page left the invoice open without closing on a grand total."""
    current_closed = bool(current.grand_total_raw and current.grand_total_raw.strip())
    if current_closed:
        return False

    printed = page.printed
    if printed.doc_number_raw is None or not printed.doc_number_raw.strip():
        return True

    return normalize_key(printed.doc_number_raw) == normalize_key(current.doc_number_raw)


def assemble_documents(pages: list[ExtractedPage]) -> list[AssembledDocument]:
    """Group sequential ExtractedPage objects into logical single- or multi-page documents."""
    # Sort by PDF page order so concurrent extraction responses stitch deterministically.
    ordered_pages = sorted(pages, key=lambda item: item.page_number)
    documents: list[AssembledDocument] = []

    for page in ordered_pages:
        printed = page.printed
        page_lines = [AssembledLine(source_page=page.page_number, printed=line) for line in printed.lines]

        if documents and _is_continuation_of(page, documents[-1]):
            current = documents[-1]
            current.pages.append(page.page_number)
            current.lines.extend(page_lines)
            if current.subtotal_raw is None and printed.subtotal_raw is not None:
                current.subtotal_raw = printed.subtotal_raw
            if current.dpp_raw is None and printed.dpp_raw is not None:
                current.dpp_raw = printed.dpp_raw
            if current.ppn_raw is None and printed.ppn_raw is not None:
                current.ppn_raw = printed.ppn_raw
            if current.grand_total_raw is None and printed.grand_total_raw is not None:
                current.grand_total_raw = printed.grand_total_raw
            continue

        documents.append(
            AssembledDocument(
                pages=[page.page_number],
                document_type=printed.document_type,
                supplier_name=printed.supplier_name,
                doc_number_raw=printed.doc_number_raw,
                doc_date_raw=printed.doc_date_raw,
                due_date_raw=printed.due_date_raw,
                po_number_raw=printed.po_number_raw,
                reference_doc_raw=printed.reference_doc_raw,
                currency=printed.currency,
                subtotal_raw=printed.subtotal_raw,
                dpp_raw=printed.dpp_raw,
                ppn_raw=printed.ppn_raw,
                grand_total_raw=printed.grand_total_raw,
                lines=page_lines,
            )
        )

    return documents
