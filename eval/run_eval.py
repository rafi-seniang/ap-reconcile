"""Run and score reconciliation against the verified September 2026 ground truth."""

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.kernel import build_reconciliation_service
from app.schemas.reconciliation import DocStatus, ReconciliationReport
from config.settings import Settings

DEFAULT_SEP_DIR = Path(os.environ.get("SEP_DATA_DIR", "data/sep2026"))
GROUND_TRUTH_PATH = Path(__file__).parent / "ground_truth_sep2026.json"


@dataclass(frozen=True)
class ExpectedDoc:
    doc_key: str
    status: DocStatus
    pdf_total: Decimal
    erp_total: Decimal
    required_reason_codes: tuple[str, ...] = ()
    expected_line_diff_skus: tuple[str, ...] = ()


def load_ground_truth(path: Path = GROUND_TRUTH_PATH) -> tuple[ExpectedDoc, ...]:
    """Load audited expected document statuses, totals, and line discrepancies from JSON."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        ExpectedDoc(
            doc_key=item["doc_key"],
            status=DocStatus(item["status"]),
            pdf_total=Decimal(item["pdf_total"]),
            erp_total=Decimal(item["erp_total"]),
            required_reason_codes=tuple(item.get("required_reason_codes", [])),
            expected_line_diff_skus=tuple(item.get("expected_line_diff_skus", [])),
        )
        for item in payload["documents"]
    )


EXPECTED_SEP_2026: tuple[ExpectedDoc, ...] = load_ground_truth()


def score_report(report: ReconciliationReport, expected: tuple[ExpectedDoc, ...]) -> dict[str, Any]:
    """Compare a ReconciliationReport against expected document statuses, totals, and line diffs."""
    actual_by_key = {doc.doc_number_norm: doc for doc in report.documents}
    status_matches = 0
    total_matches = 0
    line_diff_matches = 0
    mismatches: list[dict[str, Any]] = []

    for exp in expected:
        actual = actual_by_key.get(exp.doc_key)
        if actual is None:
            mismatches.append({"doc_key": exp.doc_key, "issue": "missing from reconciled output"})
            continue

        st_ok = actual.status == exp.status
        tot_ok = actual.pdf_total_signed == exp.pdf_total and actual.erp_total_signed == exp.erp_total
        reasons_ok = all(code in actual.reason_codes for code in exp.required_reason_codes)
        actual_skus = tuple(sorted(ld.item_code or "" for ld in actual.line_discrepancies))
        expected_skus = tuple(sorted(exp.expected_line_diff_skus))
        lines_ok = actual_skus == expected_skus

        if st_ok:
            status_matches += 1
        if tot_ok:
            total_matches += 1
        if lines_ok:
            line_diff_matches += 1

        if not (st_ok and tot_ok and reasons_ok and lines_ok):
            mismatches.append(
                {
                    "doc_key": exp.doc_key,
                    "expected_status": exp.status.value,
                    "actual_status": actual.status.value,
                    "expected_pdf_total": str(exp.pdf_total),
                    "actual_pdf_total": str(actual.pdf_total_signed),
                    "expected_reason_codes": list(exp.required_reason_codes),
                    "actual_reason_codes": actual.reason_codes,
                    "expected_line_skus": list(expected_skus),
                    "actual_line_skus": list(actual_skus),
                }
            )

    extra_keys = sorted(set(actual_by_key.keys()) - {e.doc_key for e in expected})
    if extra_keys:
        mismatches.append({"extra_unexpected_keys": extra_keys})

    ct = report.control_totals
    return {
        "total_expected_docs": len(expected),
        "status_matches": status_matches,
        "exact_total_matches": total_matches,
        "line_discrepancy_matches": line_diff_matches,
        "unexplained_variance": str(ct.unexplained_variance),
        "pdf_net_total": str(ct.raw_pdf_net_total),
        "erp_net_total": str(ct.raw_erp_net_total),
        "net_difference": str(ct.net_difference),
        "mismatches": mismatches,
        "usage": report.usage.model_dump(mode="json") if report.usage else None,
    }


async def _run(args: argparse.Namespace) -> int:
    overrides: dict[str, Any] = {}
    if args.model:
        overrides["llm_model"] = args.model
    if args.retry_model is not None:
        overrides["llm_retry_model"] = args.retry_model or None
    if args.base_url:
        overrides["llm_base_url"] = args.base_url
    if args.input_price is not None:
        overrides["llm_input_price_per_million_usd"] = args.input_price
    if args.output_price is not None:
        overrides["llm_output_price_per_million_usd"] = args.output_price
    if args.retry_input_price is not None:
        overrides["llm_retry_input_price_per_million_usd"] = args.retry_input_price
    if args.retry_output_price is not None:
        overrides["llm_retry_output_price_per_million_usd"] = args.retry_output_price
    if args.cache_dir:
        overrides["extraction_cache_dir"] = args.cache_dir

    settings = Settings(**overrides)
    service = build_reconciliation_service(settings)
    try:
        result = await service.reconcile(args.pdf, args.erp, output_dir=args.out)
    finally:
        await service.close()

    metrics = score_report(result.report, EXPECTED_SEP_2026)
    args.out.mkdir(parents=True, exist_ok=True)
    metrics_path = args.out / "eval_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    print(json.dumps(metrics, indent=2))
    return 0 if not metrics["mismatches"] and metrics["unexplained_variance"] == "0" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate AP reconciliation on September 2026 dataset.")
    parser.add_argument(
        "--pdf",
        type=Path,
        default=DEFAULT_SEP_DIR / "Supplier_Invoices_PT_Prima_Kertas_Sep2026.pdf",
    )
    parser.add_argument(
        "--erp",
        type=Path,
        default=DEFAULT_SEP_DIR / "ERP_AP_Export_Sep2026.xlsx",
    )
    parser.add_argument("--out", type=Path, default=Path("output/eval_sep2026"))
    parser.add_argument("--model", default=None)
    parser.add_argument("--retry-model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--input-price", type=float, default=None)
    parser.add_argument("--output-price", type=float, default=None)
    parser.add_argument("--retry-input-price", type=float, default=None)
    parser.add_argument("--retry-output-price", type=float, default=None)
    parser.add_argument("--cache-dir", default="eval/cache")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
