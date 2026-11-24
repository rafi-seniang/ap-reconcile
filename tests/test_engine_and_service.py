"""Integration tests for reconciliation engine (all 8 R8 statuses), math-gate retry, caching, and reporting."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pymupdf

from app.engine.math_rules import verify_pdf_document_math
from app.engine.reconcile_engine import reconcile_batch
from app.kernel import build_page_extractors, build_reconciliation_service
from app.llm.client import DiskCachedExtractor, FallbackPageExtractor, TokenCount, UpstreamUnavailable
from app.llm.parser import normalize_key, parse_assembled_document
from app.llm.pdf_splitter import PdfPagePayload
from app.schemas.erp import ErpDocument, ErpLine, ErpWorkbook
from app.schemas.invoice import (
    AssembledDocument,
    AssembledLine,
    DocumentType,
    ParsedDocument,
    PrintedLine,
    PrintedPage,
)
from app.schemas.reconciliation import DocStatus
from app.services.report_service import ReportService, write_excel_report
from config.settings import Settings, infer_provider_name


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

    # Verify Excel (4 sheets) and JSON batch report outputs.
    xlsx = write_excel_report(report, tmp_path / "recon.xlsx")
    wb = openpyxl.load_workbook(xlsx)
    assert wb.sheetnames == [
        "Executive_Summary",
        "Reconciliation_Report",
        "Line_Discrepancies",
        "Audit_Trail",
    ]
    files = ReportService().write_batch_outputs(report, tmp_path / "batch_out")
    assert set(files.keys()) == {"excel", "json"}
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
    svc = build_reconciliation_service(
        settings, primary_extractor=primary_cached, retry_extractor=retry_cached
    )

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


def test_settings_infers_provider_names_and_resolves_fallback_gracefully() -> None:
    assert infer_provider_name("gemini/gemini-3.5-flash-lite") == "gemini"
    assert infer_provider_name("deepseek/deepseek-flash") == "deepseek"
    assert infer_provider_name("anthropic/claude-sonnet-5.5") == "anthropic"
    assert infer_provider_name("openai/gpt-4o-mini") == "openai"

    # When fallback model has no API key set, build_page_extractors disables fallback instead of crashing.
    settings_single_key = Settings(
        llm_model="gemini/gemini-3.5-flash-lite",
        llm_fallback_model="deepseek/deepseek-flash",
        gemini_api_key="test-gemini-key",
        deepseek_api_key=None,
        secrets_env_path=None,
        extraction_cache_dir=None,
    )
    primary, retry, active_fallback = build_page_extractors(settings_single_key)
    assert active_fallback is None
    assert not isinstance(primary, FallbackPageExtractor)
    assert retry is primary

    # Switching LLM_MODEL without setting LLM_RETRY_MODEL reuses the switched model for retry.
    settings_switched = Settings(
        llm_model="deepseek/deepseek-flash",
        llm_retry_model=None,
        llm_fallback_model=None,
        deepseek_api_key="test-deepseek-key",
        secrets_env_path=None,
        extraction_cache_dir=None,
    )
    ds_primary, ds_retry, _ = build_page_extractors(settings_switched)
    assert ds_primary.model_name == "deepseek/deepseek-flash"
    assert ds_retry is ds_primary


class _Always503Extractor:
    provider_name = "gemini"
    model_name = "gemini/gemini-3.5-flash-lite"

    def __init__(self) -> None:
        self.calls = 0

    async def extract_page(
        self,
        page: PdfPagePayload,
        total_pages: int,
        retry_feedback: list[str] | None = None,
    ) -> tuple[PrintedPage, TokenCount]:
        self.calls += 1
        raise UpstreamUnavailable("503 high demand spike")

    async def close(self) -> None:
        return None


async def test_fallback_extractor_recovers_immediately_on_primary_503_with_in_memory_bytes() -> None:
    fitz_mod: Any = pymupdf
    pdf_doc = fitz_mod.open()
    pdf_page = pdf_doc.new_page()
    pdf_page.insert_text((72, 72), "PKI/INV/2026/09/0001")
    pdf_bytes = bytes(pdf_doc.tobytes())
    pdf_doc.close()

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
        ["E-1", "Vendor", "PKI/INV/2026/09/0001", "2026-09-10", "PO-1", "IDR", 1000000, 916667, 110000, 1110000, "Posted"]
    )
    ws_lines = wb.create_sheet("AP_Lines")
    ws_lines.append(
        ["Entry ID", "Line", "Item Code", "Item Description", "Qty", "UOM", "Unit Price", "Disc %", "Line Amount"]
    )
    ws_lines.append(["E-1", 1, "SKU-1", "Paper", 10, "rim", 100000, 0, 1000000])
    from io import BytesIO

    erp_buf = BytesIO()
    wb.save(erp_buf)
    erp_bytes = erp_buf.getvalue()

    good_page = PrintedPage(
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
                quantity_raw="10",
                uom_raw="rim",
                unit_price_raw="100.000",
                discount_pct_raw="0",
                line_amount_raw="1.000.000",
            )
        ],
    )
    failing_primary = _Always503Extractor()
    healthy_fallback = _FakeExtractor("deepseek/deepseek-flash", good_page)
    wrapped = FallbackPageExtractor(failing_primary, healthy_fallback)

    svc = build_reconciliation_service(Settings(), primary_extractor=wrapped, retry_extractor=None)
    result = await svc.reconcile(pdf_bytes, erp_bytes, pdf_display_name="mem.pdf", erp_display_name="mem.xlsx")

    assert failing_primary.calls == 1
    assert healthy_fallback.calls == 1
    assert result.report.documents[0].status == DocStatus.MATCHED
    assert result.report.control_totals.unexplained_variance == Decimal("0")
    assert result.report.usage is not None
    assert result.report.usage.fallback_model == "deepseek/deepseek-flash"
    assert result.report.usage.fallback_calls == 1


def test_engine_flags_matching_wrong_ppn_rate_and_handles_pdf_absent_erp_duplicate() -> None:
    # Both PDF and ERP have valid 11/12 DPP (916,667) yet wrong PPN (200,000 vs 110,000) -> must flag NEEDS_REVIEW.
    wrong_ppn_pdf = _pdf_doc(
        "PKI/INV/2026/10/0401",
        subtotal="1.000.000",
        dpp="916.667",
        ppn="200.000",
        total="1.200.000",
    )
    wrong_ppn_erp = _erp_doc(
        "E-401",
        "PKI/INV/2026/10/0401",
        subtotal=1_000_000,
        dpp=916_667,
        ppn=200_000,
        total=1_200_000,
    )
    dup_erp_a = _erp_doc("E-402A", "PKI/INV/2026/10/0402", row=3)
    dup_erp_b = _erp_doc("E-402B", "PKI/INV/2026/10/0402", row=4)

    report = reconcile_batch(
        [wrong_ppn_pdf],
        ErpWorkbook(documents=[wrong_ppn_erp, dup_erp_a, dup_erp_b]),
        pdf_filename="oct.pdf",
        erp_filename="oct.xlsx",
    )
    by_key = {doc.doc_number_norm: doc for doc in report.documents}
    assert by_key["PKIINV2026100401"].status == DocStatus.NEEDS_REVIEW
    assert "PDF_PPN_RULE_ERROR" in by_key["PKIINV2026100401"].reason_codes
    assert by_key["PKIINV2026100402"].status == DocStatus.DUPLICATE_IN_ERP
    assert "absent from the supplier PDF batch" in by_key["PKIINV2026100402"].plain_explanation
    assert report.control_totals.unexplained_variance == Decimal("0")


async def test_retry_resilience_erp_r7_checks_and_safe_excel_export(tmp_path: Path) -> None:
    # 1. Cross-provider LLM_API_KEY must not leak to a different fallback provider.
    isolated_settings = Settings(
        llm_model="gemini/gemini-3.5-flash-lite",
        llm_fallback_model="deepseek/deepseek-flash",
        llm_api_key="generic-gemini-only-key",
        deepseek_api_key=None,
        secrets_env_path=None,
    )
    assert isolated_settings.resolve_fallback_api_key() is None

    # 2. Retry UpstreamUnavailable or empty-line retry must preserve primary extraction without crashing.
    pdf_doc_fitz: Any = pymupdf
    raw_pdf = pdf_doc_fitz.open()
    raw_pdf.new_page().insert_text((72, 72), "PKI/INV/2026/10/0501")
    pdf_bytes = bytes(raw_pdf.tobytes())
    raw_pdf.close()

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
        ["E-501", "Vendor", "PKI/INV/2026/10/0501", "2026-10-10", "PO-501", "IDR", 1000000, 916667, 110000, 1110000, "Posted"]
    )
    ws_lines = wb.create_sheet("AP_Lines")
    ws_lines.append(
        ["Entry ID", "Line", "Item Code", "Item Description", "Qty", "UOM", "Unit Price", "Disc %", "Line Amount"]
    )
    ws_lines.append(["E-501", 1, "SKU-1", "Paper", 10, "rim", 100000, 0, 1000000])
    from io import BytesIO

    erp_buf = BytesIO()
    wb.save(erp_buf)

    supplier_math_err_page = PrintedPage(
        doc_number_raw="=PKI/INV/2026/10/0501",
        po_number_raw="PO-501",
        subtotal_raw="1.100.000",
        dpp_raw="1.008.333",
        ppn_raw="121.000",
        grand_total_raw="1.221.000",
        lines=[
            PrintedLine(
                line_number_raw="1",
                item_code="SKU-1",
                description="Paper\x0cFormFeed",
                quantity_raw="10",
                uom_raw="rim",
                unit_price_raw="100.000",
                discount_pct_raw="0",
                line_amount_raw="1.000.000",
            )
        ],
    )
    primary_ok = _FakeExtractor("gemini/gemini-3.5-flash-lite", supplier_math_err_page)
    failing_retry = _Always503Extractor()
    svc = build_reconciliation_service(Settings(), primary_extractor=primary_ok, retry_extractor=failing_retry)
    res = await svc.reconcile(pdf_bytes, erp_buf.getvalue())
    assert res.report.documents[0].status == DocStatus.NEEDS_REVIEW
    assert failing_retry.calls == 1

    # Degraded 0-line retry must never overwrite a multi-line primary extraction.
    empty_retry = _FakeExtractor("gemini/gemini-3.5-flash-lite", PrintedPage(doc_number_raw="=PKI/INV/2026/10/0501", lines=[]))
    svc_empty_retry = build_reconciliation_service(Settings(), primary_extractor=primary_ok, retry_extractor=empty_retry)
    res_preserved = await svc_empty_retry.reconcile(pdf_bytes, erp_buf.getvalue())
    assert res_preserved.report.documents[0].pdf_total_signed == Decimal("1221000")

    # 3. ERP R7 internal math error, Parked matched action, Cancelled status, and non-contiguous pages.
    parked_pdf = _pdf_doc("PKI/INV/2026/10/0502", page=2)
    parked_erp = _erp_doc("E-502", "PKI/INV/2026/10/0502", status="Parked")
    cancelled_pdf = _pdf_doc("PKI/INV/2026/10/0503", page=3)
    cancelled_erp = _erp_doc("E-503", "PKI/INV/2026/10/0503", status="Cancelled")
    erp_typo_pdf = _pdf_doc("PKI/INV/2026/10/0504", page=4)
    erp_typo_erp = _erp_doc("E-504", "PKI/INV/2026/10/0504", total=1_210_000)
    dup_p2 = _pdf_doc("PKI/INV/2026/10/0505", page=2)
    dup_p11 = _pdf_doc("PKI/INV/2026/10/0505", page=11)

    batch_report = reconcile_batch(
        [erp_typo_pdf, parked_pdf, cancelled_pdf, dup_p2, dup_p11],
        ErpWorkbook(documents=[erp_typo_erp, parked_erp, cancelled_erp]),
        pdf_filename="=oct.pdf",
        erp_filename="oct.xlsx",
    )
    by_key = {doc.doc_number_norm: doc for doc in batch_report.documents}
    assert by_key["PKIINV2026100502"].status == DocStatus.MATCHED
    assert "ERP_STATUS_PARKED" in by_key["PKIINV2026100502"].reason_codes
    assert "Post the parked ERP entry" in by_key["PKIINV2026100502"].suggested_action
    assert by_key["PKIINV2026100503"].status == DocStatus.NEEDS_REVIEW
    assert "ERP_STATUS_CANCELLED" in by_key["PKIINV2026100503"].reason_codes
    assert by_key["PKIINV2026100504"].status == DocStatus.NEEDS_REVIEW
    assert "ERP_INTERNAL_MATH_ERROR" in by_key["PKIINV2026100504"].reason_codes
    assert "PDF Pages 2, 11" in by_key["PKIINV2026100505"].plain_explanation
    assert batch_report.control_totals.unexplained_variance == Decimal("0")

    # 4. Excel export strips illegal XML chars (\x0c) and locks '=' cells to literal string data_type='s'.
    xlsx_path = write_excel_report(res.report, tmp_path / "safe.xlsx")
    reloaded = openpyxl.load_workbook(xlsx_path)
    formula_like_cell = reloaded["Reconciliation_Report"].cell(2, 1)
    assert formula_like_cell.value == "=PKI/INV/2026/10/0501"
    assert formula_like_cell.data_type == "s"
