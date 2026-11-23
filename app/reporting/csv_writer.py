"""CSV and JSON report writers alongside the Excel workbook for scripted audits."""

import csv
import json
from pathlib import Path

from app.reconcile.schemas import ReconciliationReport


def _write_reconciliation_csv(report: ReconciliationReport, out_dir: Path) -> Path:
    path = out_dir / "reconciliation_report.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "doc_number_pdf",
                "doc_number_erp",
                "document_type",
                "pdf_pages",
                "erp_refs",
                "erp_doc_status",
                "pdf_po_or_ref",
                "erp_po_or_ref",
                "pdf_total_idr",
                "erp_total_idr",
                "difference_idr",
                "status",
                "reason_codes",
                "plain_explanation",
                "suggested_action",
            ]
        )
        for doc in report.documents:
            writer.writerow(
                [
                    doc.pdf_doc_number_raw or "",
                    doc.erp_doc_number_raw or "",
                    doc.document_type,
                    "|".join(str(p) for p in doc.pdf_pages),
                    "|".join(doc.erp_refs),
                    doc.erp_doc_status or "",
                    doc.pdf_po_or_ref or "",
                    doc.erp_po_or_ref or "",
                    str(doc.pdf_total_signed),
                    str(doc.erp_total_signed),
                    str(doc.difference_signed),
                    doc.status.value,
                    "|".join(doc.reason_codes),
                    doc.plain_explanation,
                    doc.suggested_action,
                ]
            )
    return path


def _write_line_discrepancies_csv(report: ReconciliationReport, out_dir: Path) -> Path:
    path = out_dir / "line_discrepancies.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "doc_number",
                "discrepancy_type",
                "item_code",
                "pdf_page",
                "pdf_line",
                "erp_ref",
                "pdf_line_amount",
                "erp_line_amount",
                "amount_difference",
                "detail",
            ]
        )
        for item in report.line_discrepancies:
            writer.writerow(
                [
                    item.doc_number_raw,
                    item.discrepancy_type,
                    item.item_code or "",
                    item.pdf_page or "",
                    item.pdf_line_number or "",
                    item.erp_ref or "",
                    str(item.pdf_line_amount) if item.pdf_line_amount is not None else "",
                    str(item.erp_line_amount) if item.erp_line_amount is not None else "",
                    str(item.amount_difference),
                    item.detail,
                ]
            )
    return path


def _write_audit_trail_csv(report: ReconciliationReport, out_dir: Path) -> Path:
    path = out_dir / "audit_trail.csv"
    headers = [
        "doc_number_norm",
        "doc_number_raw",
        "source_side",
        "level",
        "location",
        "field_or_item",
        "raw_value",
        "numeric_value",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for ref in report.audit_trail:
            writer.writerow(
                [
                    ref.doc_number_norm,
                    ref.doc_number_raw,
                    ref.source_side,
                    ref.level,
                    ref.location,
                    ref.field_or_item,
                    ref.raw_value,
                    str(ref.numeric_value) if ref.numeric_value is not None else "",
                ]
            )
    return path


def write_csv_and_json_reports(report: ReconciliationReport, output_dir: Path | str) -> dict[str, Path]:
    """Write reconciliation_report.csv, line_discrepancies.csv, audit_trail.csv, and batch_summary.json."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary_json = out / "batch_summary.json"
    summary_json.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "reconciliation_csv": _write_reconciliation_csv(report, out),
        "line_discrepancies_csv": _write_line_discrepancies_csv(report, out),
        "audit_trail_csv": _write_audit_trail_csv(report, out),
        "batch_summary_json": summary_json,
    }
