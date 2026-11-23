"""Deterministic reconciliation engine: matching, duplicate detection, R5-R8 classification, and batch control totals."""

from decimal import Decimal

from app.engine.explanations import build_explanation_and_action
from app.engine.line_matcher import match_document_lines
from app.schemas.erp import ErpDocument, ErpWorkbook
from app.schemas.invoice import ExtractionUsage, ParsedDocument
from app.schemas.reconciliation import (
    AuditRef,
    BatchControlTotals,
    DocStatus,
    LineDiscrepancy,
    ReconciledDocument,
    ReconciliationReport,
)


def _get_pdf_document_sign(document: ParsedDocument) -> Decimal:
    return Decimal("-1") if document.document_type == "credit_note" else Decimal("1")


def _apply_document_sign(amount: Decimal | None, sign: Decimal) -> Decimal:
    return abs(amount) * sign if amount is not None else Decimal("0")


def _append_pdf_audit_refs(document: ParsedDocument, document_key: str, audit_trail: list[AuditRef]) -> None:
    sign = _get_pdf_document_sign(document)
    raw_doc_number = document.doc_number_raw or document_key
    page_location = (
        f"PDF Page {document.pages[0]}"
        if len(document.pages) == 1
        else f"PDF Pages {document.pages[0]}-{document.pages[-1]}"
    )
    for field_name, amount in (
        ("Subtotal", document.subtotal),
        ("DPP", document.dpp),
        ("PPN", document.ppn),
        ("Grand Total", document.grand_total),
    ):
        if amount is not None:
            audit_trail.append(
                AuditRef(
                    doc_number_norm=document_key,
                    doc_number_raw=raw_doc_number,
                    source_side="PDF",
                    level="HEADER",
                    location=page_location,
                    field_or_item=field_name,
                    raw_value=f"{abs(amount):,.0f}",
                    numeric_value=_apply_document_sign(amount, sign),
                )
            )
    if document.po_number_raw or document.reference_doc_raw:
        audit_trail.append(
            AuditRef(
                doc_number_norm=document_key,
                doc_number_raw=raw_doc_number,
                source_side="PDF",
                level="HEADER",
                location=page_location,
                field_or_item="PO No" if document.document_type == "invoice" else "Reference Invoice",
                raw_value=document.po_number_raw or document.reference_doc_raw or "",
            )
        )
    for line in document.lines:
        audit_trail.append(
            AuditRef(
                doc_number_norm=document_key,
                doc_number_raw=raw_doc_number,
                source_side="PDF",
                level="LINE",
                location=f"PDF Page {line.source_page} Line {line.line_number}",
                field_or_item=f"{line.item_code_raw or 'NO_SKU'} | {line.description}",
                raw_value=(
                    f"qty={line.quantity}, price={line.unit_price}, disc={line.discount_pct}%, "
                    f"amount={line.line_amount}"
                ),
                numeric_value=_apply_document_sign(line.line_amount, sign),
            )
        )


def _append_erp_audit_refs(document: ErpDocument, audit_trail: list[AuditRef]) -> None:
    header_location = f"{document.sheet}!R{document.row} ({document.entry_id})"
    for field_name, amount in (
        ("Subtotal", document.subtotal_signed),
        ("DPP", document.dpp_signed),
        ("PPN", document.ppn_signed),
        ("Grand Total", document.grand_total_signed),
    ):
        audit_trail.append(
            AuditRef(
                doc_number_norm=document.doc_number_norm,
                doc_number_raw=document.doc_number_raw,
                source_side="ERP",
                level="HEADER",
                location=header_location,
                field_or_item=field_name,
                raw_value=f"{amount:,.0f}",
                numeric_value=amount,
            )
        )
    audit_trail.append(
        AuditRef(
            doc_number_norm=document.doc_number_norm,
            doc_number_raw=document.doc_number_raw,
            source_side="ERP",
            level="HEADER",
            location=header_location,
            field_or_item="PO No" if document.document_type == "invoice" else "Original Invoice",
            raw_value=document.po_number_raw or document.original_invoice_raw or "",
        )
    )
    for line in document.lines:
        audit_trail.append(
            AuditRef(
                doc_number_norm=document.doc_number_norm,
                doc_number_raw=document.doc_number_raw,
                source_side="ERP",
                level="LINE",
                location=f"{line.sheet}!R{line.row} ({line.entry_id} L{line.line_number})",
                field_or_item=f"{line.item_code_raw or 'NO_SKU'} | {line.description}",
                raw_value=(
                    f"qty={line.quantity_signed}, price={line.unit_price}, disc={line.discount_pct}%, "
                    f"amount={line.line_amount_signed}"
                ),
                numeric_value=line.line_amount_signed,
            )
        )


def _index_batch_documents(
    pdf_documents: list[ParsedDocument],
    erp_workbook: ErpWorkbook,
) -> tuple[list[str], dict[str, list[ParsedDocument]], dict[str, list[ErpDocument]], list[AuditRef]]:
    """Index PDF and ERP documents by normalized document key in O(n) while capturing full audit trails."""
    ordered_keys: list[str] = []
    seen_keys: set[str] = set()
    pdf_by_key: dict[str, list[ParsedDocument]] = {}
    erp_by_key: dict[str, list[ErpDocument]] = {}
    audit_trail: list[AuditRef] = []

    def register_key(key: str) -> None:
        if key not in seen_keys:
            seen_keys.add(key)
            ordered_keys.append(key)

    for pdf_doc in pdf_documents:
        key = pdf_doc.doc_number_norm or f"UNREADABLE_P{'_'.join(str(p) for p in pdf_doc.pages)}"
        register_key(key)
        pdf_by_key.setdefault(key, []).append(pdf_doc)
        _append_pdf_audit_refs(pdf_doc, key, audit_trail)

    for erp_doc in erp_workbook.documents:
        register_key(erp_doc.doc_number_norm)
        erp_by_key.setdefault(erp_doc.doc_number_norm, []).append(erp_doc)
        _append_erp_audit_refs(erp_doc, audit_trail)

    return ordered_keys, pdf_by_key, erp_by_key, audit_trail


def _collect_pair_review_reasons(
    pdf_doc: ParsedDocument,
    erp_doc: ErpDocument,
    *,
    tax_base_error_as_needs_review: bool,
    reason_codes: list[str],
) -> tuple[list[str], bool]:
    check_by_name = {check.name: check for check in pdf_doc.checks}
    needs_review_reasons: list[str] = []

    has_doc_num_ok = check_by_name["has_doc_number"].passed if "has_doc_number" in check_by_name else True
    has_lines_ok = check_by_name["has_lines"].passed if "has_lines" in check_by_name else True
    if pdf_doc.has_parse_errors or not has_doc_num_ok or not has_lines_ok:
        needs_review_reasons.append("EXTRACTION_PARSE_ERROR")
    if any(not check.passed and check.name.startswith("line_") for check in pdf_doc.checks):
        needs_review_reasons.append("PDF_INTERNAL_LINE_ERROR")
    if "subtotal_adds_up" in check_by_name and not check_by_name["subtotal_adds_up"].passed:
        needs_review_reasons.append("PDF_INTERNAL_SUBTOTAL_ERROR")
    if "total_adds_up" in check_by_name and not check_by_name["total_adds_up"].passed:
        needs_review_reasons.append("PDF_INTERNAL_TOTAL_ERROR")

    dpp_check = check_by_name.get("dpp_matches_r3_11_12")
    has_wrong_dpp_base = False
    if dpp_check is not None and not dpp_check.passed:
        has_wrong_dpp_base = True
        is_full_base = (
            pdf_doc.dpp is not None
            and pdf_doc.subtotal is not None
            and abs(pdf_doc.dpp) == abs(pdf_doc.subtotal)
        )
        dpp_code = "TAX_BASE_WRONG_DPP_NOT_11_12" if is_full_base else "PDF_DPP_RULE_ERROR"
        (needs_review_reasons if tax_base_error_as_needs_review else reason_codes).append(dpp_code)

    if pdf_doc.currency != erp_doc.currency:
        needs_review_reasons.append("CURRENCY_MISMATCH")
    if pdf_doc.document_type != erp_doc.document_type:
        needs_review_reasons.append("DOC_TYPE_MISMATCH")
    if (
        pdf_doc.document_type == "invoice"
        and pdf_doc.po_number_norm
        and erp_doc.po_number_norm
        and pdf_doc.po_number_norm != erp_doc.po_number_norm
    ):
        needs_review_reasons.append("PO_NUMBER_MISMATCH")
    if (
        pdf_doc.document_type == "credit_note"
        and pdf_doc.reference_doc_norm
        and erp_doc.original_invoice_norm
        and pdf_doc.reference_doc_norm != erp_doc.original_invoice_norm
    ):
        needs_review_reasons.append("CN_REFERENCE_MISMATCH")

    return needs_review_reasons, has_wrong_dpp_base


def _classify_matched_pair(
    pdf_doc: ParsedDocument,
    erp_doc: ErpDocument,
    *,
    difference_signed: Decimal,
    pdf_ppn_signed: Decimal,
    erp_ppn_signed: Decimal,
    tolerance: Decimal,
    tax_base_error_as_needs_review: bool,
    reason_codes: list[str],
) -> tuple[DocStatus, list[LineDiscrepancy]]:
    """Classify a 1-to-1 matched PDF and ERP document pair into its exact R8 status and line discrepancies."""
    line_discrepancies = match_document_lines(pdf_doc, erp_doc)
    if pdf_doc.doc_number_raw and pdf_doc.doc_number_raw.strip() != erp_doc.doc_number_raw.strip():
        reason_codes.append("DOC_NO_FORMAT_NORMALISED")
    if pdf_doc.document_type == "credit_note":
        reason_codes.append("CREDIT_NOTE_SIGN_NORMALISED")

    needs_review_reasons, has_wrong_dpp_base = _collect_pair_review_reasons(
        pdf_doc,
        erp_doc,
        tax_base_error_as_needs_review=tax_base_error_as_needs_review,
        reason_codes=reason_codes,
    )
    if needs_review_reasons:
        reason_codes.extend(needs_review_reasons)
        return DocStatus.NEEDS_REVIEW, line_discrepancies
    if difference_signed == Decimal("0") and not line_discrepancies:
        return DocStatus.MATCHED, line_discrepancies
    if abs(difference_signed) <= tolerance and not line_discrepancies and not has_wrong_dpp_base:
        reason_codes.append(
            "PPN_ROUNDING_WITHIN_TOLERANCE"
            if pdf_ppn_signed != erp_ppn_signed
            else "TOTAL_ROUNDING_WITHIN_TOLERANCE"
        )
        return DocStatus.MATCHED_WITHIN_TOLERANCE, line_discrepancies

    seen_reasons = set(reason_codes)
    for discrepancy in line_discrepancies:
        for reason_part in discrepancy.discrepancy_type.split("+"):
            if reason_part not in seen_reasons:
                seen_reasons.add(reason_part)
                reason_codes.append(reason_part)
    if (
        not line_discrepancies
        and pdf_ppn_signed != erp_ppn_signed
        and "TAX_BASE_WRONG_DPP_NOT_11_12" not in seen_reasons
    ):
        reason_codes.append("TAX_AMOUNT_MISMATCH")
    return DocStatus.AMOUNT_MISMATCH, line_discrepancies


def _resolve_group_status_and_discrepancies(
    pdf_group: list[ParsedDocument],
    erp_group: list[ErpDocument],
    *,
    difference_signed: Decimal,
    pdf_ppn_signed: Decimal,
    erp_ppn_signed: Decimal,
    tolerance: Decimal,
    tax_base_error_as_needs_review: bool,
    reason_codes: list[str],
) -> tuple[DocStatus, list[LineDiscrepancy]]:
    if len(pdf_group) > 1:
        reason_codes.append("DUPLICATE_PDF_SUBMISSION")
        if len(erp_group) > 1:
            reason_codes.append("DUPLICATE_ERP_POSTING")
        elif not erp_group:
            reason_codes.append("MISSING_IN_ERP")
        return DocStatus.DUPLICATE_IN_PDF, []
    if len(erp_group) > 1:
        reason_codes.append("DUPLICATE_ERP_POSTING")
        if not pdf_group:
            reason_codes.append("MISSING_IN_PDF")
        return DocStatus.DUPLICATE_IN_ERP, []
    if len(pdf_group) == 1 and not erp_group:
        is_unreadable = (
            pdf_group[0].doc_number_norm is None
            or pdf_group[0].has_parse_errors
            or pdf_group[0].grand_total is None
        )
        reason_codes.append("EXTRACTION_PARSE_ERROR" if is_unreadable else "NOT_BOOKED_IN_ERP")
        return (DocStatus.NEEDS_REVIEW if is_unreadable else DocStatus.MISSING_IN_ERP), []
    if not pdf_group and len(erp_group) == 1:
        reason_codes.append("NOT_PRESENT_IN_PDF")
        if erp_group[0].doc_status.lower() == "parked":
            reason_codes.append("ERP_STATUS_PARKED")
        return DocStatus.MISSING_IN_PDF, []
    return _classify_matched_pair(
        pdf_group[0],
        erp_group[0],
        difference_signed=difference_signed,
        pdf_ppn_signed=pdf_ppn_signed,
        erp_ppn_signed=erp_ppn_signed,
        tolerance=tolerance,
        tax_base_error_as_needs_review=tax_base_error_as_needs_review,
        reason_codes=reason_codes,
    )


def _sum_group_amounts(
    pdf_group: list[ParsedDocument],
    erp_group: list[ErpDocument],
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    pdf_subtotal = sum((_apply_document_sign(doc.subtotal, _get_pdf_document_sign(doc)) for doc in pdf_group), Decimal("0"))
    pdf_ppn = sum((_apply_document_sign(doc.ppn, _get_pdf_document_sign(doc)) for doc in pdf_group), Decimal("0"))
    pdf_total = sum((_apply_document_sign(doc.grand_total, _get_pdf_document_sign(doc)) for doc in pdf_group), Decimal("0"))
    erp_subtotal = sum((doc.subtotal_signed for doc in erp_group), Decimal("0"))
    erp_ppn = sum((doc.ppn_signed for doc in erp_group), Decimal("0"))
    erp_total = sum((doc.grand_total_signed for doc in erp_group), Decimal("0"))
    return pdf_subtotal, pdf_ppn, pdf_total, erp_subtotal, erp_ppn, erp_total, pdf_total - erp_total


def _extract_group_metadata(
    pdf_group: list[ParsedDocument],
    erp_group: list[ErpDocument],
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    pdf_raw = pdf_group[0].doc_number_raw if pdf_group else None
    erp_raw = erp_group[0].doc_number_raw if erp_group else None
    erp_status = ", ".join(dict.fromkeys(doc.doc_status for doc in erp_group)) if erp_group else None
    pdf_ref = (
        (pdf_group[0].po_number_raw if pdf_group[0].document_type == "invoice" else pdf_group[0].reference_doc_raw)
        if pdf_group
        else None
    )
    erp_ref = (
        (
            erp_group[0].po_number_raw
            if erp_group[0].document_type == "invoice"
            else erp_group[0].original_invoice_raw
        )
        if erp_group
        else None
    )
    return pdf_raw, erp_raw, erp_status, pdf_ref, erp_ref


def _reconcile_document_group(
    document_key: str,
    pdf_group: list[ParsedDocument],
    erp_group: list[ErpDocument],
    *,
    tolerance: Decimal,
    tax_base_error_as_needs_review: bool,
) -> tuple[ReconciledDocument, list[LineDiscrepancy]]:
    """Reconcile all PDF and ERP occurrences sharing a single normalized document key."""
    pdf_pages = [page for doc in pdf_group for page in doc.pages]
    erp_refs = [doc.audit_ref for doc in erp_group]
    pdf_sub, pdf_ppn, pdf_tot, erp_sub, erp_ppn, erp_tot, difference = _sum_group_amounts(pdf_group, erp_group)
    pdf_raw, erp_raw, erp_status_text, pdf_po_or_ref, erp_po_or_ref = _extract_group_metadata(
        pdf_group, erp_group
    )
    document_type = pdf_group[0].document_type if pdf_group else erp_group[0].document_type

    reason_codes: list[str] = []
    checks = pdf_group[0].checks if len(pdf_group) == 1 else [c for doc in pdf_group for c in doc.checks]
    failed_check_details = [f"{check.name}: {check.detail}" for check in checks if not check.passed]
    status, line_discrepancies = _resolve_group_status_and_discrepancies(
        pdf_group,
        erp_group,
        difference_signed=difference,
        pdf_ppn_signed=pdf_ppn,
        erp_ppn_signed=erp_ppn,
        tolerance=tolerance,
        tax_base_error_as_needs_review=tax_base_error_as_needs_review,
        reason_codes=reason_codes,
    )
    explanation, action = build_explanation_and_action(
        doc_label=pdf_raw or erp_raw or document_key,
        status=status,
        reason_codes=reason_codes,
        pdf_pages=pdf_pages,
        erp_refs=erp_refs,
        erp_doc_status=erp_status_text,
        pdf_total_signed=pdf_tot,
        erp_total_signed=erp_tot,
        difference_signed=difference,
        pdf_po_or_ref=pdf_po_or_ref,
        erp_po_or_ref=erp_po_or_ref,
        line_discrepancies=line_discrepancies,
        failed_check_details=failed_check_details,
    )

    return (
        ReconciledDocument(
            doc_number_norm=document_key,
            pdf_doc_number_raw=pdf_raw,
            erp_doc_number_raw=erp_raw,
            document_type=document_type,
            pdf_pages=pdf_pages,
            erp_refs=erp_refs,
            erp_doc_status=erp_status_text,
            pdf_po_or_ref=pdf_po_or_ref,
            erp_po_or_ref=erp_po_or_ref,
            pdf_subtotal_signed=pdf_sub,
            erp_subtotal_signed=erp_sub,
            pdf_ppn_signed=pdf_ppn,
            erp_ppn_signed=erp_ppn,
            pdf_total_signed=pdf_tot,
            erp_total_signed=erp_tot,
            difference_signed=difference,
            status=status,
            reason_codes=reason_codes,
            checks=checks,
            line_discrepancies=line_discrepancies,
            plain_explanation=explanation,
            suggested_action=action,
        ),
        line_discrepancies,
    )


def _compute_batch_control_totals(
    pdf_documents: list[ParsedDocument],
    erp_workbook: ErpWorkbook,
    reconciled_documents: list[ReconciledDocument],
) -> BatchControlTotals:
    raw_pdf_invoices = Decimal("0")
    raw_pdf_credit_notes = Decimal("0")
    for document in pdf_documents:
        if document.document_type == "invoice":
            raw_pdf_invoices += _apply_document_sign(document.grand_total, Decimal("1"))
        else:
            raw_pdf_credit_notes += _apply_document_sign(document.grand_total, Decimal("-1"))
    raw_pdf_net = raw_pdf_invoices + raw_pdf_credit_notes

    raw_erp_invoices = Decimal("0")
    raw_erp_credit_notes = Decimal("0")
    erp_posted_net = Decimal("0")
    erp_parked_net = Decimal("0")
    for erp_doc in erp_workbook.documents:
        if erp_doc.document_type == "invoice":
            raw_erp_invoices += erp_doc.grand_total_signed
        else:
            raw_erp_credit_notes += erp_doc.grand_total_signed
        if erp_doc.doc_status.lower() == "posted":
            erp_posted_net += erp_doc.grand_total_signed
        else:
            erp_parked_net += erp_doc.grand_total_signed
    raw_erp_net = raw_erp_invoices + raw_erp_credit_notes

    explained_by_status: dict[str, Decimal] = {}
    status_counts: dict[str, int] = {}
    total_explained = Decimal("0")
    for reconciled_doc in reconciled_documents:
        status_name = reconciled_doc.status.value
        status_counts[status_name] = status_counts.get(status_name, 0) + 1
        explained_by_status[status_name] = (
            explained_by_status.get(status_name, Decimal("0")) + reconciled_doc.difference_signed
        )
        total_explained += reconciled_doc.difference_signed

    net_difference = raw_pdf_net - raw_erp_net
    return BatchControlTotals(
        pdf_document_count=len(pdf_documents),
        erp_entry_count=len(erp_workbook.documents),
        reconciled_unique_docs=len(reconciled_documents),
        raw_pdf_invoices_total=raw_pdf_invoices,
        raw_pdf_credit_notes_total=raw_pdf_credit_notes,
        raw_pdf_net_total=raw_pdf_net,
        raw_erp_invoices_total=raw_erp_invoices,
        raw_erp_credit_notes_total=raw_erp_credit_notes,
        raw_erp_net_total=raw_erp_net,
        erp_posted_net_total=erp_posted_net,
        erp_parked_net_total=erp_parked_net,
        net_difference=net_difference,
        explained_by_status=explained_by_status,
        status_counts=status_counts,
        total_explained_difference=total_explained,
        unexplained_variance=net_difference - total_explained,
    )


def reconcile_batch(
    pdf_documents: list[ParsedDocument],
    erp_workbook: ErpWorkbook,
    *,
    pdf_filename: str,
    erp_filename: str,
    tolerance_idr: int = 10,
    tax_base_error_as_needs_review: bool = True,
    usage: ExtractionUsage | None = None,
) -> ReconciliationReport:
    """Reconcile all parsed PDF supplier documents against all ERP entries with exact batch tie-out."""
    tolerance = Decimal(tolerance_idr)
    ordered_keys, pdf_by_key, erp_by_key, audit_trail = _index_batch_documents(pdf_documents, erp_workbook)

    reconciled_documents: list[ReconciledDocument] = []
    all_line_discrepancies: list[LineDiscrepancy] = []
    for key in ordered_keys:
        reconciled_doc, doc_discrepancies = _reconcile_document_group(
            key,
            pdf_by_key.get(key, []),
            erp_by_key.get(key, []),
            tolerance=tolerance,
            tax_base_error_as_needs_review=tax_base_error_as_needs_review,
        )
        reconciled_documents.append(reconciled_doc)
        all_line_discrepancies.extend(doc_discrepancies)

    control_totals = _compute_batch_control_totals(pdf_documents, erp_workbook, reconciled_documents)
    return ReconciliationReport(
        pdf_filename=pdf_filename,
        erp_filename=erp_filename,
        export_info=erp_workbook.export_info,
        documents=reconciled_documents,
        line_discrepancies=all_line_discrepancies,
        audit_trail=audit_trail,
        control_totals=control_totals,
        usage=usage,
    )
