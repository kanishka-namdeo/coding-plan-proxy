"""HTTP helper utilities for the DashScope proxy."""

import random
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

from dashscope_proxy_lib.config import HOP_BY_HOP_HEADERS
from dashscope_proxy_lib.rate_limiter import RateLimiter


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
    transport = getattr(request, "transport", None)
    if transport is not None:
        return getattr(transport, "is_closing", lambda: False)()
    return False


def _compute_backoff(rate_limiter: RateLimiter, attempt: int) -> float:
    return rate_limiter.base_backoff * (2 ** attempt) * random.uniform(0.5, 1.5)


def _add_forwarded_headers(response: web.Response, upstream_headers: dict | aiohttp.ClientResponse) -> None:
    """Forward meaningful upstream headers, stripping hop-by-hop."""
    if isinstance(upstream_headers, aiohttp.ClientResponse):
        hdrs = dict(upstream_headers.headers)
    else:
        hdrs = dict(upstream_headers)
    clean = _strip_hop_by_hop(hdrs)
    for k, v in clean.items():
        if k.lower() not in ("content-type", "content-length", "transfer-encoding"):
            response.headers[k] = v
