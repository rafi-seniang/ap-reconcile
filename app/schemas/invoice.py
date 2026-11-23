"""Schemas for verbatim LLM extraction, assembled multi-page PDF documents, and parsed records."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

DocumentType = Literal["invoice", "credit_note"]

AS_PRINTED = "copied verbatim as printed with punctuation and separators; null when absent or illegible"


class CheckResult(BaseModel):
    """Result of one deterministic verification rule on an extracted or ERP document."""

    name: str
    passed: bool
    detail: str = ""


class PrintedLine(BaseModel):
    """One line item copied verbatim from a supplier invoice or credit note page."""

    line_number_raw: str | None = Field(None, description=f"Row index number {AS_PRINTED}")
    item_code: str | None = Field(None, description=f"SKU or item code {AS_PRINTED}")
    description: str = Field(..., description="Item description copied verbatim")
    quantity_raw: str = Field(..., description=f"Quantity {AS_PRINTED}")
    uom_raw: str | None = Field(None, description=f"Unit of measure (e.g. rim, box, pcs) {AS_PRINTED}")
    unit_price_raw: str = Field(..., description=f"Unit price before discount {AS_PRINTED}")
    discount_pct_raw: str | None = Field(
        None,
        description="Discount percentage as printed (e.g. '10' or '2,5'); null when the column is blank",
    )
    line_amount_raw: str = Field(..., description=f"Extended line total amount {AS_PRINTED}")


class PrintedPage(BaseModel):
    """One PDF page copied verbatim by the multimodal model without arithmetic edits."""

    is_continuation: bool = Field(
        False,
        description="True when this page continues a line-item table from the previous page without starting a new invoice",
    )
    document_type: DocumentType = Field(
        "invoice",
        description="'credit_note' when titled Nota Kredit / Credit Note; otherwise 'invoice'",
    )
    supplier_name: str | None = Field(
        None, description="Seller name from letterhead; null on continuation pages"
    )
    doc_number_raw: str | None = Field(
        None,
        description="Invoice or credit note number verbatim with separators; null if not printed on this page",
    )
    doc_date_raw: str | None = Field(None, description=f"Document date {AS_PRINTED}")
    due_date_raw: str | None = Field(None, description=f"Payment due date {AS_PRINTED}")
    po_number_raw: str | None = Field(None, description=f"Purchase order number (No. PO) {AS_PRINTED}")
    reference_doc_raw: str | None = Field(
        None,
        description=f"Original invoice referenced by a credit note (Referensi / Reference) {AS_PRINTED}",
    )
    currency: str | None = Field(None, description="Currency code or symbol (e.g. IDR, Rp); null if absent")
    subtotal_raw: str | None = Field(None, description=f"Printed Subtotal before tax {AS_PRINTED}")
    dpp_raw: str | None = Field(None, description=f"Printed DPP or DPP Nilai Lain tax base {AS_PRINTED}")
    ppn_raw: str | None = Field(None, description=f"Printed PPN / VAT amount {AS_PRINTED}")
    grand_total_raw: str | None = Field(None, description=f"Printed final TOTAL amount {AS_PRINTED}")
    lines: list[PrintedLine] = Field(
        default_factory=list,
        description="Every line item printed on this page, in printed order",
    )


class ExtractedPage(BaseModel):
    """A PrintedPage bound to its 1-based source PDF page number assigned by code."""

    page_number: int = Field(..., ge=1)
    printed: PrintedPage


class AssembledLine(BaseModel):
    source_page: int = Field(..., ge=1)
    printed: PrintedLine


class AssembledDocument(BaseModel):
    """One logical supplier invoice or credit note assembled across one or more PDF pages."""

    pages: list[int]
    document_type: DocumentType
    supplier_name: str | None
    doc_number_raw: str | None
    doc_date_raw: str | None
    due_date_raw: str | None
    po_number_raw: str | None
    reference_doc_raw: str | None
    currency: str | None
    subtotal_raw: str | None
    dpp_raw: str | None
    ppn_raw: str | None
    grand_total_raw: str | None
    lines: list[AssembledLine]


class ParsedLine(BaseModel):
    source_page: int
    line_number: int
    item_code_raw: str | None
    item_code_norm: str | None
    description: str
    quantity: Decimal | None
    uom_raw: str | None
    unit_price: Decimal | None
    discount_pct: Decimal | None
    line_amount: Decimal | None


class ParsedDocument(BaseModel):
    """Typed document produced deterministically from AssembledDocument with explicit parse and math checks."""

    pages: list[int]
    document_type: DocumentType
    supplier_name: str | None
    doc_number_raw: str | None
    doc_number_norm: str | None
    doc_date: date | None
    due_date: date | None
    po_number_raw: str | None
    po_number_norm: str | None
    reference_doc_raw: str | None
    reference_doc_norm: str | None
    currency: str | None
    subtotal: Decimal | None
    dpp: Decimal | None
    ppn: Decimal | None
    grand_total: Decimal | None
    lines: list[ParsedLine]
    checks: list[CheckResult] = Field(default_factory=list)

    @property
    def has_parse_errors(self) -> bool:
        return any(not check.passed and check.name.startswith("parse_") for check in self.checks)


class ExtractionUsage(BaseModel):
    """Token, retry, cache, latency, and optional cost accounting for one batch extraction."""

    provider: str
    model: str
    retry_model: str | None = None
    primary_calls: int = 0
    retry_calls: int = 0
    cached_pages: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    retry_input_tokens: int = 0
    retry_output_tokens: int = 0
    estimated_cost_usd: float | None = None
    latency_ms: float = 0.0
