"""Generate and evaluate an unseen synthetic October 2026 batch covering all 8 R8 statuses and layout shifts."""

import argparse
import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pymupdf

from app.reconcile.math_rules import compute_dpp_nilai_lain, compute_line_amount, compute_ppn, compute_total
from app.reconcile.schemas import DocStatus
from app.service import ReconciliationService
from config.settings import Settings
from eval.run_eval import ExpectedDoc, score_report

_pymupdf: Any = pymupdf


@dataclass
class _SynLine:
    sku: str
    desc: str
    qty: int
    uom: str
    price: int
    disc: Decimal

    @property
    def amount(self) -> Decimal:
        return compute_line_amount(Decimal(self.qty), Decimal(self.price), self.disc)


def _fmt_id(amount: Decimal | int) -> str:
    return f"{int(amount):,}".replace(",", ".")


def _fmt_disc(disc: Decimal) -> str:
    if disc == Decimal("0"):
        return "-"
    text = f"{disc:f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + "%"


def _draw_invoice_page(
    pdf_doc: Any,
    *,
    title: str,
    supplier: str,
    doc_no: str | None,
    doc_date: str | None,
    po_or_ref_label: str | None,
    po_or_ref_value: str | None,
    lines: list[tuple[int, _SynLine]],
    totals: tuple[Decimal, Decimal, Decimal, Decimal] | None,
    english_layout: bool = False,
    continuation_note: str | None = None,
) -> Any:
    page = pdf_doc.new_page(width=595, height=842)
    y = 44.0
    if supplier:
        page.insert_text((40, y), supplier, fontsize=13, fontname="helv")
        y += 18
    if title:
        page.insert_text((40, y), title, fontsize=11, fontname="helv")
        y += 18
    if continuation_note:
        page.insert_text((40, y), continuation_note, fontsize=9, fontname="helv")
        y += 16

    if doc_no:
        lbl = "Invoice No." if english_layout else "No. Dokumen"
        page.insert_text((40, y), f"{lbl}: {doc_no}", fontsize=10, fontname="helv")
        if doc_date:
            dlbl = "Invoice Date" if english_layout else "Tanggal"
            page.insert_text((320, y), f"{dlbl}: {doc_date}", fontsize=10, fontname="helv")
        y += 16
    if po_or_ref_label and po_or_ref_value:
        page.insert_text((40, y), f"{po_or_ref_label}: {po_or_ref_value}", fontsize=10, fontname="helv")
        y += 18

    y += 6
    headers = (
        ("No", "SKU", "Item Description", "Qty", "UOM", "Unit Price", "Disc", "Line Total")
        if english_layout
        else ("No", "Kode", "Nama Barang", "Qty", "Sat", "Harga Satuan", "Disk", "Jumlah (IDR)")
    )
    cols = (40, 68, 148, 305, 342, 382, 456, 500)
    page.draw_rect(_pymupdf.Rect(36, y - 12, 560, y + 6), fill=(0.90, 0.93, 0.96), color=(0.7, 0.7, 0.7))
    for x, h in zip(cols, headers, strict=True):
        page.insert_text((x, y), h, fontsize=8.5, fontname="helv")
    y += 16

    for line_no, item in lines:
        vals = (
            str(line_no),
            item.sku,
            item.desc[:26],
            str(item.qty),
            item.uom,
            _fmt_id(item.price),
            _fmt_disc(item.disc),
            _fmt_id(item.amount),
        )
        for x, val in zip(cols, vals, strict=True):
            page.insert_text((x, y), val, fontsize=8.5, fontname="helv")
        y += 15

    if totals is not None:
        subtotal, dpp, ppn, grand_total = totals
        y += 12
        page.draw_line((330, y - 8), (560, y - 8), color=(0.4, 0.4, 0.4))
        rows = (
            [
                ("Subtotal", _fmt_id(subtotal)),
                ("Tax Base (11/12 DPP)", _fmt_id(dpp)),
                ("VAT 12% (PPN)", _fmt_id(ppn)),
                ("GRAND TOTAL (IDR)", _fmt_id(grand_total)),
            ]
            if english_layout
            else [
                ("Subtotal", _fmt_id(subtotal)),
                ("DPP Nilai Lain (11/12)", _fmt_id(dpp)),
                ("PPN 12%", _fmt_id(ppn)),
                ("TOTAL (IDR)", _fmt_id(grand_total)),
            ]
        )
        for label, val in rows:
            page.insert_text((335, y + 6), label, fontsize=9.5, fontname="helv")
            page.insert_text((485, y + 6), f"Rp {val}", fontsize=9.5, fontname="helv")
            y += 16

    return page


def _append_skewed_scan_page(target_pdf: Any, source_doc: Any, angle_deg: float = 2.4) -> None:
    """Rasterize a vector invoice page and embed it slightly skewed with zero text layer."""
    pix = source_doc.load_page(0).get_pixmap(dpi=150, alpha=False)
    img_pdf = _pymupdf.open()
    img_page = img_pdf.new_page(width=595, height=842)
    img_page.insert_image(_pymupdf.Rect(0, 0, 595, 842), stream=pix.tobytes("png"))

    out_page = target_pdf.new_page(width=595, height=842)
    # Draw scanner shadow border and show rotated raster page so zero selectable text remains.
    out_page.draw_rect(_pymupdf.Rect(0, 0, 595, 842), fill=(0.94, 0.94, 0.93))
    out_page.show_pdf_page(_pymupdf.Rect(14, 14, 581, 828), img_pdf, 0, rotate=0)
    # Apply subtle skew by rotating an inner rasterized snapshot via PyMuPDF matrix on a fresh canvas.
    skew_mat = _pymupdf.Matrix(1, 1).prerotate(angle_deg)
    pix_rot = img_page.get_pixmap(matrix=skew_mat, dpi=150, alpha=False)
    out_page.clean_contents()
    out_page.draw_rect(_pymupdf.Rect(0, 0, 595, 842), fill=(0.95, 0.95, 0.94))
    out_page.insert_image(_pymupdf.Rect(12, 12, 583, 830), stream=pix_rot.tobytes("png"))
    img_pdf.close()


def generate_synthetic_october(out_dir: Path) -> tuple[Path, Path, tuple[ExpectedDoc, ...]]:
    """Create unseen October 2026 PDF (15 pages) and ERP Excel workbook covering all 8 R8 statuses."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / "Supplier_Invoices_Oct2026_Unseen.pdf"
    erp_path = out_dir / "ERP_AP_Export_Oct2026_Unseen.xlsx"

    pdf_doc = _pymupdf.open()
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
            "Posted By",
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
    ws_cn = wb.create_sheet("AP_CreditNotes")
    ws_cn.append(
        [
            "Entry ID",
            "Vendor",
            "Vendor CN No",
            "Doc Date",
            "Original Invoice",
            "Curr",
            "Subtotal",
            "DPP",
            "PPN",
            "Grand Total",
            "Doc Status",
        ]
    )
    ws_info = wb.create_sheet("Export_Info")
    ws_info.append(["Company", "PT Nusantara Retail Group"])
    ws_info.append(["Period", "October 2026 (Synthetic Unseen Stress Test)"])

    def add_erp_inv(
        entry_id: str,
        doc_no: str,
        doc_date: str,
        po_no: str,
        lines: list[_SynLine],
        *,
        status: str = "Posted",
        ppn_override: Decimal | None = None,
        total_as_string: bool = False,
        sort_lines_by_sku: bool = False,
    ) -> Decimal:
        sub = sum((ln.amount for ln in lines), Decimal("0"))
        dpp = compute_dpp_nilai_lain(sub)
        ppn = ppn_override if ppn_override is not None else compute_ppn(dpp)
        tot = compute_total(sub, ppn)
        ws_inv.append(
            [
                entry_id,
                "PT Sinar Kertas Nusantara",
                doc_no,
                doc_date,
                po_no,
                "IDR",
                int(sub),
                int(dpp),
                int(ppn),
                f"{int(tot):,}" if total_as_string else int(tot),
                status,
                "fin.ap02",
            ]
        )
        ordered = sorted(lines, key=lambda x: x.sku) if sort_lines_by_sku else lines
        for idx, ln in enumerate(ordered, start=1):
            ws_lines.append(
                [entry_id, idx, ln.sku, ln.desc, ln.qty, ln.uom, ln.price, float(ln.disc), int(ln.amount)]
            )
        return tot

    expected: list[ExpectedDoc] = []

    # 1. English altered layout + exact .5 half-up rounding -> MATCHED
    l201 = [
        _SynLine("PAPER-A4-80", "Copy Paper A4 80gsm", 65, "RIM", 4750, Decimal("5")),  # 293,312.5 -> 293,313
        _SynLine("CLIP-BIG-02", "Binder Clip 32mm", 40, "BOX", 14500, Decimal("0")),
    ]
    sub201 = sum((x.amount for x in l201), Decimal("0"))
    dpp201, ppn201 = compute_dpp_nilai_lain(sub201), compute_ppn(compute_dpp_nilai_lain(sub201))
    tot201 = compute_total(sub201, ppn201)
    _draw_invoice_page(
        pdf_doc,
        title="COMMERCIAL TAX INVOICE",
        supplier="PT SINAR KERTAS NUSANTARA",
        doc_no="SKN/INV/2026/10/0201",
        doc_date="02 October 2026",
        po_or_ref_label="Purchase Order",
        po_or_ref_value="PO-99101",
        lines=list(enumerate(l201, start=1)),
        totals=(sub201, dpp201, ppn201, tot201),
        english_layout=True,
    )
    add_erp_inv("E-20001", "SKN-INV-2026-10-0201", "10/02/2026", "PO-99101", l201, total_as_string=True)
    expected.append(
        ExpectedDoc(
            "SKNINV2026100201",
            DocStatus.MATCHED,
            tot201,
            tot201,
            required_reason_codes=("DOC_NO_FORMAT_NORMALISED",),
        )
    )

    # 2. Skewed rasterized scan (zero text layer) -> MATCHED
    l202 = [
        _SynLine("TAPE-CLR-48", "Clear Packing Tape 48mm", 50, "ROLL", 12500, Decimal("0")),
        _SynLine(
            "SCISS-21", "Office Scissors 21cm", 27, "PCS", 18500, Decimal("2.5")
        ),  # 487,012.5 -> 487,013
    ]
    sub202 = sum((x.amount for x in l202), Decimal("0"))
    dpp202 = compute_dpp_nilai_lain(sub202)
    ppn202 = compute_ppn(dpp202)
    tot202 = compute_total(sub202, ppn202)
    scan_src = _pymupdf.open()
    _draw_invoice_page(
        scan_src,
        title="FAKTUR PENJUALAN",
        supplier="PT SINAR KERTAS NUSANTARA",
        doc_no="SKN/INV/2026/10/0202",
        doc_date="04 Oktober 2026",
        po_or_ref_label="No. PO",
        po_or_ref_value="PO-99102",
        lines=list(enumerate(l202, start=1)),
        totals=(sub202, dpp202, ppn202, tot202),
    )
    _append_skewed_scan_page(pdf_doc, scan_src, angle_deg=2.2)
    scan_src.close()
    add_erp_inv("E-20002", "SKN/INV/2026/10/0202", "2026-10-04", "PO-99102", l202)
    expected.append(ExpectedDoc("SKNINV2026100202", DocStatus.MATCHED, tot202, tot202))

    # 3. 3-page multi-page invoice (24 lines across P3, P4, P5; ERP sorted by SKU) -> MATCHED
    skus_pool = ["ZEB-PEN", "AMP-110", "KRD-BOX", "PNS-2B", "LKB-48", "ORD-5CM"]
    l203 = [
        _SynLine(
            skus_pool[i % len(skus_pool)],
            f"Stationery Bundle {i + 1}",
            10 + (i % 7),
            "BOX",
            15000 + (i * 500),
            Decimal("5") if i % 3 == 0 else Decimal("0"),
        )
        for i in range(24)
    ]
    sub203 = sum((x.amount for x in l203), Decimal("0"))
    dpp203 = compute_dpp_nilai_lain(sub203)
    ppn203 = compute_ppn(dpp203)
    tot203 = compute_total(sub203, ppn203)
    _draw_invoice_page(
        pdf_doc,
        title="FAKTUR PENJUALAN (Hal 1 dari 3)",
        supplier="PT SINAR KERTAS NUSANTARA",
        doc_no="SKN/INV/2026/10/0203",
        doc_date="06 Oktober 2026",
        po_or_ref_label="No. PO",
        po_or_ref_value="PO-99103",
        lines=list(enumerate(l203[:9], start=1)),
        totals=None,
    )
    _draw_invoice_page(
        pdf_doc,
        title="",
        supplier="",
        doc_no=None,
        doc_date=None,
        po_or_ref_label=None,
        po_or_ref_value=None,
        lines=list(enumerate(l203[9:18], start=10)),
        totals=None,
        continuation_note="Lanjutan Faktur (Hal 2 dari 3)",
    )
    _draw_invoice_page(
        pdf_doc,
        title="",
        supplier="",
        doc_no=None,
        doc_date=None,
        po_or_ref_label=None,
        po_or_ref_value=None,
        lines=list(enumerate(l203[18:], start=19)),
        totals=(sub203, dpp203, ppn203, tot203),
        continuation_note="Lanjutan Faktur (Hal 3 dari 3)",
    )
    add_erp_inv("E-20003", "SKN/INV/2026/10/0203", "2026-10-06", "PO-99103", l203, sort_lines_by_sku=True)
    expected.append(ExpectedDoc("SKNINV2026100203", DocStatus.MATCHED, tot203, tot203))

    # 4. Rp 1 PPN rounding tolerance -> MATCHED_WITHIN_TOLERANCE
    l204 = [_SynLine("BULK-A4", "Paper Pallet A4", 1, "LOT", 15902350, Decimal("0"))]
    sub204 = Decimal("15902350")
    dpp204 = compute_dpp_nilai_lain(sub204)
    ppn204 = compute_ppn(dpp204)  # 1,749,258
    tot204 = compute_total(sub204, ppn204)  # 17,651,608
    _draw_invoice_page(
        pdf_doc,
        title="FAKTUR PENJUALAN",
        supplier="PT SINAR KERTAS NUSANTARA",
        doc_no="SKN/INV/2026/10/0204",
        doc_date="08 Oktober 2026",
        po_or_ref_label="No. PO",
        po_or_ref_value="PO-99104",
        lines=list(enumerate(l204, start=1)),
        totals=(sub204, dpp204, ppn204, tot204),
    )
    erp_tot204 = add_erp_inv(
        "E-20004", "SKN/INV/2026/10/0204", "2026-10-08", "PO-99104", l204, ppn_override=ppn204 + Decimal("1")
    )
    expected.append(ExpectedDoc("SKNINV2026100204", DocStatus.MATCHED_WITHIN_TOLERANCE, tot204, erp_tot204))

    # 5. Discount % mismatch -> AMOUNT_MISMATCH
    l205_pdf = [_SynLine("TONER-85A", "Laser Toner 85A", 10, "PCS", 400000, Decimal("5"))]
    l205_erp = [_SynLine("TONER-85A", "Laser Toner 85A", 10, "PCS", 400000, Decimal("10"))]
    sub205 = l205_pdf[0].amount
    dpp205, ppn205 = compute_dpp_nilai_lain(sub205), compute_ppn(compute_dpp_nilai_lain(sub205))
    tot205 = compute_total(sub205, ppn205)
    _draw_invoice_page(
        pdf_doc,
        title="FAKTUR PENJUALAN",
        supplier="PT SINAR KERTAS NUSANTARA",
        doc_no="SKN/INV/2026/10/0205",
        doc_date="10 Oktober 2026",
        po_or_ref_label="No. PO",
        po_or_ref_value="PO-99105",
        lines=list(enumerate(l205_pdf, start=1)),
        totals=(sub205, dpp205, ppn205, tot205),
    )
    erp_tot205 = add_erp_inv("E-20005", "SKN/INV/2026/10/0205", "2026-10-10", "PO-99105", l205_erp)
    expected.append(
        ExpectedDoc(
            "SKNINV2026100205",
            DocStatus.AMOUNT_MISMATCH,
            tot205,
            erp_tot205,
            expected_line_diff_skus=("TONER-85A",),
        )
    )

    # 6. Missing in ERP -> MISSING_IN_ERP
    l206 = [_SynLine("ENV-DL-100", "Envelope DL Box 100", 80, "BOX", 24000, Decimal("0"))]
    sub206 = l206[0].amount
    dpp206, ppn206 = compute_dpp_nilai_lain(sub206), compute_ppn(compute_dpp_nilai_lain(sub206))
    tot206 = compute_total(sub206, ppn206)
    _draw_invoice_page(
        pdf_doc,
        title="FAKTUR PENJUALAN",
        supplier="PT SINAR KERTAS NUSANTARA",
        doc_no="SKN/INV/2026/10/0206",
        doc_date="12 Oktober 2026",
        po_or_ref_label="No. PO",
        po_or_ref_value="PO-99106",
        lines=list(enumerate(l206, start=1)),
        totals=(sub206, dpp206, ppn206, tot206),
    )
    expected.append(ExpectedDoc("SKNINV2026100206", DocStatus.MISSING_IN_ERP, tot206, Decimal("0")))

    # 7. DUPLICATE_IN_PDF (two full pages with same doc no, booked once in ERP)
    l207 = [_SynLine("FOLD-F4", "Folio Folder Pack 12", 40, "PAK", 35000, Decimal("0"))]
    sub207 = l207[0].amount
    dpp207, ppn207 = compute_dpp_nilai_lain(sub207), compute_ppn(compute_dpp_nilai_lain(sub207))
    tot207 = compute_total(sub207, ppn207)
    for _ in range(2):
        _draw_invoice_page(
            pdf_doc,
            title="FAKTUR PENJUALAN",
            supplier="PT SINAR KERTAS NUSANTARA",
            doc_no="SKN/INV/2026/10/0207",
            doc_date="14 Oktober 2026",
            po_or_ref_label="No. PO",
            po_or_ref_value="PO-99107",
            lines=list(enumerate(l207, start=1)),
            totals=(sub207, dpp207, ppn207, tot207),
        )
    erp_tot207 = add_erp_inv("E-20007", "SKN/INV/2026/10/0207", "2026-10-14", "PO-99107", l207)
    expected.append(ExpectedDoc("SKNINV2026100207", DocStatus.DUPLICATE_IN_PDF, tot207 * 2, erp_tot207))

    # 8. DUPLICATE_IN_ERP (once in PDF, twice in ERP)
    l208 = [_SynLine("MARKER-BLK", "Permanent Marker Black", 60, "BOX", 42000, Decimal("0"))]
    sub208 = l208[0].amount
    dpp208, ppn208 = compute_dpp_nilai_lain(sub208), compute_ppn(compute_dpp_nilai_lain(sub208))
    tot208 = compute_total(sub208, ppn208)
    _draw_invoice_page(
        pdf_doc,
        title="FAKTUR PENJUALAN",
        supplier="PT SINAR KERTAS NUSANTARA",
        doc_no="SKN/INV/2026/10/0208",
        doc_date="16 Oktober 2026",
        po_or_ref_label="No. PO",
        po_or_ref_value="PO-99108",
        lines=list(enumerate(l208, start=1)),
        totals=(sub208, dpp208, ppn208, tot208),
    )
    add_erp_inv("E-20008", "SKN/INV/2026/10/0208", "2026-10-16", "PO-99108", l208)
    add_erp_inv("E-20009", "SKN/INV/2026/10/0208", "2026-10-16", "PO-99108", l208)
    expected.append(ExpectedDoc("SKNINV2026100208", DocStatus.DUPLICATE_IN_ERP, tot208, tot208 * 2))

    # 9. PO Number mismatch -> NEEDS_REVIEW
    l209 = [_SynLine("STAPLE-HD", "Heavy Duty Stapler", 15, "PCS", 95000, Decimal("0"))]
    sub209 = l209[0].amount
    dpp209, ppn209 = compute_dpp_nilai_lain(sub209), compute_ppn(compute_dpp_nilai_lain(sub209))
    tot209 = compute_total(sub209, ppn209)
    _draw_invoice_page(
        pdf_doc,
        title="FAKTUR PENJUALAN",
        supplier="PT SINAR KERTAS NUSANTARA",
        doc_no="SKN/INV/2026/10/0209",
        doc_date="18 Oktober 2026",
        po_or_ref_label="No. PO",
        po_or_ref_value="PO-99109",
        lines=list(enumerate(l209, start=1)),
        totals=(sub209, dpp209, ppn209, tot209),
    )
    add_erp_inv("E-20010", "SKN/INV/2026/10/0209", "2026-10-18", "PO-99190", l209)
    expected.append(
        ExpectedDoc(
            "SKNINV2026100209",
            DocStatus.NEEDS_REVIEW,
            tot209,
            tot209,
            required_reason_codes=("PO_NUMBER_MISMATCH",),
        )
    )

    # 10. Wrong DPP base (100% subtotal instead of 11/12) -> NEEDS_REVIEW
    l210 = [_SynLine("A3-80G-RM", "Paper A3 80gsm", 50, "RIM", 98000, Decimal("0"))]
    sub210 = l210[0].amount
    bad_dpp210 = sub210
    bad_ppn210 = compute_ppn(bad_dpp210)
    bad_tot210 = compute_total(sub210, bad_ppn210)
    _draw_invoice_page(
        pdf_doc,
        title="FAKTUR PENJUALAN",
        supplier="PT SINAR KERTAS NUSANTARA",
        doc_no="SKN/INV/2026/10/0210",
        doc_date="20 Oktober 2026",
        po_or_ref_label="No. PO",
        po_or_ref_value="PO-99110",
        lines=list(enumerate(l210, start=1)),
        totals=(sub210, bad_dpp210, bad_ppn210, bad_tot210),
    )
    erp_tot210 = add_erp_inv("E-20011", "SKN/INV/2026/10/0210", "2026-10-20", "PO-99110", l210)
    expected.append(
        ExpectedDoc(
            "SKNINV2026100210",
            DocStatus.NEEDS_REVIEW,
            bad_tot210,
            erp_tot210,
            required_reason_codes=("TAX_BASE_WRONG_DPP_NOT_11_12",),
        )
    )

    # 11. PDF internal subtotal error (+50,000) -> NEEDS_REVIEW
    l211 = [
        _SynLine("NOTE-3X3", "Sticky Notes 3x3", 20, "PAK", 50000, Decimal("0")),
        _SynLine("PEN-GEL", "Gel Pen Box 12", 10, "BOX", 60000, Decimal("0")),
    ]
    bad_sub211 = Decimal("1650000")  # True line sum is 1,600,000
    bad_dpp211 = compute_dpp_nilai_lain(bad_sub211)
    bad_ppn211 = compute_ppn(bad_dpp211)
    bad_tot211 = compute_total(bad_sub211, bad_ppn211)
    _draw_invoice_page(
        pdf_doc,
        title="FAKTUR PENJUALAN",
        supplier="PT SINAR KERTAS NUSANTARA",
        doc_no="SKN/INV/2026/10/0211",
        doc_date="22 Oktober 2026",
        po_or_ref_label="No. PO",
        po_or_ref_value="PO-99111",
        lines=list(enumerate(l211, start=1)),
        totals=(bad_sub211, bad_dpp211, bad_ppn211, bad_tot211),
    )
    erp_tot211 = add_erp_inv("E-20012", "SKN/INV/2026/10/0211", "2026-10-22", "PO-99111", l211)
    expected.append(
        ExpectedDoc(
            "SKNINV2026100211",
            DocStatus.NEEDS_REVIEW,
            bad_tot211,
            erp_tot211,
            required_reason_codes=("PDF_INTERNAL_SUBTOTAL_ERROR",),
        )
    )

    # 12. Credit Note -> MATCHED
    lcn = [_SynLine("PAPER-A4-80", "Damaged Paper Return", 10, "RIM", 48000, Decimal("0"))]
    sub_cn = lcn[0].amount
    dpp_cn = compute_dpp_nilai_lain(sub_cn)
    ppn_cn = compute_ppn(dpp_cn)
    tot_cn = compute_total(sub_cn, ppn_cn)
    _draw_invoice_page(
        pdf_doc,
        title="NOTA KREDIT / CREDIT NOTE",
        supplier="PT SINAR KERTAS NUSANTARA",
        doc_no="SKN/CN/2026/10/0003",
        doc_date="25 Oktober 2026",
        po_or_ref_label="Referensi Faktur",
        po_or_ref_value="SKN/INV/2026/09/0190",
        lines=list(enumerate(lcn, start=1)),
        totals=(sub_cn, dpp_cn, ppn_cn, tot_cn),
    )
    ws_cn.append(
        [
            "E-20013",
            "PT Sinar Kertas Nusantara",
            "SKN/CN/2026/10/0003",
            "2026-10-25",
            "SKN/INV/2026/09/0190",
            "IDR",
            -int(sub_cn),
            -int(dpp_cn),
            -int(ppn_cn),
            -int(tot_cn),
            "Posted",
        ]
    )
    ws_lines.append(
        ["E-20013", 1, lcn[0].sku, lcn[0].desc, -lcn[0].qty, lcn[0].uom, lcn[0].price, 0, -int(sub_cn)]
    )
    expected.append(ExpectedDoc("SKNCN2026100003", DocStatus.MATCHED, -tot_cn, -tot_cn))

    # 13. Parked invoice missing in PDF -> MISSING_IN_PDF
    l212 = [_SynLine("BINDER-A4", "Ring Binder A4", 30, "PCS", 32000, Decimal("0"))]
    erp_tot212 = add_erp_inv(
        "E-20014", "SKN/INV/2026/10/0212", "2026-10-30", "PO-99112", l212, status="Parked"
    )
    expected.append(
        ExpectedDoc(
            "SKNINV2026100212",
            DocStatus.MISSING_IN_PDF,
            Decimal("0"),
            erp_tot212,
            required_reason_codes=("ERP_STATUS_PARKED",),
        )
    )

    pdf_doc.save(pdf_path)
    pdf_doc.close()
    wb.save(erp_path)
    return pdf_path, erp_path, tuple(expected)


async def _run(args: argparse.Namespace) -> int:
    pdf_path, erp_path, expected = generate_synthetic_october(args.gen_dir)
    if args.generate_only:
        print(f"Generated unseen October batch:\n  PDF: {pdf_path}\n  ERP: {erp_path}")
        return 0

    settings = Settings(extraction_cache_dir=args.cache_dir)
    service = ReconciliationService.from_settings(settings)
    try:
        result = await service.reconcile(pdf_path, erp_path, output_dir=args.out)
    finally:
        await service.close()

    metrics = score_report(result.report, expected)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "eval_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    return 0 if not metrics["mismatches"] and metrics["unexplained_variance"] == "0" else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate and run unseen synthetic October 2026 stress test."
    )
    parser.add_argument("--gen-dir", type=Path, default=Path("eval/generated"))
    parser.add_argument("--out", type=Path, default=Path("output/eval_oct2026_synthetic"))
    parser.add_argument("--cache-dir", default="eval/cache")
    parser.add_argument("--generate-only", action="store_true")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
