"""HTTP helper utilities for the DashScope proxy."""

import asyncio
import json
import random
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

from dashscope_proxy_lib.config import HOP_BY_HOP_HEADERS
from dashscope_proxy_lib.rate_limiter import RateLimiter

# Substrings in 429 bodies that indicate a hard quota limit (not transient rate limiting).
_NON_RETRYABLE_429_MARKERS = (
    "quota exceeded",
    "allocated quota",
    "insufficient_quota",
    "billing_hard_limit",
    "exceeded your current quota",
)


def parse_retry_after(header_value: str) -> float | None:
    """Parse Retry-After header: delta-seconds or HTTP-date."""
    try:
        return float(header_value)
    except ValueError:
        pass
    try:
        parsed_dt = parsedate_to_datetime(header_value)
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        delta = (parsed_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.5, delta)
    except (TypeError, ValueError):
        return None


def _make_error_response(status: int, body: bytes, request_id: str, retry_after: int | None = None) -> web.Response:
    """Create an error response with standard headers."""
    headers = {"X-Request-ID": request_id}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return web.Response(status=status, body=body, headers=headers, content_type="application/json")


def _add_ratelimit_headers(response: web.Response, rate_limiter: RateLimiter) -> None:
    """Add X-RateLimit-* headers to a response."""
    now = time.monotonic()
    rpm_current = rate_limiter.rpm_window.count(now)
    response.headers["X-RateLimit-Limit"] = str(rate_limiter.rpm_limit)
    response.headers["X-RateLimit-Remaining"] = str(max(0, rate_limiter.rpm_limit - rpm_current))
    tpm_avail = rate_limiter.tpm_bucket.available(now)
    response.headers["X-RateLimit-Tokens-Remaining"] = str(int(tpm_avail))


def _strip_hop_by_hop(headers: dict) -> dict:
    """Remove hop-by-hop headers from a headers dict."""
    return {
        k: v for k, v in headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }


def _client_disconnected(request: web.Request) -> bool:
    """Check if the client has disconnected."""
    try:
        protocol = getattr(request, "protocol", None)
        if protocol is not None:
            transport = getattr(protocol, "transport", None)
            if transport is not None:
                return transport.is_closing()
    except Exception:
        pass
    return False


def _compute_backoff(rate_limiter: RateLimiter, attempt: int) -> float:
    return rate_limiter.base_backoff * (2 ** attempt) * random.uniform(0.5, 1.5)


def should_retry_429(error_body: bytes) -> bool:
    """Return False when upstream 429 indicates a hard quota limit (do not retry)."""
    if not error_body:
        return True
    text = error_body.decode(errors="replace").lower()
    if any(marker in text for marker in _NON_RETRYABLE_429_MARKERS):
        return False
    try:
        payload = json.loads(error_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code", "")).lower()
        message = str(error.get("message", "")).lower()
        if code in ("insufficient_quota", "billing_hard_limit", "quota_exceeded"):
            return False
        if any(marker in message for marker in _NON_RETRYABLE_429_MARKERS):
            return False
    return True


async def _sleep_interruptible(request: web.Request, seconds: float, chunk: float = 0.5) -> bool:
    """Sleep up to *seconds*, checking for client disconnect. Returns False if disconnected."""
    remaining = max(0.0, seconds)
    while remaining > 0:
        if _client_disconnected(request):
            return False
        step = min(chunk, remaining)
        await asyncio.sleep(step)
        remaining -= step
    return not _client_disconnected(request)


def _upstream_error_response(
    status: int,
    body: bytes,
    upstream_headers: dict,
    request_id: str,
) -> web.Response:
    """Build a proxied error response from upstream status/body/headers."""
    content_type = upstream_headers.get("Content-Type", "application/json")
    content_type = content_type.split(";")[0].strip()
    resp = web.Response(status=status, body=body, content_type=content_type)
    resp.headers["X-Request-ID"] = request_id
    _add_forwarded_headers(resp, upstream_headers)
    return resp


def _add_forwarded_headers(response: web.Response, upstream_headers: dict | aiohttp.ClientResponse) -> None:
    """Forward meaningful upstream headers, stripping hop-by-hop."""
    if isinstance(upstream_headers, aiohttp.ClientResponse):
        hdrs = dict(upstream_headers.headers)
    else:
        hdrs = dict(upstream_headers)
    clean = _strip_hop_by_hop(hdrs)
    for k, v in clean.items():
        if k.lower() not in ("content-type", "content-length", "transfer-encoding", "content-encoding"):
            response.headers[k] = v


def _sse_response_headers() -> dict[str, str]:
    """Headers required for SSE to stream correctly through reverse proxies/tunnels."""
    return {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


async def _finalize_stream_response(resp: web.StreamResponse) -> web.StreamResponse:
    """Finish a prepared stream response; ignore errors if the client already left."""
    try:
        await resp.write_eof()
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, asyncio.CancelledError):
        pass
    return resp
