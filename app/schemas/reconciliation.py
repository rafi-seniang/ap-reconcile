"""Reconciliation output schemas, R8 statuses, line discrepancies, control totals, and audit trail."""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.invoice import CheckResult, DocumentType, ExtractionUsage


class DocStatus(StrEnum):
    """Exact eight document statuses specified by business rule R8."""

    MATCHED = "MATCHED"
    MATCHED_WITHIN_TOLERANCE = "MATCHED_WITHIN_TOLERANCE"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    MISSING_IN_ERP = "MISSING_IN_ERP"
    MISSING_IN_PDF = "MISSING_IN_PDF"
    DUPLICATE_IN_ERP = "DUPLICATE_IN_ERP"
    DUPLICATE_IN_PDF = "DUPLICATE_IN_PDF"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class LineDiscrepancy(BaseModel):
    doc_number_raw: str
    doc_number_norm: str
    discrepancy_type: str
    item_code: str | None
    pdf_page: int | None = None
    pdf_line_number: int | None = None
    pdf_description: str | None = None
    pdf_qty: Decimal | None = None
    pdf_unit_price: Decimal | None = None
    pdf_discount_pct: Decimal | None = None
    pdf_line_amount: Decimal | None = None
    erp_ref: str | None = None
    erp_description: str | None = None
    erp_qty: Decimal | None = None
    erp_unit_price: Decimal | None = None
    erp_discount_pct: Decimal | None = None
    erp_line_amount: Decimal | None = None
    amount_difference: Decimal
    detail: str


class AuditRef(BaseModel):
    """Traceability record linking every reconciled figure to a PDF page or ERP sheet and row."""

    doc_number_norm: str
    doc_number_raw: str
    source_side: str
    level: str
    location: str
    field_or_item: str
    raw_value: str
    numeric_value: Decimal | None = None


class ReconciledDocument(BaseModel):
    doc_number_norm: str
    pdf_doc_number_raw: str | None = None
    erp_doc_number_raw: str | None = None
    document_type: DocumentType
    pdf_pages: list[int] = Field(default_factory=list)
    erp_refs: list[str] = Field(default_factory=list)
    erp_doc_status: str | None = None
    pdf_po_or_ref: str | None = None
    erp_po_or_ref: str | None = None
    pdf_subtotal_signed: Decimal = Decimal("0")
    erp_subtotal_signed: Decimal = Decimal("0")
    pdf_ppn_signed: Decimal = Decimal("0")
    erp_ppn_signed: Decimal = Decimal("0")
    pdf_total_signed: Decimal = Decimal("0")
    erp_total_signed: Decimal = Decimal("0")
    difference_signed: Decimal = Decimal("0")
    status: DocStatus
    reason_codes: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    line_discrepancies: list[LineDiscrepancy] = Field(default_factory=list)
    plain_explanation: str
    suggested_action: str


class BatchControlTotals(BaseModel):
    """Whole-batch tie-out proving every rupiah of difference between PDF and ERP is explained."""

    pdf_document_count: int
    erp_entry_count: int
    reconciled_unique_docs: int
    raw_pdf_invoices_total: Decimal
    raw_pdf_credit_notes_total: Decimal
    raw_pdf_net_total: Decimal
    raw_erp_invoices_total: Decimal
    raw_erp_credit_notes_total: Decimal
    raw_erp_net_total: Decimal
    erp_posted_net_total: Decimal
    erp_parked_net_total: Decimal
    net_difference: Decimal
    explained_by_status: dict[str, Decimal]
    status_counts: dict[str, int]
    total_explained_difference: Decimal
    unexplained_variance: Decimal


class ReconciliationReport(BaseModel):
    pdf_filename: str
    erp_filename: str
    export_info: dict[str, str] = Field(default_factory=dict)
    documents: list[ReconciledDocument]
    line_discrepancies: list[LineDiscrepancy]
    audit_trail: list[AuditRef]
    control_totals: BatchControlTotals
    usage: ExtractionUsage | None = None
