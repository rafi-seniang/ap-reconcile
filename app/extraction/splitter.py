"""PDF page splitting (1-page native sub-PDFs) and PNG rendering via PyMuPDF."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

_pymupdf: Any = pymupdf


@dataclass(frozen=True)
class PdfPagePayload:
    """One 1-based page from a supplier PDF batch with both native PDF bytes and rendered PNG."""

    page_number: int
    pdf_bytes: bytes
    png_bytes: bytes


def load_pdf_pages(path: Path | str, render_dpi: int = 200) -> list[PdfPagePayload]:
    """Split a PDF into single-page PDF bytes (for native multimodal PDF models) and PNGs (for vision/UI)."""
    pdf_path = Path(path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    payloads: list[PdfPagePayload] = []
    with _pymupdf.open(pdf_path) as doc:
        if doc.page_count == 0:
            raise ValueError(f"PDF file has 0 pages: {pdf_path}")
        for page_index in range(doc.page_count):
            single_doc = _pymupdf.open()
            single_doc.insert_pdf(doc, from_page=page_index, to_page=page_index)
            pdf_bytes = bytes(single_doc.tobytes())
            single_doc.close()

            page = doc.load_page(page_index)
            pix = page.get_pixmap(dpi=render_dpi, alpha=False)
            png_bytes = bytes(pix.tobytes("png"))
            payloads.append(
                PdfPagePayload(page_number=page_index + 1, pdf_bytes=pdf_bytes, png_bytes=png_bytes)
            )
    return payloads


def render_single_page_png(path: Path | str, page_number: int, dpi: int = 200) -> bytes:
    """Render one 1-based page of a PDF as PNG bytes."""
    with _pymupdf.open(path) as doc:
        if page_number < 1 or page_number > doc.page_count:
            raise IndexError(f"Page {page_number} out of range (1..{doc.page_count})")
        pix = doc.load_page(page_number - 1).get_pixmap(dpi=dpi, alpha=False)
        return bytes(pix.tobytes("png"))
