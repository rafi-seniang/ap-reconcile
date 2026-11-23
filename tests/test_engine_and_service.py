"""Integration tests for reconciliation engine (all 8 R8 statuses), math-gate retry, caching, and reporting."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pymupdf

from app.erp.schemas import ErpDocument, ErpLine, ErpWorkbook
from app.extraction.llm_client import DiskCachedExtractor, TokenCount
from app.extraction.parser import normalize_key, parse_assembled_document
from app.extraction.schemas import (
    AssembledDocument,
    AssembledLine,
    DocumentType,
    ParsedDocument,
    PrintedLine,
    PrintedPage,
)
from app.extraction.splitter import PdfPagePayload
from app.reconcile.engine import reconcile_batch
from app.reconcile.math_rules import verify_pdf_document_math
from app.reconcile.schemas import DocStatus
from app.reporting.csv_writer import write_csv_and_json_reports
from app.reporting.excel_writer import write_excel_report
from app.service import ReconciliationService
from config.settings import Settings


def _pdf_doc(
    doc_no: str,
    *,
    page: int = 1,
    doc_type: DocumentType = "invoice",
    po_no: str | None = "PO-1",
    ref_doc: str | None = None,
    subtotal: str = "1.000.000",
    dpp: str = "916.667",
    ppn: str = "110.000",
    total: str = "1.110.000",
    sku: str = "SKU-1",
    qty: str = "10",
    price: str = "100.000",
    line_amt: str = "1.000.000",
) -> ParsedDocument:
    assembled = AssembledDocument(
        pages=[page],
        document_type=doc_type,
        supplier_name="PT Prima Kertas",
        doc_number_raw=doc_no,
        doc_date_raw="10 September 2026",
        due_date_raw=None,
        po_number_raw=po_no if doc_type == "invoice" else None,
        reference_doc_raw=ref_doc if doc_type == "credit_note" else None,
        currency="IDR",
        subtotal_raw=subtotal,
        dpp_raw=dpp,
        ppn_raw=ppn,
        grand_total_raw=total,
        lines=[
            AssembledLine(
                source_page=page,
                printed=PrintedLine(
                    line_number_raw="1",
                    item_code=sku,
                    description="Paper",
                    quantity_raw=qty,
                    uom_raw="rim",
                    unit_price_raw=price,
                    discount_pct_raw="0",
                    line_amount_raw=line_amt,
                ),
            )
        ],
    )
    parsed = parse_assembled_document(assembled)
    parsed.checks.extend(verify_pdf_document_math(parsed))
    return parsed


def _erp_doc(
    entry_id: str,
    doc_no: str,
    *,
    row: int = 2,
    doc_type: DocumentType = "invoice",
    po_no: str | None = "PO-1",
    orig_inv: str | None = None,
    subtotal: int = 1_000_000,
    dpp: int = 916_667,
    ppn: int = 110_000,
    total: int = 1_110_000,
    status: str = "Posted",
    sku: str = "SKU-1",
    qty: int = 10,
    price: int = 100_000,
    line_amt: int = 1_000_000,
) -> ErpDocument:
    sign = Decimal("-1") if doc_type == "credit_note" else Decimal("1")
    return ErpDocument(
        entry_id=entry_id,
        sheet="AP_CreditNotes" if doc_type == "credit_note" else "AP_Invoices",
        row=row,
        document_type=doc_type,
        vendor="PT Prima Kertas",
        doc_number_raw=doc_no,
        doc_number_norm=normalize_key(doc_no) or doc_no,
        doc_date=date(2026, 9, 10),
        po_number_raw=po_no if doc_type == "invoice" else None,
        po_number_norm=normalize_key(po_no) if doc_type == "invoice" else None,
        original_invoice_raw=orig_inv if doc_type == "credit_note" else None,
        original_invoice_norm=normalize_key(orig_inv) if doc_type == "credit_note" else None,
        currency="IDR",
        subtotal_signed=Decimal(subtotal) * sign,
        dpp_signed=Decimal(dpp) * sign,
        ppn_signed=Decimal(ppn) * sign,
        grand_total_signed=Decimal(total) * sign,
        doc_status=status,
        lines=[
            ErpLine(
                entry_id=entry_id,
                sheet="AP_Lines",
                row=row,
                line_number=1,
                item_code_raw=sku,
                item_code_norm=normalize_key(sku),
                description="Paper",
                quantity_signed=Decimal(qty) * sign,
                quantity_abs=Decimal(abs(qty)),
                uom_raw="rim",
                unit_price=Decimal(price),
                discount_pct=Decimal("0"),
                line_amount_signed=Decimal(line_amt) * sign,
                line_amount_abs=Decimal(abs(line_amt)),
            )
        ],
    )


def test_engine_covers_all_eight_r8_statuses_with_zero_unexplained_variance(tmp_path: Path) -> None:
    pdf_docs = [
        _pdf_doc("PKI/INV/2026/09/0001", page=1),  # MATCHED (with normalized ERP hyphen)
        _pdf_doc(
            "PKI/INV/2026/09/0002",
            page=2,
            subtotal="15.902.350",
            dpp="14.577.154",
            ppn="1.749.258",
            total="17.651.608",
            qty="1",
            price="15.902.350",
            line_amt="15.902.350",
        ),  # MATCHED_WITHIN_TOLERANCE (diff -1)
        _pdf_doc(
            "PKI/INV/2026/09/0003",
            page=3,
            qty="12",
            line_amt="1.200.000",
            subtotal="1.200.000",
            dpp="1.100.000",
            ppn="132.000",
            total="1.332.000",
        ),  # AMOUNT_MISMATCH
        _pdf_doc("PKI/INV/2026/09/0004", page=4),  # MISSING_IN_ERP
        _pdf_doc("PKI/INV/2026/09/0005", page=5),  # DUPLICATE_IN_ERP
        _pdf_doc("PKI/INV/2026/09/0006", page=6),  # DUPLICATE_IN_PDF (copy 1)
        _pdf_doc("PKI/INV/2026/09/0006", page=7),  # DUPLICATE_IN_PDF (copy 2)
        _pdf_doc("PKI/INV/2026/09/0007", page=8, po_no="PO-88239"),  # NEEDS_REVIEW (PO mismatch)
        _pdf_doc(
            "PKI/INV/2026/09/0008", page=9, dpp="1.000.000", ppn="120.000", total="1.120.000"
        ),  # NEEDS_REVIEW (wrong DPP 100%)
        _pdf_doc(
            "PKI/CN/2026/09/0012",
            page=10,
            doc_type="credit_note",
            ref_doc="PKI/INV/2026/08/0131",
            subtotal="876.000",
            dpp="803.000",
            ppn="96.360",
            total="972.360",
            qty="1",
            price="876.000",
            line_amt="876.000",
        ),  # MATCHED (credit note sign normalized)
    ]

    erp_docs = [
        _erp_doc("E-1", "PKI-INV-2026-09-0001", row=2),
        _erp_doc(
            "E-2",
            "PKI/INV/2026/09/0002",
            row=3,
            subtotal=15_902_350,
            dpp=14_577_154,
            ppn=1_749_259,
            total=17_651_609,
            qty=1,
            price=15_902_350,
            line_amt=15_902_350,
        ),
        _erp_doc("E-3", "PKI/INV/2026/09/0003", row=4),
        _erp_doc("E-5A", "PKI/INV/2026/09/0005", row=5),
        _erp_doc("E-5B", "PKI/INV/2026/09/0005", row=6),
        _erp_doc("E-6", "PKI/INV/2026/09/0006", row=7),
        _erp_doc("E-7", "PKI/INV/2026/09/0007", row=8, po_no="PO-88293"),
        _erp_doc("E-8", "PKI/INV/2026/09/0008", row=9),
        _erp_doc("E-9", "PKI/INV/2026/09/0009", row=10, status="Parked"),  # MISSING_IN_PDF
        _erp_doc(
            "E-CN",
            "PKI/CN/2026/09/0012",
            row=2,
            doc_type="credit_note",
            orig_inv="PKI/INV/2026/08/0131",
            subtotal=876_000,
            dpp=803_000,
            ppn=96_360,
            total=972_360,
            qty=1,
            price=876_000,
            line_amt=876_000,
        ),
    ]

    report = reconcile_batch(
        pdf_docs,
        ErpWorkbook(documents=erp_docs),
        pdf_filename="batch.pdf",
        erp_filename="erp.xlsx",
    )

    by_key = {d.doc_number_norm: d for d in report.documents}
    assert by_key["PKIINV2026090001"].status == DocStatus.MATCHED
    assert "DOC_NO_FORMAT_NORMALISED" in by_key["PKIINV2026090001"].reason_codes
    assert by_key["PKIINV2026090002"].status == DocStatus.MATCHED_WITHIN_TOLERANCE
    assert by_key["PKIINV2026090003"].status == DocStatus.AMOUNT_MISMATCH
    assert by_key["PKIINV2026090004"].status == DocStatus.MISSING_IN_ERP
    assert by_key["PKIINV2026090005"].status == DocStatus.DUPLICATE_IN_ERP
    assert by_key["PKIINV2026090005"].difference_signed == Decimal("-1110000")
    assert by_key["PKIINV2026090006"].status == DocStatus.DUPLICATE_IN_PDF
    assert by_key["PKIINV2026090006"].difference_signed == Decimal("1110000")
    assert by_key["PKIINV2026090007"].status == DocStatus.NEEDS_REVIEW
    assert "PO_NUMBER_MISMATCH" in by_key["PKIINV2026090007"].reason_codes
    assert by_key["PKIINV2026090008"].status == DocStatus.NEEDS_REVIEW
    assert "TAX_BASE_WRONG_DPP_NOT_11_12" in by_key["PKIINV2026090008"].reason_codes
    assert by_key["PKIINV2026090009"].status == DocStatus.MISSING_IN_PDF
    assert "ERP_STATUS_PARKED" in by_key["PKIINV2026090009"].reason_codes
    assert by_key["PKICN2026090012"].status == DocStatus.MATCHED

    # Exact batch tie-out: every single rupiah of variance is explained.
    assert report.control_totals.unexplained_variance == Decimal("0")
    assert set(report.control_totals.status_counts.keys()) == {s.value for s in DocStatus}

    # Verify policy knob: wrong DPP base as AMOUNT_MISMATCH when configured.
    alt_report = reconcile_batch(
        pdf_docs,
        ErpWorkbook(documents=erp_docs),
        pdf_filename="batch.pdf",
        erp_filename="erp.xlsx",
        tax_base_error_as_needs_review=False,
    )
    alt_by_key = {d.doc_number_norm: d for d in alt_report.documents}
    assert alt_by_key["PKIINV2026090008"].status == DocStatus.AMOUNT_MISMATCH
    assert alt_report.control_totals.unexplained_variance == Decimal("0")

    # Verify Excel (4 sheets) and CSV/JSON writers.
    xlsx = write_excel_report(report, tmp_path / "recon.xlsx")
    wb = openpyxl.load_workbook(xlsx)
    assert wb.sheetnames == [
        "Executive_Summary",
        "Reconciliation_Report",
        "Line_Discrepancies",
        "Audit_Trail",
    ]
    files = write_csv_and_json_reports(report, tmp_path)
    assert all(p.is_file() for p in files.values())


def test_engine_flags_currency_mismatch_and_illegible_unbooked_page_for_review() -> None:
    usd_pdf = _pdf_doc("PKI/INV/2026/11/0301", page=1)
    usd_pdf.currency = "USD"
    usd_erp = _erp_doc("E-301", "PKI/INV/2026/11/0301", row=2)

    blank_pdf = parse_assembled_document(
        AssembledDocument(
            pages=[2],
            document_type="invoice",
            supplier_name=None,
            doc_number_raw=None,
            doc_date_raw=None,
            due_date_raw=None,
            po_number_raw=None,
            reference_doc_raw=None,
            currency=None,
            subtotal_raw=None,
            dpp_raw=None,
            ppn_raw=None,
            grand_total_raw=None,
            lines=[],
        )
    )
    blank_pdf.checks.extend(verify_pdf_document_math(blank_pdf))

    report = reconcile_batch(
        [usd_pdf, blank_pdf],
        ErpWorkbook(documents=[usd_erp]),
        pdf_filename="adv.pdf",
        erp_filename="adv.xlsx",
    )
    by_key = {d.doc_number_norm: d for d in report.documents}
    assert by_key["PKIINV2026110301"].status == DocStatus.NEEDS_REVIEW
    assert "CURRENCY_MISMATCH" in by_key["PKIINV2026110301"].reason_codes
    assert by_key["UNREADABLE_P2"].status == DocStatus.NEEDS_REVIEW
    assert "EXTRACTION_PARSE_ERROR" in by_key["UNREADABLE_P2"].reason_codes
    assert report.control_totals.unexplained_variance == Decimal("0")


class _FakeExtractor:
    provider_name = "gemini"

    def __init__(self, model_name: str, response: PrintedPage) -> None:
        self.model_name = model_name
        self._response = response
        self.calls = 0

    async def extract_page(
        self,
        page: PdfPagePayload,
        total_pages: int,
        retry_feedback: list[str] | None = None,
    ) -> tuple[PrintedPage, TokenCount]:
        self.calls += 1
        return self._response, TokenCount(input_tokens=100, output_tokens=50)

    async def close(self) -> None:
        return None


async def test_service_math_gate_retry_repairs_primary_misread_and_cache_hits(tmp_path: Path) -> None:
    # Create a tiny valid 1-page PDF and matching 1-invoice ERP workbook.
    pdf_path = tmp_path / "one.pdf"
    fitz_mod: Any = pymupdf
    doc = fitz_mod.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Invoice PKI/INV/2026/09/0001")
    doc.save(pdf_path)
    doc.close()

    erp_path = tmp_path / "one.xlsx"
    wb = openpyxl.Workbook()
    ws_inv = wb.active
    assert ws_inv is not None
    ws_inv.title = "AP_Invoices"
    ws_inv.append(
        [
            "Entry ID",
            "Vendor",
            "Vendor Inv. No",
            "Doc Date",
            "PO No",
            "Curr",
            "Subtotal",
            "DPP",
            "PPN",
            "Grand Total",
            "Doc Status",
        ]
    )
    ws_inv.append(
        [
            "E-1",
            "Vendor",
            "PKI/INV/2026/09/0001",
            "2026-09-10",
            "PO-1",
            "IDR",
            1000000,
            916667,
            110000,
            1110000,
            "Posted",
        ]
    )
    ws_lines = wb.create_sheet("AP_Lines")
    ws_lines.append(
        [
            "Entry ID",
            "Line",
            "Item Code",
            "Item Description",
            "Qty",
            "UOM",
            "Unit Price",
            "Disc %",
            "Line Amount",
        ]
    )
    ws_lines.append(["E-1", 1, "SKU-1", "Paper", 10, "rim", 100000, 0, 1000000])
    wb.save(erp_path)

    # Primary misreads qty as '18' instead of '10' -> fails line_1_adds_up -> triggers retry -> retry reads '10'.
    bad_primary = PrintedPage(
        doc_number_raw="PKI/INV/2026/09/0001",
        po_number_raw="PO-1",
        subtotal_raw="1.000.000",
        dpp_raw="916.667",
        ppn_raw="110.000",
        grand_total_raw="1.110.000",
        lines=[
            PrintedLine(
                line_number_raw="1",
                item_code="SKU-1",
                description="Paper",
                quantity_raw="18",
                uom_raw="rim",
                unit_price_raw="100.000",
                discount_pct_raw="0",
                line_amount_raw="1.000.000",
            )
        ],
    )
    fixed_retry = bad_primary.model_copy(deep=True)
    fixed_retry.lines[0].quantity_raw = "10"

    primary_inner = _FakeExtractor("fake-primary", bad_primary)
    retry_inner = _FakeExtractor("fake-retry", fixed_retry)
    cache_dir = tmp_path / "cache"
    primary_cached = DiskCachedExtractor(primary_inner, cache_dir)
    retry_cached = DiskCachedExtractor(retry_inner, cache_dir)

    settings = Settings(
        llm_input_price_per_million_usd=1.0,
        llm_output_price_per_million_usd=2.0,
    )
    svc = ReconciliationService(settings, primary_cached, retry_cached)

    first = await svc.reconcile(pdf_path, erp_path, output_dir=tmp_path / "out")
    assert first.report.documents[0].status == DocStatus.MATCHED
    assert first.report.control_totals.unexplained_variance == Decimal("0")
    assert first.report.usage is not None
    assert first.report.usage.primary_calls == 1
    assert first.report.usage.retry_calls == 1
    assert primary_inner.calls == 1
    assert retry_inner.calls == 1

    # Second run must hit disk cache with zero new inner LLM calls.
    second = await svc.reconcile(pdf_path, erp_path)
    assert second.report.documents[0].status == DocStatus.MATCHED
    assert second.report.usage is not None
    assert second.report.usage.cached_pages == 2
    assert primary_inner.calls == 1
    assert retry_inner.calls == 1
