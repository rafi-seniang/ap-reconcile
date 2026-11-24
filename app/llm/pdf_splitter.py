"""Deterministic PDF page PNG rendering via PyMuPDF from file paths or in-memory bytes."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

_pymupdf: Any = pymupdf


@dataclass(frozen=True)
class PdfPagePayload:
    """One 1-based page from a supplier PDF batch rendered to PNG bytes."""

    page_number: int
    png_bytes: bytes


def _open_pdf_document(source: Path | str | bytes) -> Any:
    if isinstance(source, bytes):
        if not source:
            raise ValueError("PDF byte payload is empty")
        try:
            return _pymupdf.open(stream=source, filetype="pdf")
        except Exception as exc:
            raise ValueError(f"Invalid or corrupted PDF file: {exc}") from exc
    pdf_path = Path(source)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    try:
        return _pymupdf.open(pdf_path)
    except Exception as exc:
        raise ValueError(f"Invalid or corrupted PDF file: {exc}") from exc


def load_pdf_pages(
    source: Path | str | bytes, render_dpi: int = 200, max_pages: int = 200
) -> list[PdfPagePayload]:
    """Render each page of a PDF (from disk path or raw bytes) to PNG bytes for vision and UI preview."""
    payloads: list[PdfPagePayload] = []
    with _open_pdf_document(source) as document:
        if document.needs_pass:
            raise ValueError("Password-protected PDF files are not supported")
        if document.page_count == 0:
            raise ValueError("PDF document has 0 pages")
        if document.page_count > max_pages:
            raise ValueError(f"PDF exceeds maximum supported page count ({document.page_count} > {max_pages})")
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(dpi=render_dpi, alpha=False)
            payloads.append(PdfPagePayload(page_number=page_index + 1, png_bytes=bytes(pixmap.tobytes("png"))))
    return payloads
