"""Lightweight HTTP request guards: per-IP sliding-window rate limiter and upload size validator."""

import time
from collections import deque

from fastapi import HTTPException, Request, UploadFile

_BYTES_PER_MB = 1024 * 1024


def resolve_client_ip(request: Request) -> str:
    """Resolve the caller IP from X-Forwarded-For (behind reverse proxy) or direct client socket."""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_hop = forwarded_for.split(",", 1)[0].strip()
        if first_hop:
            return first_hop
    return request.client.host if request.client is not None else "unknown"


class InMemoryRateLimiter:
    """Single-process sliding-window per-IP rate limiter protecting LLM endpoints from accidental loops or abuse."""

    def __init__(self, max_requests: int = 10, window_seconds: int = 60) -> None:
        self._max_requests = max_requests
        self._window_seconds = float(window_seconds)
        self._timestamps_by_ip: dict[str, deque[float]] = {}

    def enforce(self, client_ip: str) -> None:
        """Record one request for client_ip or raise HTTP 429 if the sliding window is exhausted."""
        now = time.monotonic()
        cutoff = now - self._window_seconds
        timestamps = self._timestamps_by_ip.setdefault(client_ip, deque())
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= self._max_requests:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({self._max_requests} requests per {int(self._window_seconds)}s). Please retry shortly.",
            )
        timestamps.append(now)


async def validate_uploaded_reconciliation_files(
    pdf_file: UploadFile,
    erp_file: UploadFile,
    *,
    max_pdf_mb: int = 25,
    max_erp_mb: int = 10,
) -> tuple[str, bytes, str, bytes]:
    """Validate uploaded PDF and ERP Excel extensions, non-empty content, and maximum byte sizes."""
    pdf_filename = pdf_file.filename or "supplier_invoices.pdf"
    erp_filename = erp_file.filename or "erp_export.xlsx"
    if not pdf_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Supplier document file must have a .pdf extension.")
    if not erp_filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=422, detail="ERP export file must have a .xlsx extension.")

    pdf_bytes = await pdf_file.read()
    erp_bytes = await erp_file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=422, detail="Uploaded PDF file is empty.")
    if not erp_bytes:
        raise HTTPException(status_code=422, detail="Uploaded ERP Excel file is empty.")
    if len(pdf_bytes) > max_pdf_mb * _BYTES_PER_MB:
        raise HTTPException(status_code=413, detail=f"Uploaded PDF exceeds the {max_pdf_mb} MB limit.")
    if len(erp_bytes) > max_erp_mb * _BYTES_PER_MB:
        raise HTTPException(status_code=413, detail=f"Uploaded ERP Excel exceeds the {max_erp_mb} MB limit.")
    return pdf_filename, pdf_bytes, erp_filename, erp_bytes
