"""Assemble page-level extractions into logical supplier documents without merging duplicate invoices."""

from app.llm.parser import normalize_key
from app.schemas.invoice import AssembledDocument, AssembledLine, ExtractedPage


def _is_continuation_of_open_document(page: ExtractedPage, current_document: AssembledDocument) -> bool:
    """Join only when the previous page left a non-empty invoice open without closing on a grand total."""
    if not current_document.doc_number_raw and not current_document.lines:
        return False
    current_is_closed = bool(current_document.grand_total_raw and current_document.grand_total_raw.strip())
    if current_is_closed:
        return False

    printed = page.printed
    if printed.doc_number_raw is None or not printed.doc_number_raw.strip():
        return True

    return normalize_key(printed.doc_number_raw) == normalize_key(current_document.doc_number_raw)


def assemble_documents(pages: list[ExtractedPage]) -> list[AssembledDocument]:
    """Group sequential ExtractedPage objects into logical single- or multi-page documents."""
    ordered_pages = sorted(pages, key=lambda item: item.page_number)
    documents: list[AssembledDocument] = []

    for page in ordered_pages:
        printed = page.printed
        page_lines = [AssembledLine(source_page=page.page_number, printed=line) for line in printed.lines]

        if documents and _is_continuation_of_open_document(page, documents[-1]):
            current_document = documents[-1]
            current_document.pages.append(page.page_number)
            current_document.lines.extend(page_lines)
            for field_name in (
                "supplier_name",
                "doc_date_raw",
                "due_date_raw",
                "po_number_raw",
                "reference_doc_raw",
                "currency",
                "subtotal_raw",
                "dpp_raw",
                "ppn_raw",
                "grand_total_raw",
            ):
                if getattr(current_document, field_name) is None and getattr(printed, field_name) is not None:
                    setattr(current_document, field_name, getattr(printed, field_name))
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
