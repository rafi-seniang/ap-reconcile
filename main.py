"""CLI entry point for running AP reconciliation batches or starting the Review Queue UI."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn

from app.api.routes import create_app
from app.erp.schemas import ErpValidationError
from app.extraction.llm_client import UpstreamUnavailable
from app.service import ReconciliationService
from config.settings import Settings


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def _run_reconcile_command(pdf_path: Path, erp_path: Path, out_dir: Path) -> int:
    settings = Settings()
    service = ReconciliationService.from_settings(settings)
    try:
        result = await service.reconcile(pdf_path, erp_path, output_dir=out_dir)
    except ErpValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except UpstreamUnavailable as exc:
        print(f"Upstream LLM error: {exc}", file=sys.stderr)
        return 3
    finally:
        await service.close()

    ct = result.report.control_totals
    print(f"Reconciled {ct.pdf_document_count} PDF docs vs {ct.erp_entry_count} ERP entries")
    print(f"PDF Net Total:       Rp {ct.raw_pdf_net_total:,.0f}")
    print(
        f"ERP Net Total:       Rp {ct.raw_erp_net_total:,.0f} (Posted {ct.erp_posted_net_total:,.0f} | Parked {ct.erp_parked_net_total:,.0f})"
    )
    print(f"Net Difference:      Rp {ct.net_difference:,.0f}")
    print(f"Unexplained Variance: Rp {ct.unexplained_variance:,.0f}")
    print("Status breakdown:")
    for status_name, count in sorted(ct.status_counts.items()):
        variance = ct.explained_by_status.get(status_name, 0)
        print(f"  {status_name:26s} count={count:2d}  variance=Rp {variance:,.0f}")
    if result.report.usage is not None:
        u = result.report.usage
        total_in = u.input_tokens + u.retry_input_tokens
        total_out = u.output_tokens + u.retry_output_tokens
        cost_txt = f"${u.estimated_cost_usd:.5f}" if u.estimated_cost_usd is not None else "n/a"
        print(
            f"Extraction: {u.provider}/{u.model} (retry={u.retry_model}) | "
            f"calls={u.primary_calls}+{u.retry_calls} cached={u.cached_pages} | "
            f"tokens={total_in:,} in / {total_out:,} out | cost={cost_txt} | {u.latency_ms / 1000:.2f}s"
        )
    print(f"Reports written to: {out_dir.resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ap-reconcile", description="Supplier PDF to ERP AP reconciliation engine."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("reconcile", help="Reconcile a supplier PDF batch against an ERP AP Excel export")
    rec.add_argument("--pdf", required=True, type=Path, help="Path to supplier invoice PDF")
    rec.add_argument("--erp", required=True, type=Path, help="Path to ERP AP Excel export (.xlsx)")
    rec.add_argument(
        "--out", default=Path("output/latest"), type=Path, help="Output directory for Excel/CSV/JSON reports"
    )

    srv = sub.add_parser("serve", help="Start the local AP Review Queue web UI")
    srv.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    srv.add_argument("--port", default=8000, type=int, help="Bind port (default 8000)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if args.command == "reconcile":
        return asyncio.run(_run_reconcile_command(args.pdf, args.erp, args.out))
    if args.command == "serve":
        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
