"""Deterministic plain-language explanations and suggested actions written for AP clerks."""

from decimal import Decimal

from app.schemas.reconciliation import DocStatus, LineDiscrepancy


def _format_idr(amount: Decimal) -> str:
    sign_prefix = "-" if amount < 0 else ""
    return f"{sign_prefix}IDR {abs(amount):,.0f}"


def _format_page_range(pages: list[int]) -> str:
    if not pages:
        return "not in PDF"
    if len(pages) == 1:
        return f"PDF Page {pages[0]}"
    return f"PDF Pages {pages[0]}-{pages[-1]}"


def _explain_matched(
    reason_codes: list[str],
    pdf_pages: list[int],
    page_text: str,
    erp_text: str,
    pdf_total_signed: Decimal,
) -> tuple[str, str]:
    notes: list[str] = []
    if "DOC_NO_FORMAT_NORMALISED" in reason_codes:
        notes.append("document number separators differed (/ vs -) and matched after normalisation")
    if "CREDIT_NOTE_SIGN_NORMALISED" in reason_codes:
        notes.append("credit note printed as positive in PDF and booked as negative in ERP")
    if len(pdf_pages) > 1:
        notes.append(f"multi-page invoice across {page_text}")
    suffix = f" ({', '.join(notes)})" if notes else ""
    return (
        f"All line items, tax base (11/12 DPP), PPN, and total ({_format_idr(pdf_total_signed)}) match exactly "
        f"between {page_text} and {erp_text}{suffix}.",
        "No action required. Cleared for scheduled payment run.",
    )


def _explain_needs_review(
    *,
    reason_codes: list[str],
    page_text: str,
    erp_text: str,
    pdf_total_signed: Decimal,
    erp_total_signed: Decimal,
    difference_signed: Decimal,
    pdf_po_or_ref: str | None,
    erp_po_or_ref: str | None,
    failed_check_details: list[str],
) -> tuple[str, str]:
    reason_set = set(reason_codes)
    causes: list[str] = []
    if "CURRENCY_MISMATCH" in reason_set:
        causes.append("document currency differs between supplier PDF and ERP export")
    if "PO_NUMBER_MISMATCH" in reason_set:
        causes.append(
            f"PO number disagrees (PDF '{pdf_po_or_ref}' vs ERP '{erp_po_or_ref}') while amounts match"
        )
    if "CN_REFERENCE_MISMATCH" in reason_set:
        causes.append(
            f"original invoice reference disagrees (PDF '{pdf_po_or_ref}' vs ERP '{erp_po_or_ref}')"
        )
    if "TAX_BASE_WRONG_DPP_NOT_11_12" in reason_set:
        causes.append(
            f"supplier calculated 12% PPN on 100% of subtotal instead of the 11/12 DPP Nilai Lain base (R3/R4), "
            f"creating a {_format_idr(difference_signed)} tax difference (PDF {_format_idr(pdf_total_signed)} vs ERP {_format_idr(erp_total_signed)})"
        )
    if "PDF_PPN_RULE_ERROR" in reason_set:
        causes.append("printed PPN deviates from the R3/R4 12% on 11/12 DPP formula beyond rounding tolerance")
    if reason_set & {"PDF_INTERNAL_SUBTOTAL_ERROR", "PDF_INTERNAL_LINE_ERROR", "PDF_INTERNAL_TOTAL_ERROR"}:
        check_text = (
            ", ".join(failed_check_details) if failed_check_details else "internal figures do not add up"
        )
        causes.append(
            f"supplier PDF arithmetic does not add up ({check_text}). Per R7, no figure was auto-corrected "
            f"(PDF {_format_idr(pdf_total_signed)} vs ERP {_format_idr(erp_total_signed)}, diff {_format_idr(difference_signed)})"
        )
    if "EXTRACTION_PARSE_ERROR" in reason_set:
        check_text = (
            ", ".join(failed_check_details) if failed_check_details else "fields could not be read reliably"
        )
        causes.append(f"extraction check failed ({check_text})")
    if not causes:
        causes.append(
            ", ".join(failed_check_details) if failed_check_details else "requires manual AP verification"
        )

    action = "Do not pay yet. Hold for AP supervisor review."
    if "CURRENCY_MISMATCH" in reason_set:
        action = f"Verify invoice currency on {page_text} before booking or converting amounts manually."
    elif "TAX_BASE_WRONG_DPP_NOT_11_12" in reason_set or "PDF_PPN_RULE_ERROR" in reason_set:
        action = "Hold payment and request a revised tax invoice (Faktur Pajak) using the 11/12 DPP Nilai Lain base."
    elif "PDF_INTERNAL_SUBTOTAL_ERROR" in reason_set or "PDF_INTERNAL_LINE_ERROR" in reason_set:
        action = "Hold payment and request a corrected invoice from the supplier (do not pay the overstated PDF total)."
    elif "PO_NUMBER_MISMATCH" in reason_set:
        action = (
            "Confirm the correct PO number with Procurement and correct the ERP PO reference before payment."
        )
    elif "EXTRACTION_PARSE_ERROR" in reason_set:
        action = (
            f"Open {page_text} in the review queue and verify the illegible or unparseable fields manually."
        )

    return f"Flagged for review on {page_text} vs {erp_text}: {'. '.join(causes)}.", action


def _explain_amount_mismatch(
    page_text: str,
    erp_text: str,
    pdf_total_signed: Decimal,
    erp_total_signed: Decimal,
    difference_signed: Decimal,
    line_discrepancies: list[LineDiscrepancy],
) -> tuple[str, str]:
    line_summary = ", ".join(item.detail for item in line_discrepancies)
    if line_summary:
        explanation = (
            f"Total differs by {_format_idr(difference_signed)} between {page_text} ({_format_idr(pdf_total_signed)}) "
            f"and {erp_text} ({_format_idr(erp_total_signed)}). Line differences: {line_summary}."
        )
    else:
        explanation = (
            f"Total differs by {_format_idr(difference_signed)} between {page_text} ({_format_idr(pdf_total_signed)}) "
            f"and {erp_text} ({_format_idr(erp_total_signed)}) with matching line items (tax or header difference)."
        )

    if any(item.discrepancy_type == "EXTRA_LINE_IN_PDF" for item in line_discrepancies):
        action = "Check PO/contract terms for the unbooked charge (e.g. delivery fee). Book in ERP if approved or dispute with supplier."
    elif any("QTY" in item.discrepancy_type for item in line_discrepancies):
        action = "Verify Goods Receipt Note (GRN) quantity. Pay only for received quantity and request a credit note if overbilled."
    elif any("PRICE" in item.discrepancy_type for item in line_discrepancies):
        action = "Check agreed PO unit price. Dispute overbilling with supplier or update PO/ERP if a price revision was approved."
    else:
        action = "Review the discrepant line items with Procurement and supplier before approving payment."
    return explanation, action


def _explain_missing_or_duplicate(
    *,
    doc_label: str,
    status: DocStatus,
    pdf_pages: list[int],
    page_text: str,
    erp_text: str,
    erp_refs: list[str],
    erp_doc_status: str | None,
    pdf_total_signed: Decimal,
    erp_total_signed: Decimal,
    difference_signed: Decimal,
    pdf_po_or_ref: str | None,
) -> tuple[str, str]:
    if status == DocStatus.DUPLICATE_IN_ERP:
        pdf_clause = (
            f"appears once in {page_text} ({_format_idr(pdf_total_signed)})"
            if pdf_pages
            else "is absent from the supplier PDF batch"
        )
        return (
            f"{doc_label} {pdf_clause} but is booked {len(erp_refs)} times in ERP ({erp_text}), "
            f"totaling {_format_idr(erp_total_signed)} and overstating AP by {_format_idr(abs(difference_signed))}.",
            f"Hold payment immediately and reverse the duplicate ERP entry to prevent a {_format_idr(abs(difference_signed))} overpayment.",
        )
    if status == DocStatus.DUPLICATE_IN_PDF:
        return (
            f"{doc_label} appears multiple times across {page_text} (combined PDF {_format_idr(pdf_total_signed)}) "
            f"compared to ERP {erp_text} ({_format_idr(erp_total_signed)}).",
            "Confirm with the supplier whether the repeated invoice in the PDF batch is a duplicate copy and pay once only.",
        )
    if status == DocStatus.MISSING_IN_ERP:
        return (
            f"{doc_label} ({_format_idr(pdf_total_signed)}) is present on {page_text} "
            f"(PO {pdf_po_or_ref or 'N/A'}) but has no matching entry in the ERP export.",
            "Verify goods receipt against the PO and post the invoice in ERP if valid, or reject back to supplier.",
        )
    parked_note = (
        f" Its ERP Doc Status is '{erp_doc_status}', so it has not yet been posted."
        if erp_doc_status and erp_doc_status.lower() != "posted"
        else ""
    )
    action = (
        "Request the missing supplier invoice PDF before posting or releasing payment."
        if erp_doc_status and erp_doc_status.lower() == "parked"
        else "Obtain the supporting invoice PDF from the supplier or verify whether this ERP posting belongs to another period."
    )
    return (
        f"{doc_label} ({_format_idr(erp_total_signed)}) exists in ERP ({erp_text}) but is missing from the supplier PDF batch.{parked_note}",
        action,
    )


def build_explanation_and_action(
    *,
    doc_label: str,
    status: DocStatus,
    reason_codes: list[str],
    pdf_pages: list[int],
    erp_refs: list[str],
    erp_doc_status: str | None,
    pdf_total_signed: Decimal,
    erp_total_signed: Decimal,
    difference_signed: Decimal,
    pdf_po_or_ref: str | None,
    erp_po_or_ref: str | None,
    line_discrepancies: list[LineDiscrepancy],
    failed_check_details: list[str],
) -> tuple[str, str]:
    """Generate an auditor-clear explanation and actionable next step without LLM guesswork."""
    page_text = _format_page_range(pdf_pages)
    erp_text = ", ".join(erp_refs) if erp_refs else "not in ERP"

    if status == DocStatus.MATCHED:
        return _explain_matched(reason_codes, pdf_pages, page_text, erp_text, pdf_total_signed)

    if status == DocStatus.MATCHED_WITHIN_TOLERANCE:
        return (
            f"All line items and subtotal match between {page_text} and {erp_text}. "
            f"Total differs by {_format_idr(abs(difference_signed))} (PDF {_format_idr(pdf_total_signed)} vs "
            f"ERP {_format_idr(erp_total_signed)}), within the IDR 10 rounding tolerance under R5 "
            f"(two-step 11/12 DPP x 12% rounding vs one-step 11% effective rate).",
            "Approve within rounding tolerance (R5). No supplier dispute needed.",
        )

    if status in (
        DocStatus.DUPLICATE_IN_ERP,
        DocStatus.DUPLICATE_IN_PDF,
        DocStatus.MISSING_IN_ERP,
        DocStatus.MISSING_IN_PDF,
    ):
        return _explain_missing_or_duplicate(
            doc_label=doc_label,
            status=status,
            pdf_pages=pdf_pages,
            page_text=page_text,
            erp_text=erp_text,
            erp_refs=erp_refs,
            erp_doc_status=erp_doc_status,
            pdf_total_signed=pdf_total_signed,
            erp_total_signed=erp_total_signed,
            difference_signed=difference_signed,
            pdf_po_or_ref=pdf_po_or_ref,
        )

    if status == DocStatus.NEEDS_REVIEW:
        return _explain_needs_review(
            reason_codes=reason_codes,
            page_text=page_text,
            erp_text=erp_text,
            pdf_total_signed=pdf_total_signed,
            erp_total_signed=erp_total_signed,
            difference_signed=difference_signed,
            pdf_po_or_ref=pdf_po_or_ref,
            erp_po_or_ref=erp_po_or_ref,
            failed_check_details=failed_check_details,
        )

    return _explain_amount_mismatch(
        page_text, erp_text, pdf_total_signed, erp_total_signed, difference_signed, line_discrepancies
    )
