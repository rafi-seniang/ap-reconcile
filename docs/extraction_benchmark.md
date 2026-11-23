# Extraction Architecture Benchmark: Vision LLM vs. Docling, OCR, and PDF Parsers

This note documents the empirical benchmark conducted on the 16-page September 2026 supplier batch (`PT Prima Kertas Indonesia`), the unseen October 2026 skewed raster scan (`PT Sinar Kertas Nusantara`), and the 8-page Adversarial November 2026 batch to justify using a single-path multimodal Vision LLM over traditional PDF text extractors, standalone OCR, or two-stage Docling pipelines.

---

## Ground Truth Verification Methodology

Evaluation accuracy is measured against audited deterministic ground truth, never against another LLM's output:

1. **September 2026 (`eval/ground_truth_sep2026.json`)**: Every printed header, line item, tax base, and total across all 15 PDF documents and 16 ERP rows was verified directly against the raw source files under rules R1-R8 with exact whole-batch tie-out (`Unexplained Variance = Rp 0`). For the scanned page (Page 10, `0150`), the rendered page image was visually verified line-by-line against `ERP_AP_Export_Sep2026.xlsx` (`E-10463`).
2. **Unseen Synthetic October 2026 (`eval/synthetic_october.py`) & Adversarial November 2026 (`eval/adversarial_eval.py`)**: Exact ground-truth ledgers are generated programmatically before rendering PDFs and Excel exports, giving 100% deterministic scoring across altered layouts, skewed raster scans, prompt injection, and corrupt pages.

---

## Full Benchmark Results (September 2026, 16 Pages, Local CPU)

All local baselines were run in an isolated Python 3.11 environment using each library's documented production settings (`Docling` configured with `PdfPipelineOptions(do_ocr=True, do_table_structure=True)`, `TableFormerMode.ACCURATE`, `do_cell_matching=True`, and `RapidOcrOptions` tested with both `force_full_page_ocr=False` and `force_full_page_ocr=True`).

| Approach | Skewed Scan P10 (`0150`) | Multi-Page 34 Lines (`0153`) | Direct Output | 16-Page Runtime | Local Footprint | Batch API Cost | Ready for Typed Reconciliation? |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| **`PyMuPDF`** (text + `find_tables`) | **Fail (0/8 fields)**, 96 footer chars only | 2 tables (35 + 9 rows) | Raw text / cells | 15.6 s | ~60 MB | USD 0 | No (misses scans, per-vendor parser needed) |
| **`pdfplumber`** | **Fail (0/8 fields)**, 92 footer chars only | **Fail (0 rows on P13 & P14)** | Raw text / cells | 0.11 s | ~50 MB | USD 0 | No (fails on scans and light-ruled tables) |
| **`pymupdf4llm`** (default) | **Fail (0/8 fields)**, 101 footer chars only | Markdown | Markdown | 12.5 s | ~65 MB | USD 0 | No (misses scans, still needs LLM/parser) |
| **`RapidOCR` standalone** (200 DPI PNG) | 8/8 tokens read, **0 table structure** | N/A | 77 flat unordered lines | ~7.5 s / scan page | ~450 MB | USD 0 | No (loses columns and row order) |
| **`Docling`** (`TableFormer ACCURATE` + `RapidOCR`) | 8/8 tokens read, **layout quirks** | 34/34 rows in 2 Markdown tables | Unstructured Markdown | **60.1 s warm** (135.1 s cold) | **~2.0 GB** (1,496 MB venv + 506 MB weights) | USD 0 local + **Stage-2 LLM** | No (still needs Stage-2 LLM or brittle parser) |
| **Vision LLM: `deepseek-flash`** (200 DPI PNG) | **Exact (100%)** | **34/34 lines exact (100%)** | **Validated Pydantic JSON** | **33.7 s** | **415 MB** (0 MB ML weights) | **USD 0.01 actual** | **Yes (16/16 status, Rp 0 unexplained)** |
| **Vision LLM: `gemini-3.5-flash-lite`** (Native PDF) | **Exact (100%)** | **34/34 lines exact (100%)** | **Validated Pydantic JSON** | **22.7 s** | **415 MB** (0 MB ML weights) | **USD 0 free tier** (est. USD 0.045 paid) | **Yes (16/16 status, Rp 0 unexplained)** |

---

## Concrete Failure Artifacts Observed

### 1. Silent Scan-Router Trap in Plain PDF Extractors
On Page 10 (`PKI/INV/2026/09/0150`), the PDF embeds a 96-character vector footer (`PT Prima Kertas Indonesia ... Page 10`) over a 69%-area skewed scan image.
- `PyMuPDF`, `pdfplumber`, and `pymupdf4llm` extracted **0 invoice fields and 0 table rows**.
- Any naive hybrid router using a character threshold (`if len(text) < 50: run_ocr()`) classifies Page 10 as digital and silently drops a Rp 21.6M invoice.
- `pdfplumber` also returned **0 table rows** on both Pages 13 and 14 (`0153`, 34 line items) because the table uses light horizontal hairlines without full grid boxes.

### 2. Column Loss in Standalone OCR (`RapidOCR`)
Running `RapidOCR` on Page 10 at 200 DPI read all 8 numeric tokens, but returned 77 flat lines with no column coordinates:
- Row 5 (`KW-A4-80-RM`) split out of reading order across four lines (`KW-A4-80-RM`, `KertaswarnaA480gsm,pastelmix`, `5`, `(rim)`).
- Punctuation drifted into fullwidth CJK characters (`TOTAL(Rp）`).

### 3. Structural Drift Inside `Docling` Markdown (Even on One Supplier)
Tested fairly with `TableFormerMode.ACCURATE` and `RapidOcrOptions`, `Docling` read all 15 invoice totals and all 34 lines of `0153` into Markdown. However, four operational problems emerged immediately:
1. **Inconsistent table boundaries within the same vendor**: Across the 15 `PT Prima Kertas` documents, Docling placed `Subtotal / DPP / PPN / TOTAL` **inside the Markdown table on 9 documents** and **outside as detached paragraphs on 6 documents**.
2. **Scrambled header and table labels on the skewed scan (Page 10)**:
   - Default OCR merged a label and invoice number onto one line: `Jatuh Tempo / Due Date PKI/INV/2026/09/0150` and duplicated `DPP Nilai Lain (11/12)` across two adjacent columns.
   - `force_full_page_ocr=True` inverted the amount column header (`Amount (Rp) Jumlah /`), dropped `#`, and prepended `(rim)` to the start of the item description.
3. **Still requires a Stage-2 LLM across 40+ suppliers**: Writing deterministic Markdown parsers for 40+ shifting vendor layouts creates ongoing maintenance debt. Feeding Docling Markdown into a Stage-2 text LLM removes the pixel view, so if Docling drops a column or shifts a cell, the text LLM cannot see the original image to recover.
4. **Slower and heavier on CPU**: Warm Docling alone took **60.1 s** on CPU (135.1 s cold with HuggingFace weight download) and consumed **~2.0 GB** of disk space (`1,496 MB` environment + `506 MB` model weights), before any Stage-2 LLM call. By contrast, single-stage `deepseek-flash` completed all 16 pages directly into validated Pydantic JSON in **33.7 s** for **USD 0.01**.

---

## Vision LLM Provider Comparison (September 2026)

| Metric | `gemini-3.5-flash-lite` (Native 1-Page PDF) | `deepseek-flash` (200 DPI PNG) |
| --- | --- | --- |
| Status / Totals / Line Discrepancies | **16/16**, **16/16**, **16/16** | **16/16**, **16/16**, **16/16** |
| Unexplained Batch Variance | **Rp 0** | **Rp 0** |
| Primary + Math-Gate Retry Calls | 16 + 1 (only Page 9 / `0149`) | 16 + 1 (only Page 9 / `0149`) |
| Input / Output Tokens | 13,735 in / 15,330 out | 32,242 in / 14,335 out |
| Latency (concurrency=3) | **22.7 s** | 33.7 s |
| Actual Spend | USD 0 (free tier, list eq. USD 0.045) | **USD 0.01** (API balance USD 1.78 to USD 1.77) |

Native PDF input on `gemini-3.5-flash-lite` uses ~57% fewer input tokens and completes in 22.7 s, while `deepseek-flash` provides predictable pay-as-you-go throughput at USD 0.01 per 16-page batch without free-tier daily caps. `gemini-3.8-flash` was also tested live and returned upstream HTTP 503/504 capacity errors on the free tier, validating the provider-agnostic switch and `UpstreamUnavailable` fail-fast behavior.

---

## When Docling or Local VLM Pipelines Make Sense

Single-path cloud Vision LLM is the right architecture for PT Nusantara Retail Group's volume (40+ suppliers, roughly USD 0.40/month at USD 0.01 per 16-page batch). Local pipelines become preferable only under two constraints:

1. **Strict air-gapped / zero-egress compliance**: When supplier invoices cannot leave an on-premise network, swap the `PageExtractor` adapter to a self-hosted VLM (such as Docling's `VlmPipeline` with `Granite-Docling` or local `Qwen-VL`) on an internal GPU while keeping the entire deterministic parser, math gate, and reconciliation engine unchanged.
2. **Extreme document volume (100,000+ pages/month)**: At enterprise scale with dedicated GPU capacity, amortizing local VLM inference beats per-token API spend.
