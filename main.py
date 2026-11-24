"""CLI entry point for running AP reconciliation batches or starting the Review Queue UI."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn

from app.kernel import build_reconciliation_service, create_web_application
from app.llm.client import UpstreamUnavailable
from app.schemas.erp import ErpValidationError
from app.services.reconciliation_service import ReconciliationService
from config.settings import Settings


def _configure_logging(verbose: bool) -> None:
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def _run_reconcile_command(pdf_path: Path, erp_path: Path, output_dir: Path) -> int:
    service: ReconciliationService | None = None
    try:
        settings = Settings()
        service = build_reconciliation_service(settings)
        result = await service.reconcile(pdf_path, erp_path, output_dir=output_dir)
    except ErpValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError, PermissionError) as exc:
        print(f"Input or output error: {exc}", file=sys.stderr)
        return 2
    except UpstreamUnavailable as exc:
        print(f"Upstream LLM error: {exc}", file=sys.stderr)
        return 3
    finally:
        if service is not None:
            await service.close()

    totals = result.report.control_totals
    print(f"Reconciled {totals.pdf_document_count} PDF docs vs {totals.erp_entry_count} ERP entries")
    print(f"PDF Net Total:        Rp {totals.raw_pdf_net_total:,.0f}")
    print(
        f"ERP Net Total:        Rp {totals.raw_erp_net_total:,.0f} "
        f"(Posted {totals.erp_posted_net_total:,.0f} | Parked {totals.erp_parked_net_total:,.0f})"
    )
    print(f"Net Difference:       Rp {totals.net_difference:,.0f}")
    print(f"Unexplained Variance: Rp {totals.unexplained_variance:,.0f}")
    print("Status breakdown:")
    for status_name, count in sorted(totals.status_counts.items()):
        variance = totals.explained_by_status.get(status_name, 0)
        print(f"  {status_name:26s} count={count:2d}  variance=Rp {variance:,.0f}")
    if result.report.usage is not None:
        usage = result.report.usage
        total_in = usage.input_tokens + usage.retry_input_tokens
        total_out = usage.output_tokens + usage.retry_output_tokens
        cost_label = f"${usage.estimated_cost_usd:.5f}" if usage.estimated_cost_usd is not None else "n/a"
        fallback_note = (
            f" fallback={usage.fallback_calls}x ({usage.fallback_model}) |"
            if usage.fallback_calls > 0 and usage.fallback_model
            else ""
        )
        print(
            f"Extraction: {usage.provider}/{usage.model} (retry={usage.retry_model}) | "
            f"calls={usage.primary_calls}+{usage.retry_calls} cached={usage.cached_pages} |{fallback_note} "
            f"tokens={total_in:,} in / {total_out:,} out | cost={cost_label} | {usage.latency_ms / 1000:.2f}s"
        )
    print(f"Reports written to: {output_dir.resolve()}")
    return 0


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ap-reconcile", description="Supplier PDF to ERP AP reconciliation engine."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    reconcile_cmd = subparsers.add_parser(
        "reconcile", help="Reconcile a supplier PDF batch against an ERP AP Excel export"
    )
    reconcile_cmd.add_argument("--pdf", required=True, type=Path, help="Path to supplier invoice PDF")
    reconcile_cmd.add_argument("--erp", required=True, type=Path, help="Path to ERP AP Excel export (.xlsx)")
    reconcile_cmd.add_argument(
        "--out", default=Path("output/latest"), type=Path, help="Output directory for Excel and JSON reports"
    )

    serve_cmd = subparsers.add_parser("serve", help="Start the local AP Review Queue web UI")
    serve_cmd.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    serve_cmd.add_argument("--port", default=8000, type=int, help="Bind port (default 8000)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if args.command == "reconcile":
        return asyncio.run(_run_reconcile_command(args.pdf, args.erp, args.out))
    if args.command == "serve":
        uvicorn.run(create_web_application(), host=args.host, port=args.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
