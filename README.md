# AP Transaction Reconciliation (`ap-reconcile`)

Automated supplier invoice PDF to ERP accounts payable (AP) reconciliation engine built for Indonesian B2B invoices (2025/2026 PPN 12% on the 11/12 DPP Nilai Lain tax base, multi-page invoices, skewed scans, and credit notes).

Every rupiah of variance between the supplier PDF batch and the ERP AP export is accounted for (`Unexplained Variance = Rp 0`) with full `PDF Page` and `ERP Sheet!Row` audit trails.

## Quick Start (<= 5 Commands)

Requires Python 3.11+. On macOS/Linux, use `source .venv/bin/activate` and `cp`.

```bash
# 1. Create virtual environment and install dependencies
python -m venv .venv && .venv\Scripts\activate && pip install -e ".[dev]"

# 2. Configure provider and API key (Gemini or OpenAI-compatible such as DeepSeek)
copy .env.example .env

# 3. Run the offline unit and API test suite (no API key required)
pytest -q

# 4. Reconcile a supplier PDF batch against an ERP AP Excel export
python main.py reconcile --pdf path/to/Supplier_Invoices.pdf --erp path/to/ERP_AP_Export.xlsx --out output/sep2026

# 5. (Optional) Start the local AP Review Queue web UI at http://127.0.0.1:8000
python main.py serve
```

## Architecture Overview

Visual reading is isolated to a multimodal LLM, while all arithmetic, normalization, matching, and tie-outs run deterministically in Python (`Decimal` + `ROUND_HALF_UP`).

```text
ERP Excel (.xlsx) ──► Fail-Fast Schema Validator (all Sheet!Row errors before LLM spend)
                                │
                                ▼
Supplier PDF ──► PyMuPDF Page Splitter (1-page native PDF + 200 DPI PNG)
                                │
                                ▼
              Multimodal Verbatim Extractor (gemini native PDF | openai_compat PNG)
                                │
                                ▼
              Deterministic Assembler, Parser & R1-R4 Math Gate (1x targeted retry)
                                │
                                ▼
              Deterministic Matcher & R5-R8 Engine (Unexplained Variance = Rp 0)
                                │
                                ▼
         Outputs: 4-Sheet Excel Report | CSV + JSON | FastAPI AP Review Queue UI
```

| Folder | Responsibility |
| --- | --- |
| `app/erp/` | Strict ERP workbook validator (`AP_Invoices`, `AP_Lines`, `AP_CreditNotes`, `Export_Info`) with cell-level `Sheet!Row` errors |
| `app/extraction/` | Page splitter, provider-agnostic Vision LLM clients with disk cache, multi-page assembler, and ID/EN parser |
| `app/reconcile/` | `Decimal` + `ROUND_HALF_UP` rules R1-R4, two-pass multiset SKU line matcher, R5-R8 engine, and plain-language explanations |
| `app/reporting/` | 4-sheet formatted Excel workbook (`Executive_Summary`, `Reconciliation_Report`, `Line_Discrepancies`, `Audit_Trail`) + CSV/JSON |
| `app/api/` | Thin FastAPI router (`routes.py`) serving a separated vanilla HTML/CSS/JS Review Queue workbench (`app/api/static/`) |
| `eval/` | Audited ground truth (`ground_truth_sep2026.json`), Sep 2026 evaluator, synthetic Oct 2026, and adversarial Nov 2026 suites |
| `docs/` | [CFO Summary](docs/customer_summary.md), [Extraction Benchmark](docs/extraction_benchmark.md), and [Evaluation Report](docs/evaluation_report.md) |

## Key Engineering Decisions

1. **Single-path Vision LLM over PDF/OCR branching**: Plain text extractors fail silently on partial scans (Page 10 embeds 96 footer chars over a scanned image). One multimodal path avoids brittle layout routers for about USD 0.01 per 16-page batch.
2. **Verbatim extraction + deterministic `Decimal` (`ROUND_HALF_UP`)**: The model never computes math. Standard Python `round()` (banker's rounding) diverges on five exact `.5` IDR cases in September 2026.
3. **1x math-gate retry instead of blind double-extraction**: Only pages failing deterministic R1-R4 checks retry once with check feedback. On September and October batches, only 1 genuine supplier error triggered a retry.
4. **Explicit R3/R4 tax-base policy (`0148`)**: Default `TAX_BASE_ERROR_AS_NEEDS_REVIEW=true` routes 100%-DPP invoices to `NEEDS_REVIEW` (requires revised Faktur Pajak), configurable to `AMOUNT_MISMATCH` via `.env`.
5. **Fail-fast ERP contract validation**: Broken sheets, columns, dates, or numbers return HTTP 422 / exit code 2 with every `Sheet!Row Column` error before spending any LLM tokens.
6. **Deterministic check gates over LLM confidence scores**: Auto-clearance requires every `CheckResult` gate (`parse_*`, line math, subtotal, DPP, PPN, grand total) to pass rather than trusting uncalibrated self-reported scores.
7. **Provider-agnostic client**: During live testing, `gemini-3.8-flash` hit upstream HTTP 503/504 capacity outages. Switching `.env` to `gemini-3.5-flash-lite` or `deepseek-flash` required zero code changes and produced identical 16/16 results.

## Evaluation Summary

### Why Single-Path Vision LLM (16-Page September 2026 Batch)

Full benchmark methodology, timing, and raw failure artifacts are in [`docs/extraction_benchmark.md`](docs/extraction_benchmark.md).

| Extractor | Scan P10 (`0150`) | 34-Line P13-14 (`0153`) | 16p Runtime | Cost & Output |
| --- | --- | --- | ---: | --- |
| `PyMuPDF` / `pdfplumber` / `pymupdf4llm` | **Fail (0/8 fields)** | `pdfplumber` 0 rows | 0.1-15.6 s | USD 0, raw text/Markdown only |
| `RapidOCR` (200 DPI PNG) | Tokens read, **0 table structure** | N/A | ~7.5 s / scan | USD 0, 77 flat unordered lines |
| `Docling` (`TableFormer ACCURATE` + `RapidOCR`) | Tokens read, **inconsistent Markdown** | 34/34 Markdown rows | 60.1 s warm | ~2.0 GB weights + **needs Stage-2 LLM** |
| **Vision LLM (`gemini-3.5-flash-lite` / `deepseek-flash`)** | **Exact (100%)** | **34/34 exact (100%)** | **22.7-33.7 s** | **USD 0 free / USD 0.01, direct JSON** |

### End-to-End Reconciliation Results

Full control totals (Rp 0 tie-out), 7 adversarial cases, and observed failure guardrails are in [`docs/evaluation_report.md`](docs/evaluation_report.md).

| Benchmark Suite | Model | Status & Totals | Line Diffs | Unexplained | Calls (Primary+Retry) | Runtime & Cost |
| --- | --- | --- | --- | ---: | --- | --- |
| Sep 2026 (`ground_truth_sep2026.json`) | `gemini-3.5-flash-lite` | **16 / 16 (100%)** | **16 / 16** | **Rp 0** | 16 + 1 (`0149`) | **22.7 s** / USD 0 free |
| Sep 2026 (`ground_truth_sep2026.json`) | `deepseek-flash` | **16 / 16 (100%)** | **16 / 16** | **Rp 0** | 16 + 1 (`0149`) | 33.7 s / **USD 0.01** |
| Sep 2026 (`ground_truth_sep2026.json`) | `gemini-3.8-flash` | Upstream 503/504 | - | - | Failed loudly | `UpstreamUnavailable` |
| Unseen Synthetic Oct 2026 (all 8 statuses) | `gemini-3.5-flash-lite` | **13 / 13 (100%)** | **13 / 13** | **Rp 0** | 15 + 1 | **16.0 s** / USD 0 free |
| Adversarial Nov 2026 (injection, blank scan, FX) | `gemini-3.5-flash-lite` | **7 / 7 (100%)** | **7 / 7** | **Rp 0** | 8 + 2 | **9.3 s** / USD 0 free |

## Onboarding 40+ Additional Suppliers

- **Shadow-run gate**: Run 5-10 historical invoices per new supplier via `eval/run_eval.py` and promote only at 100% tie-out (`Unexplained = Rp 0`).
- **Supplier policy config**: Store per-vendor tax regime (11/12 DPP vs 0% exempt vs non-PKP), currency rules, and approved charge SKUs in config rather than branching code.
- **Thin ERP adapters**: Map SAP, Accurate, Jurnal, or NetSuite exports into `ErpWorkbook` while keeping the strict validator unchanged.
- **Drift alerts**: Monitor per-supplier retry and `NEEDS_REVIEW` rates monthly to catch vendor invoice template changes early.

## Known Limitations & What I Would Do With More Time

- **Current limitations**: Single currency per batch (whole-rupiah IDR), no direct PO/GRN master lookup, no invoice-wide header discounts or PPh 23 withholding lines, in-memory run history (latest 10 runs), and no automatic re-ordering of physically shuffled multi-page scans.
- **Proper CI/CD and regression gating**: Add GitHub Actions to run `ruff`, `mypy --strict`, offline `pytest`, and scheduled golden-dataset evals so prompt or model changes cannot silently regress accuracy before deployment.
- **Deeper extraction and model research**: Benchmark additional vision models (`gemini-3.8-flash` once upstream capacity stabilizes, `gemini-4-neon`, `gemini-4-hydrogen`, `deepseek-pro`, `claude-haiku-5.5`, `claude-sonnet-5.5`, and `gpt-luna-6.2`), structured-output settings, and render DPI trade-offs (150 vs 200 vs 300 DPI) across providers.
- **Systematic edge-case testing**: Collect and test harder real-world Indonesian AP edge cases including overlapping wet stamps, handwritten adjustments, low-DPI phone photos, out-of-order scanned pages, multi-currency FX rounding, invoice-wide discounts, and PPh 23 withholding.
- **Production workflow hardening**: Add 3-way match (PO + GRN + Indonesian e-Faktur QR/NSFP), persistent PostgreSQL storage with reviewer sign-offs and ERP reversal journals, and scheduled email/SFTP ingestion with Slack exception alerts.

## How AI Tools Were Used

I used an AI coding assistant for initial research, module scaffolding, unit test authoring, and synthetic October/November test generation. All business rules, September 2026 ground-truth figures (audited directly against the raw PDF and ERP files), exact `.5` rounding cases, `mypy --strict`, `ruff`, automated tests, and live `gemini-3.5-flash-lite` / `deepseek-flash` runs were independently verified.
