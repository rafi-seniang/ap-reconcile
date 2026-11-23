"""Deterministic reconciliation engine: matching, duplicate detection, R5-R8 classification, and batch control totals."""

from decimal import Decimal

from app.erp.schemas import ErpDocument, ErpWorkbook
from app.extraction.schemas import ExtractionUsage, ParsedDocument
from app.reconcile.explanations import build_explanation_and_action
from app.reconcile.line_matcher import match_document_lines
from app.reconcile.schemas import (
    AuditRef,
    BatchControlTotals,
    DocStatus,
    LineDiscrepancy,
    ReconciledDocument,
    ReconciliationReport,
)


def _pdf_sign(doc: ParsedDocument) -> Decimal:
    return Decimal("-1") if doc.document_type == "credit_note" else Decimal("1")


def _pdf_signed(amount: Decimal | None, sign: Decimal) -> Decimal:
    return abs(amount) * sign if amount is not None else Decimal("0")


def _append_pdf_audit_refs(doc: ParsedDocument, doc_key: str, refs: list[AuditRef]) -> None:
    sign = _pdf_sign(doc)
    raw_no = doc.doc_number_raw or doc_key
    page_loc = (
        f"PDF Page {doc.pages[0]}" if len(doc.pages) == 1 else f"PDF Pages {doc.pages[0]}-{doc.pages[-1]}"
    )
    for field_name, val in (
        ("Subtotal", doc.subtotal),
        ("DPP", doc.dpp),
        ("PPN", doc.ppn),
        ("Grand Total", doc.grand_total),
    ):
        if val is not None:
            refs.append(
                AuditRef(
                    doc_number_norm=doc_key,
                    doc_number_raw=raw_no,
                    source_side="PDF",
                    level="HEADER",
                    location=page_loc,
                    field_or_item=field_name,
                    raw_value=f"{abs(val):,.0f}",
                    numeric_value=_pdf_signed(val, sign),
                )
            )
    if doc.po_number_raw or doc.reference_doc_raw:
        refs.append(
            AuditRef(
                doc_number_norm=doc_key,
                doc_number_raw=raw_no,
                source_side="PDF",
                level="HEADER",
                location=page_loc,
                field_or_item="PO No" if doc.document_type == "invoice" else "Reference Invoice",
                raw_value=doc.po_number_raw or doc.reference_doc_raw or "",
            )
        )
    for line in doc.lines:
        refs.append(
            AuditRef(
                doc_number_norm=doc_key,
                doc_number_raw=raw_no,
                source_side="PDF",
                level="LINE",
                location=f"PDF Page {line.source_page} Line {line.line_number}",
                field_or_item=f"{line.item_code_raw or 'NO_SKU'} | {line.description}",
                raw_value=(
                    f"qty={line.quantity}, price={line.unit_price}, disc={line.discount_pct}%, "
                    f"amount={line.line_amount}"
                ),
                numeric_value=_pdf_signed(line.line_amount, sign),
            )
        )


def _append_erp_audit_refs(doc: ErpDocument, refs: list[AuditRef]) -> None:
    header_loc = f"{doc.sheet}!R{doc.row} ({doc.entry_id})"
    for field_name, val in (
        ("Subtotal", doc.subtotal_signed),
        ("DPP", doc.dpp_signed),
        ("PPN", doc.ppn_signed),
        ("Grand Total", doc.grand_total_signed),
    ):
        refs.append(
            AuditRef(
                doc_number_norm=doc.doc_number_norm,
                doc_number_raw=doc.doc_number_raw,
                source_side="ERP",
                level="HEADER",
                location=header_loc,
                field_or_item=field_name,
                raw_value=f"{val:,.0f}",
                numeric_value=val,
            )
        )
    refs.append(
        AuditRef(
            doc_number_norm=doc.doc_number_norm,
            doc_number_raw=doc.doc_number_raw,
            source_side="ERP",
            level="HEADER",
            location=header_loc,
            field_or_item="PO No" if doc.document_type == "invoice" else "Original Invoice",
            raw_value=doc.po_number_raw or doc.original_invoice_raw or "",
        )
    )
    for line in doc.lines:
        refs.append(
            AuditRef(
                doc_number_norm=doc.doc_number_norm,
                doc_number_raw=doc.doc_number_raw,
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
    """Index PDF and ERP documents by normalized document key while capturing full audit references."""
    ordered_keys: list[str] = []
    pdf_by_key: dict[str, list[ParsedDocument]] = {}
    erp_by_key: dict[str, list[ErpDocument]] = {}
    audit_trail: list[AuditRef] = []

    def register(key: str) -> None:
        if key not in ordered_keys:
            ordered_keys.append(key)

    for pdf_doc in pdf_documents:
        key = pdf_doc.doc_number_norm or f"UNREADABLE_P{'_'.join(str(p) for p in pdf_doc.pages)}"
        register(key)
        pdf_by_key.setdefault(key, []).append(pdf_doc)
        _append_pdf_audit_refs(pdf_doc, key, audit_trail)

    for erp_doc in erp_workbook.documents:
        register(erp_doc.doc_number_norm)
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
    check_map = {c.name: c for c in pdf_doc.checks}
    needs_review: list[str] = []

    has_doc_num_ok = check_map["has_doc_number"].passed if "has_doc_number" in check_map else True
    has_lines_ok = check_map["has_lines"].passed if "has_lines" in check_map else True
    if pdf_doc.has_parse_errors or not has_doc_num_ok or not has_lines_ok:
        needs_review.append("EXTRACTION_PARSE_ERROR")
    if any(not c.passed and c.name.startswith("line_") for c in pdf_doc.checks):
        needs_review.append("PDF_INTERNAL_LINE_ERROR")
    if "subtotal_adds_up" in check_map and not check_map["subtotal_adds_up"].passed:
        needs_review.append("PDF_INTERNAL_SUBTOTAL_ERROR")
    if "total_adds_up" in check_map and not check_map["total_adds_up"].passed:
        needs_review.append("PDF_INTERNAL_TOTAL_ERROR")

    dpp_check = check_map.get("dpp_matches_r3_11_12")
    has_wrong_dpp_base = False
    if dpp_check is not None and not dpp_check.passed:
        has_wrong_dpp_base = True
        is_full_base = (
            pdf_doc.dpp is not None
            and pdf_doc.subtotal is not None
            and abs(pdf_doc.dpp) == abs(pdf_doc.subtotal)
        )
        dpp_code = "TAX_BASE_WRONG_DPP_NOT_11_12" if is_full_base else "PDF_DPP_RULE_ERROR"
        (needs_review if tax_base_error_as_needs_review else reason_codes).append(dpp_code)

    if pdf_doc.currency != erp_doc.currency:
        needs_review.append("CURRENCY_MISMATCH")
    if pdf_doc.document_type != erp_doc.document_type:
        needs_review.append("DOC_TYPE_MISMATCH")
    if (
        pdf_doc.document_type == "invoice"
        and pdf_doc.po_number_norm
        and erp_doc.po_number_norm
        and pdf_doc.po_number_norm != erp_doc.po_number_norm
    ):
        needs_review.append("PO_NUMBER_MISMATCH")
    if (
        pdf_doc.document_type == "credit_note"
        and pdf_doc.reference_doc_norm
        and erp_doc.original_invoice_norm
        and pdf_doc.reference_doc_norm != erp_doc.original_invoice_norm
    ):
        needs_review.append("CN_REFERENCE_MISMATCH")

    return needs_review, has_wrong_dpp_base


def _classify_matched_pair(
    pdf_doc: ParsedDocument,
    erp_doc: ErpDocument,
    *,
    diff: Decimal,
    pdf_ppn: Decimal,
    erp_ppn: Decimal,
    tolerance: Decimal,
    tax_base_error_as_needs_review: bool,
    reason_codes: list[str],
) -> tuple[DocStatus, list[LineDiscrepancy]]:
    """Classify a 1-to-1 matched PDF and ERP document pair into its exact R8 status and line discrepancies."""
    doc_line_diffs = match_document_lines(pdf_doc, erp_doc)
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
        return DocStatus.NEEDS_REVIEW, doc_line_diffs
    if diff == Decimal("0") and not doc_line_diffs:
        return DocStatus.MATCHED, doc_line_diffs
    if abs(diff) <= tolerance and not doc_line_diffs and not has_wrong_dpp_base:
        reason_codes.append(
            "PPN_ROUNDING_WITHIN_TOLERANCE" if pdf_ppn != erp_ppn else "TOTAL_ROUNDING_WITHIN_TOLERANCE"
        )
        return DocStatus.MATCHED_WITHIN_TOLERANCE, doc_line_diffs

    for item in doc_line_diffs:
        for part in item.discrepancy_type.split("+"):
            if part not in reason_codes:
                reason_codes.append(part)
    if not doc_line_diffs and pdf_ppn != erp_ppn and "TAX_BASE_WRONG_DPP_NOT_11_12" not in reason_codes:
        reason_codes.append("TAX_AMOUNT_MISMATCH")
    return DocStatus.AMOUNT_MISMATCH, doc_line_diffs


def _resolve_status_and_diffs(
    p_list: list[ParsedDocument],
    e_list: list[ErpDocument],
    *,
    diff: Decimal,
    pdf_ppn: Decimal,
    erp_ppn: Decimal,
    tolerance: Decimal,
    tax_base_error_as_needs_review: bool,
    reason_codes: list[str],
) -> tuple[DocStatus, list[LineDiscrepancy]]:
    if len(p_list) > 1:
        reason_codes.append("DUPLICATE_PDF_SUBMISSION")
        if len(e_list) > 1:
            reason_codes.append("DUPLICATE_ERP_POSTING")
        elif not e_list:
            reason_codes.append("MISSING_IN_ERP")
        return DocStatus.DUPLICATE_IN_PDF, []
    if len(e_list) > 1:
        reason_codes.append("DUPLICATE_ERP_POSTING")
        if not p_list:
            reason_codes.append("MISSING_IN_PDF")
        return DocStatus.DUPLICATE_IN_ERP, []
    if len(p_list) == 1 and not e_list:
        unreadable = (
            p_list[0].doc_number_norm is None or p_list[0].has_parse_errors or p_list[0].grand_total is None
        )
        reason_codes.append("EXTRACTION_PARSE_ERROR" if unreadable else "NOT_BOOKED_IN_ERP")
        return (DocStatus.NEEDS_REVIEW if unreadable else DocStatus.MISSING_IN_ERP), []
    if not p_list and len(e_list) == 1:
        reason_codes.append("NOT_PRESENT_IN_PDF")
        if e_list[0].doc_status.lower() == "parked":
            reason_codes.append("ERP_STATUS_PARKED")
        return DocStatus.MISSING_IN_PDF, []
    return _classify_matched_pair(
        p_list[0],
        e_list[0],
        diff=diff,
        pdf_ppn=pdf_ppn,
        erp_ppn=erp_ppn,
        tolerance=tolerance,
        tax_base_error_as_needs_review=tax_base_error_as_needs_review,
        reason_codes=reason_codes,
    )


def _sum_group_amounts(
    p_list: list[ParsedDocument],
    e_list: list[ErpDocument],
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    pdf_sub = sum((_pdf_signed(doc.subtotal, _pdf_sign(doc)) for doc in p_list), Decimal("0"))
    pdf_ppn = sum((_pdf_signed(doc.ppn, _pdf_sign(doc)) for doc in p_list), Decimal("0"))
    pdf_tot = sum((_pdf_signed(doc.grand_total, _pdf_sign(doc)) for doc in p_list), Decimal("0"))
    erp_sub = sum((doc.subtotal_signed for doc in e_list), Decimal("0"))
    erp_ppn = sum((doc.ppn_signed for doc in e_list), Decimal("0"))
    erp_tot = sum((doc.grand_total_signed for doc in e_list), Decimal("0"))
    return pdf_sub, pdf_ppn, pdf_tot, erp_sub, erp_ppn, erp_tot, pdf_tot - erp_tot


def _extract_group_metadata(
    p_list: list[ParsedDocument],
    e_list: list[ErpDocument],
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    pdf_raw = p_list[0].doc_number_raw if p_list else None
    erp_raw = e_list[0].doc_number_raw if e_list else None
    erp_status = ", ".join(dict.fromkeys(doc.doc_status for doc in e_list)) if e_list else None
    pdf_ref = (
        (p_list[0].po_number_raw if p_list[0].document_type == "invoice" else p_list[0].reference_doc_raw)
        if p_list
        else None
    )
    erp_ref = (
        (e_list[0].po_number_raw if e_list[0].document_type == "invoice" else e_list[0].original_invoice_raw)
        if e_list
        else None
    )
    return pdf_raw, erp_raw, erp_status, pdf_ref, erp_ref


def _reconcile_document_group(
    key: str,
    p_list: list[ParsedDocument],
    e_list: list[ErpDocument],
    *,
    tolerance: Decimal,
    tax_base_error_as_needs_review: bool,
) -> tuple[ReconciledDocument, list[LineDiscrepancy]]:
    """Reconcile all PDF and ERP occurrences sharing a single normalized document key."""
    pdf_pages = [p for doc in p_list for p in doc.pages]
    erp_refs = [doc.audit_ref for doc in e_list]
    pdf_sub, pdf_ppn, pdf_tot, erp_sub, erp_ppn, erp_tot, diff = _sum_group_amounts(p_list, e_list)
    pdf_raw, erp_raw, erp_status_str, pdf_po_or_ref, erp_po_or_ref = _extract_group_metadata(p_list, e_list)
    doc_type = p_list[0].document_type if p_list else e_list[0].document_type

    reason_codes: list[str] = []
    checks = p_list[0].checks if len(p_list) == 1 else [c for doc in p_list for c in doc.checks]
    failed_details = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
    status, doc_line_diffs = _resolve_status_and_diffs(
        p_list,
        e_list,
        diff=diff,
        pdf_ppn=pdf_ppn,
        erp_ppn=erp_ppn,
        tolerance=tolerance,
        tax_base_error_as_needs_review=tax_base_error_as_needs_review,
        reason_codes=reason_codes,
    )
    explanation, action = build_explanation_and_action(
        doc_label=pdf_raw or erp_raw or key,
        status=status,
        reason_codes=reason_codes,
        pdf_pages=pdf_pages,
        erp_refs=erp_refs,
        erp_doc_status=erp_status_str,
        pdf_total_signed=pdf_tot,
        erp_total_signed=erp_tot,
        difference_signed=diff,
        pdf_po_or_ref=pdf_po_or_ref,
        erp_po_or_ref=erp_po_or_ref,
        line_discrepancies=doc_line_diffs,
        failed_check_details=failed_details,
    )

    return (
        ReconciledDocument(
            doc_number_norm=key,
            pdf_doc_number_raw=pdf_raw,
            erp_doc_number_raw=erp_raw,
            document_type=doc_type,
            pdf_pages=pdf_pages,
            erp_refs=erp_refs,
            erp_doc_status=erp_status_str,
            pdf_po_or_ref=pdf_po_or_ref,
            erp_po_or_ref=erp_po_or_ref,
            pdf_subtotal_signed=pdf_sub,
            erp_subtotal_signed=erp_sub,
            pdf_ppn_signed=pdf_ppn,
            erp_ppn_signed=erp_ppn,
            pdf_total_signed=pdf_tot,
            erp_total_signed=erp_tot,
            difference_signed=diff,
            status=status,
            reason_codes=reason_codes,
            checks=checks,
            line_discrepancies=doc_line_diffs,
            plain_explanation=explanation,
            suggested_action=action,
        ),
        doc_line_diffs,
    )


def _compute_control_totals(
    pdf_documents: list[ParsedDocument],
    erp_workbook: ErpWorkbook,
    reconciled_docs: list[ReconciledDocument],
) -> BatchControlTotals:
    raw_pdf_inv = sum(
        (_pdf_signed(d.grand_total, Decimal("1")) for d in pdf_documents if d.document_type == "invoice"),
        Decimal("0"),
    )
    raw_pdf_cn = sum(
        (
            _pdf_signed(d.grand_total, Decimal("-1"))
            for d in pdf_documents
            if d.document_type == "credit_note"
        ),
        Decimal("0"),
    )
    raw_pdf_net = raw_pdf_inv + raw_pdf_cn

    raw_erp_inv = sum(
        (d.grand_total_signed for d in erp_workbook.documents if d.document_type == "invoice"), Decimal("0")
    )
    raw_erp_cn = sum(
        (d.grand_total_signed for d in erp_workbook.documents if d.document_type == "credit_note"),
        Decimal("0"),
    )
    raw_erp_net = raw_erp_inv + raw_erp_cn
    erp_posted_net = sum(
        (d.grand_total_signed for d in erp_workbook.documents if d.doc_status.lower() == "posted"),
        Decimal("0"),
    )
    erp_parked_net = sum(
        (d.grand_total_signed for d in erp_workbook.documents if d.doc_status.lower() != "posted"),
        Decimal("0"),
    )

    explained_by_status: dict[str, Decimal] = {}
    status_counts: dict[str, int] = {}
    for doc in reconciled_docs:
        st = doc.status.value
        status_counts[st] = status_counts.get(st, 0) + 1
        explained_by_status[st] = explained_by_status.get(st, Decimal("0")) + doc.difference_signed

    net_diff = raw_pdf_net - raw_erp_net
    total_explained = sum((doc.difference_signed for doc in reconciled_docs), Decimal("0"))
    return BatchControlTotals(
        pdf_document_count=len(pdf_documents),
        erp_entry_count=len(erp_workbook.documents),
        reconciled_unique_docs=len(reconciled_docs),
        raw_pdf_invoices_total=raw_pdf_inv,
        raw_pdf_credit_notes_total=raw_pdf_cn,
        raw_pdf_net_total=raw_pdf_net,
        raw_erp_invoices_total=raw_erp_inv,
        raw_erp_credit_notes_total=raw_erp_cn,
        raw_erp_net_total=raw_erp_net,
        erp_posted_net_total=erp_posted_net,
        erp_parked_net_total=erp_parked_net,
        net_difference=net_diff,
        explained_by_status=explained_by_status,
        status_counts=status_counts,
        total_explained_difference=total_explained,
        unexplained_variance=net_diff - total_explained,
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

    reconciled_docs: list[ReconciledDocument] = []
    all_line_discrepancies: list[LineDiscrepancy] = []
    for key in ordered_keys:
        doc, diffs = _reconcile_document_group(
            key,
            pdf_by_key.get(key, []),
            erp_by_key.get(key, []),
            tolerance=tolerance,
            tax_base_error_as_needs_review=tax_base_error_as_needs_review,
        )
        reconciled_docs.append(doc)
        all_line_discrepancies.extend(diffs)

    control_totals = _compute_control_totals(pdf_documents, erp_workbook, reconciled_docs)
    return ReconciliationReport(
        pdf_filename=pdf_filename,
        erp_filename=erp_filename,
        export_info=erp_workbook.export_info,
        documents=reconciled_docs,
        line_discrepancies=all_line_discrepancies,
        audit_trail=audit_trail,
        control_totals=control_totals,
        usage=usage,
    )
