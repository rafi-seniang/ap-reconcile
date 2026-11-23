"""Schemas and structured validation errors for the internal ERP AP Excel export."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.extraction.schemas import DocumentType


class ErpValidationIssue(BaseModel):
    """One contract or data error found in the ERP workbook, pinpointed for an AP clerk to fix."""

    sheet: str
    row: int | None = None
    column: str | None = None
    raw_value: str | None = None
    message: str

    def format_human(self) -> str:
        location = f"{self.sheet}"
        if self.row is not None:
            location += f"!Row {self.row}"
        if self.column:
            location += f", Column '{self.column}'"
        val = f" (value={self.raw_value!r})" if self.raw_value is not None else ""
        return f"{location}{val}: {self.message}"


class ErpValidationError(ValueError):
    """Raised when the ERP Excel export violates the expected internal schema contract."""

    def __init__(self, issues: list[ErpValidationIssue]) -> None:
        self.issues = issues
        summary = f"ERP workbook validation failed with {len(issues)} issue(s):\n" + "\n".join(
            f"  - {issue.format_human()}" for issue in issues
        )
        super().__init__(summary)


class ErpLine(BaseModel):
    entry_id: str
    sheet: str
    row: int
    line_number: int
    item_code_raw: str | None
    item_code_norm: str | None
    description: str
    quantity_signed: Decimal
    quantity_abs: Decimal
    uom_raw: str | None
    unit_price: Decimal
    discount_pct: Decimal
    line_amount_signed: Decimal
    line_amount_abs: Decimal


class ErpDocument(BaseModel):
    """One invoice or credit note header from ERP with its linked AP_Lines and exact Sheet!Row coordinates."""

    entry_id: str
    sheet: str
    row: int
    document_type: DocumentType
    vendor: str
    doc_number_raw: str
    doc_number_norm: str
    doc_date: date
    po_number_raw: str | None = None
    po_number_norm: str | None = None
    original_invoice_raw: str | None = None
    original_invoice_norm: str | None = None
    currency: str
    subtotal_signed: Decimal
    dpp_signed: Decimal
    ppn_signed: Decimal
    grand_total_signed: Decimal
    doc_status: str
    posted_by: str | None = None
    lines: list[ErpLine] = Field(default_factory=list)

    @property
    def audit_ref(self) -> str:
        return f"{self.sheet}!R{self.row} ({self.entry_id})"


class ErpWorkbook(BaseModel):
    documents: list[ErpDocument]
    export_info: dict[str, str] = Field(default_factory=dict)
