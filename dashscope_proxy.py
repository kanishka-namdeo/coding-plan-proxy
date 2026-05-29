import json
import os
import time
import random
import asyncio
import signal
import logging
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from collections import deque
from aiohttp import web
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8899
TARGET_BASE = "https://coding-intl.dashscope.aliyuncs.com"

# ---------------------------------------------------------------------------
# Security: API key from environment
# ---------------------------------------------------------------------------
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UPSTREAM_TIMEOUT_TOTAL = 120
UPSTREAM_TIMEOUT_CONNECT = 10
MAX_CONNECTIONS = 200
MAX_CONNECTIONS_PER_HOST = 50
MAX_BODY_SIZE = 10 * 1024 * 1024
MAX_5XX_RETRIES = 3
DEQUE_MAX_SIZE = 100_000

HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "transfer-encoding", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "upgrade", "content-length",
})

CODING_PLAN_CONFIG = {
    "rpm_limit": 2400,
    "tpm_limit": 4_000_000,
    "safety_factor": 0.8,
    "requests_per_5h": 6000,
    "requests_per_week": 45000,
    "requests_per_month": 90000,
    "max_queue_size": 200,
    "max_retries": 10,
    "base_backoff": 2.0,
}

MOCK_MODELS = {
    "object": "list",
    "data": [
        {"id": "qwen3.6-plus", "object": "model"},
        {"id": "qwen3.5-plus", "object": "model"},
        {"id": "qwen3-max", "object": "model"},
        {"id": "qwen3-coder-plus", "object": "model"},
        {"id": "kimi-k2-5", "object": "model"},
        {"id": "glm-5-0", "object": "model"},
        {"id": "MiniMax-M2.5", "object": "model"},
    ]
}


class SlidingWindowCounter:
    """Tracks count of events within a sliding time window with bounded memory."""

    def __init__(self, window_seconds: int, max_size: int = DEQUE_MAX_SIZE):
        self.window = window_seconds
        self.max_size = max_size
        self.events: deque[float] = deque()

    def add(self, now: float | None = None):
        now = now or time.monotonic()
        self.events.append(now)
        self._prune(now)

    def count(self, now: float | None = None) -> int:
        now = now or time.monotonic()
        self._prune(now)
        return len(self.events)

    def _prune(self, now: float):
        cutoff = now - self.window
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        if len(self.events) > self.max_size:
            excess = len(self.events) - self.max_size
            for _ in range(excess):
                self.events.popleft()


class RateLimiter:
    """
    Multi-layer rate limiter for DashScope Coding Plan.
    Thread-safe via asyncio.Lock for concurrent request handling.
    """

    def __init__(self, config: dict):
        sf = config["safety_factor"]
        self.rpm_limit = int(config["rpm_limit"] * sf)
        self.tpm_limit = int(config["tpm_limit"] * sf)
        self.rps_limit = max(1, int(self.rpm_limit / 60 * sf))

        self.rpm_window = SlidingWindowCounter(60)
        self.hour5_window = SlidingWindowCounter(5 * 3600)

        self.week_count = 0
        self.month_count = 0
        self.week_start = time.time()
        self.month_start = time.time()

        self.week_limit = config["requests_per_week"]
        self.month_limit = config["requests_per_month"]
        self.hour5_limit = config["requests_per_5h"]

        self.max_queue_size = config["max_queue_size"]
        self.max_retries = config["max_retries"]
        self.base_backoff = config["base_backoff"]
        self.pending_requests = 0

        self.total_forwarded = 0
        self.total_queued = 0
        self.total_429s = 0
        self.total_rejected = 0
        self.total_tokens_consumed = 0

        self.last_request_time: float = 0.0
        self._lock = asyncio.Lock()

    async def can_proceed(self) -> tuple[bool, str, float]:
        async with self._lock:
            now_mono = time.monotonic()
            now_wall = time.time()

            if now_wall - self.week_start >= 7 * 24 * 3600:
                self.week_count = 0
                self.week_start = now_wall
            if now_wall - self.month_start >= 30 * 24 * 3600:
                self.month_count = 0
                self.month_start = now_wall

            h5_count = self.hour5_window.count(now_mono)
            if h5_count >= self.hour5_limit:
                oldest = self.hour5_window.events[0] if self.hour5_window.events else now_mono
                wait = max(0, oldest + 5 * 3600 - now_mono)
                return False, "5-hour quota exhausted", wait

            if self.week_count >= self.week_limit:
                return False, "Weekly quota exhausted", 60

            if self.month_count >= self.month_limit:
                return False, "Monthly quota exhausted", 60

            rpm_count = self.rpm_window.count(now_mono)
            if rpm_count >= self.rpm_limit:
                wait = 60.0 / self.rpm_limit
                return False, "RPM limit reached", wait

            min_gap = 1.0 / self.rps_limit
            time_since_last = now_mono - self.last_request_time
            if time_since_last < min_gap:
                return False, "RPS spacing", min_gap - time_since_last

            return True, "ok", 0.0

    async def record_request(self, tokens_used: int = 0, now: float | None = None):
        async with self._lock:
            now = now or time.monotonic()
            self.rpm_window.add(now)
            self.hour5_window.add(now)
            self.week_count += 1
            self.month_count += 1
            self.total_forwarded += 1
            self.total_tokens_consumed += tokens_used
            self.last_request_time = now

    async def remaining_tpm(self) -> int:
        async with self._lock:
            now = time.monotonic()
            used = sum(
                estimate_tokens_for_request_cached(e)
                for e in self.rpm_window.events
            ) if self.rpm_window.events else 0
            return max(0, self.tpm_limit - used)

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "rps_limit": self.rps_limit,
            "rpm_limit": self.rpm_limit,
            "rpm_current": self.rpm_window.count(now),
            "requests_5h": self.hour5_window.count(now),
            "requests_5h_limit": self.hour5_limit,
            "requests_week": self.week_count,
            "requests_week_limit": self.week_limit,
            "requests_month": self.month_count,
            "requests_month_limit": self.month_limit,
            "total_forwarded": self.total_forwarded,
            "total_queued": self.total_queued,
            "total_429s": self.total_429s,
            "total_rejected": self.total_rejected,
            "total_tokens_consumed": self.total_tokens_consumed,
            "pending_requests": self.pending_requests,
        }


_token_cache: dict[int, int] = {}


def estimate_tokens_for_request_cached(ts: float) -> int:
    """Cached rough token estimate per tracked event (simplified)."""
    return 100


async def wait_for_slot(rate_limiter: RateLimiter) -> float | None:
    """
    Wait until the rate limiter allows a request through.
    Returns total wait time, or None if queue is full.
    """
    if rate_limiter.pending_requests > rate_limiter.max_queue_size:
        return None

    total_wait = 0.0
    while True:
        allowed, reason, wait = await rate_limiter.can_proceed()
        if allowed:
            return total_wait

        if rate_limiter.pending_requests > rate_limiter.max_queue_size:
            return None

        if "quota" in reason.lower():
            jitter = random.uniform(0.5, 2.0)
            wait_time = max(wait, 5.0) + jitter
        else:
            jitter = random.uniform(0.05, 0.2)
            wait_time = wait + jitter

        total_wait += wait_time
        logger.info(
            "Rate limited: %s. Waiting %.1fs before retry...",
            reason, wait_time
        )
        await asyncio.sleep(wait_time)


def extract_tokens_from_response(body: bytes) -> int:
    """Parse token usage from a JSON response body."""
    try:
        data = json.loads(body)
        usage = data.get("usage", {})
        return usage.get("total_tokens", 0)
    except (json.JSONDecodeError, AttributeError):
        return 0


def extract_tokens_from_stream(buffer: bytes) -> int:
    """Parse token usage from accumulated SSE stream buffer."""
    try:
        lines = buffer.decode("utf-8", errors="replace").split("\n")
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                data = json.loads(line[6:])
                usage = data.get("usage", {})
                if usage and "total_tokens" in usage:
                    return usage["total_tokens"]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return 0


def estimate_tokens_for_request(body_bytes: bytes) -> int:
    """Rough estimate of tokens in the request body for TPM planning."""
    try:
        body = json.loads(body_bytes)
        messages = body.get("messages", [])
        if not isinstance(messages, list):
            return 100
        total_chars = sum(
            len(m.get("content", ""))
            for m in messages
            if isinstance(m.get("content"), str)
        )
        return max(100, total_chars // 4)
    except (json.JSONDecodeError, AttributeError):
        return 100


def map_developer_to_system(body: dict) -> dict:
    """Convert 'developer' role to 'system', handling multi-modal and malformed messages."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        if messages is not None:
            logger.warning("messages field is not a list (type=%s), skipping role mapping", type(messages).__name__)
        return body
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "developer":
            msg["role"] = "system"
        elif not isinstance(msg, dict):
            logger.warning("Non-dict message entry found (type=%s), skipping", type(msg).__name__)
    return body


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


def _is_chat_endpoint(path: str) -> bool:
    """Check if path is a chat completion endpoint."""
    return "chat/completions" in path.lower()


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


def _strip_hop_by_hop(headers: dict) -> dict:
    """Remove hop-by-hop headers from a headers dict."""
    return {
        k: v for k, v in headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }


async def handle_request(request: web.Request) -> web.StreamResponse:
    path = request.path
    method = request.method
    rate_limiter = request.app["rate_limiter"]

    request_id = request.headers.get("X-Request-ID", f"{uuid.uuid4().hex[:12]}")
    log_prefix = f"[{request_id}]"

    app_shutting_down = request.app.get("shutting_down")
    if app_shutting_down and app_shutting_down.is_set():
        return _make_error_response(
            503, b'{"error":"shutting down"}', request_id, retry_after=30
        )

    # Health / Ready endpoints — no rate limiting, no stats
    if path == "/health" and method == "GET":
        return web.json_response({"status": "ok"})
    if path == "/ready" and method == "GET":
        session = request.app.get("client_session")
        ready = session is not None and not session.closed
        status_code = 200 if ready else 503
        return web.json_response({"status": "ready" if ready else "not_ready"}, status=status_code)

    if path == "/v1/proxy/status" and method == "GET":
        return web.json_response(rate_limiter.status())

    if method == "GET" and path in ("/v1/models", "/models"):
        logger.info("%s Intercepted GET %s -> returning mock model list", log_prefix, path)
        resp = web.json_response(MOCK_MODELS)
        resp.headers["X-Request-ID"] = request_id
        return resp

    body_bytes = await request.read()

    if method == "POST" and not body_bytes:
        return _make_error_response(400, b'{"error":"empty request body"}', request_id)

    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        return _make_error_response(405, b'{"error":"method not allowed"}', request_id)

    if _is_chat_endpoint(path) and method != "POST":
        return _make_error_response(405, b'{"error":"method not allowed"}', request_id)

    if len(body_bytes) > MAX_BODY_SIZE:
        return _make_error_response(413, b'{"error":"payload too large"}', request_id)

    is_stream = b'"stream": true' in body_bytes
    body = None
    if body_bytes and method == "POST":
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError:
            return _make_error_response(400, b'{"error":"invalid JSON"}', request_id)

        if not isinstance(body, dict):
            return _make_error_response(400, b'{"error":"request body must be a JSON object"}', request_id)

        if "model" not in body:
            return _make_error_response(400, b'{"error":"missing required field: model"}', request_id)

        messages = body.get("messages")
        if messages is None or (isinstance(messages, list) and len(messages) == 0):
            return _make_error_response(400, b'{"error":"missing required field: messages"}', request_id)

        body = map_developer_to_system(body)
        try:
            body_bytes = json.dumps(body).encode()
        except (TypeError, ValueError) as e:
            return _make_error_response(400, json.dumps({"error": f"unserializable body: {e}"}).encode(), request_id)

    estimated_tokens = estimate_tokens_for_request(body_bytes) if body_bytes else 0

    rate_limiter.pending_requests += 1
    try:
        wait_time = await wait_for_slot(rate_limiter)
        if wait_time is None:
            rate_limiter.total_rejected += 1
            retry_sec = max(1, rate_limiter.pending_requests // max(1, rate_limiter.rps_limit))
            return _make_error_response(
                503,
                json.dumps({"error": "queue full", "retry_after": retry_sec}).encode(),
                request_id,
                retry_after=retry_sec,
            )

        if wait_time > 0:
            rate_limiter.total_queued += 1
            logger.info("%s Request queued for %.1fs", log_prefix, wait_time)
    except Exception:
        rate_limiter.pending_requests -= 1
        raise

    target_path = path if path.startswith("/v1") else f"/v1{path}"
    target_url = f"{TARGET_BASE}{target_path}"
    if request.query_string:
        target_url += f"?{request.query_string}"

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "transfer-encoding", "content-length", "authorization")
    }
    headers["Content-Type"] = "application/json"
    headers["Authorization"] = f"Bearer {DASHSCOPE_API_KEY}"
    headers["X-Request-ID"] = request_id

    logger.info("%s %s %s -> %s", log_prefix, method, path, target_url)

    retry = 0
    retry_5xx = 0

    try:
        while retry + retry_5xx <= rate_limiter.max_retries:
            try:
                if _client_disconnected(request):
                    logger.info("%s Client disconnected, aborting", log_prefix)
                    return _make_error_response(499, b'{"error":"client disconnected"}', request_id)

                if is_stream:
                    upstream = await request.app["client_session"].request(
                        method=method,
                        url=target_url,
                        headers=headers,
                        data=body_bytes,
                    )
                    try:
                        if upstream.status == 429:
                            rate_limiter.total_429s += 1
                            error_body = await upstream.read()
                            retry += 1
                            if retry > rate_limiter.max_retries:
                                logger.error("%s Max retries exceeded after 429", log_prefix)
                                resp = web.Response(status=429, body=error_body, content_type="application/json")
                                resp.headers["X-Request-ID"] = request_id
                                return resp

                            retry_wait = _compute_backoff(rate_limiter, retry)
                            logger.info(
                                "%s 429 (attempt %d/%d). Backing off %.1fs",
                                log_prefix, retry, rate_limiter.max_retries, retry_wait
                            )
                            await asyncio.sleep(retry_wait)
                            continue

                        if 500 <= upstream.status < 600 and not is_stream:
                            upstream_body = await upstream.read()
                            retry_5xx += 1
                            if retry_5xx > MAX_5XX_RETRIES:
                                logger.warning("%s Upstream %d after %d retries", log_prefix, upstream.status, MAX_5XX_RETRIES)
                                resp = web.Response(status=upstream.status, body=upstream_body)
                                resp.headers["X-Request-ID"] = request_id
                                _add_forwarded_headers(resp, upstream)
                                return resp

                            retry_wait = 1.0 * (2 ** retry_5xx) * random.uniform(0.5, 1.5)
                            logger.warning(
                                "%s Upstream %d (retry %d/%d). Backing off %.1fs",
                                log_prefix, upstream.status, retry_5xx, MAX_5XX_RETRIES, retry_wait
                            )
                            await asyncio.sleep(retry_wait)
                            continue

                        stream_buffer = b""
                        tokens_from_stream = 0

                        resp = web.StreamResponse(
                            status=upstream.status,
                            headers={"Content-Type": "text/event-stream"},
                        )
                        resp.headers["X-Request-ID"] = request_id
                        await resp.prepare(request)
                        try:
                            async for chunk in upstream.content:
                                if _client_disconnected(request):
                                    logger.info("%s Client disconnected mid-stream, aborting", log_prefix)
                                    upstream.close()
                                    return _make_error_response(499, b'', request_id)
                                stream_buffer += chunk
                                await resp.write(chunk)
                        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                            logger.info("%s Client disconnected during stream, aborting", log_prefix)
                            upstream.close()
                            return _make_error_response(499, b'', request_id)

                        await resp.write_eof()

                        tokens_from_stream = extract_tokens_from_stream(stream_buffer)
                        if tokens_from_stream == 0:
                            tokens_from_stream = estimated_tokens

                        await rate_limiter.record_request(tokens_from_stream)
                        return resp
                    except Exception:
                        upstream.close()
                        raise

                else:
                    async with request.app["client_session"].request(
                        method=method,
                        url=target_url,
                        headers=headers,
                        data=body_bytes,
                    ) as resp_up:
                        resp_body = await resp_up.read()
                        resp_headers = dict(resp_up.headers)
                        status_code = resp_up.status

                    if status_code == 429:
                        rate_limiter.total_429s += 1
                        retry += 1
                        if retry > rate_limiter.max_retries:
                            logger.error("%s Max retries exceeded after 429", log_prefix)
                            out = web.Response(status=429, body=resp_body, content_type="application/json")
                            out.headers["X-Request-ID"] = request_id
                            return out

                        retry_after_raw = resp_headers.get("Retry-After")
                        if retry_after_raw:
                            parsed = parse_retry_after(retry_after_raw)
                            if parsed is not None:
                                wait = parsed
                            else:
                                wait = _compute_backoff(rate_limiter, retry)
                        else:
                            wait = _compute_backoff(rate_limiter, retry)

                        logger.info(
                            "%s 429 (attempt %d/%d). Backing off %.1fs",
                            log_prefix, retry, rate_limiter.max_retries, wait
                        )
                        await asyncio.sleep(wait)
                        continue

                    if 500 <= status_code < 600:
                        retry_5xx += 1
                        if retry_5xx > MAX_5XX_RETRIES:
                            logger.warning("%s Upstream %d after %d retries", log_prefix, status_code, MAX_5XX_RETRIES)
                            out = web.Response(status=status_code, body=resp_body)
                            out.headers["X-Request-ID"] = request_id
                            _add_forwarded_headers(out, resp_headers)
                            return out

                        retry_wait = 1.0 * (2 ** retry_5xx) * random.uniform(0.5, 1.5)
                        logger.warning(
                            "%s Upstream %d (retry %d/%d). Backing off %.1fs",
                            log_prefix, status_code, retry_5xx, MAX_5XX_RETRIES, retry_wait
                        )
                        await asyncio.sleep(retry_wait)
                        continue

                    tokens_used = extract_tokens_from_response(resp_body)
                    if tokens_used == 0:
                        tokens_used = estimated_tokens
                    await rate_limiter.record_request(tokens_used)

                    if status_code != 200:
                        logger.error("%s Upstream %d: %s", log_prefix, status_code, resp_body.decode(errors="replace"))

                    content_type = resp_headers.get("Content-Type", "application/json")
                    content_type = content_type.split(";")[0].strip()
                    out = web.Response(
                        status=status_code,
                        body=resp_body,
                        content_type=content_type,
                    )
                    out.headers["X-Request-ID"] = request_id
                    _add_forwarded_headers(out, resp_headers)
                    _add_ratelimit_headers(out, rate_limiter)
                    return out

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error("%s Forward error (attempt %d): %s", log_prefix, retry + 1, e)
                retry += 1
                if retry > rate_limiter.max_retries:
                    return _make_error_response(502, b'{"error":"proxy forward error"}', request_id)
                await asyncio.sleep(_compute_backoff(rate_limiter, retry))
                continue

        return _make_error_response(502, b'{"error":"proxy forward error"}', request_id)
    finally:
        rate_limiter.pending_requests -= 1


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


def create_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.route("*", "/{tail:.*}", handle_request)])
    return app


async def main():
    if not DASHSCOPE_API_KEY:
        logger.error("DASHSCOPE_API_KEY environment variable is not set")
        raise SystemExit(1)

    config = CODING_PLAN_CONFIG
    rate_limiter = RateLimiter(config)

    app = create_app()
    timeout = aiohttp.ClientTimeout(total=UPSTREAM_TIMEOUT_TOTAL, connect=UPSTREAM_TIMEOUT_CONNECT)
    connector = aiohttp.TCPConnector(
        limit=MAX_CONNECTIONS,
        limit_per_host=MAX_CONNECTIONS_PER_HOST,
        ttl_dns_cache=300,
    )
    app["client_session"] = aiohttp.ClientSession(timeout=timeout, connector=connector)
    app["rate_limiter"] = rate_limiter
    app["shutting_down"] = asyncio.Event()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, PROXY_HOST, PROXY_PORT)
    await site.start()

    logger.info("Proxy listening on %s:%d", PROXY_HOST, PROXY_PORT)
    logger.info("Intercepting: GET /v1/models -> mock model list")
    logger.info("Forwarding all other requests to %s", TARGET_BASE)
    logger.info("Converting 'developer' role -> 'system' in messages")
    logger.info("Rate limiting enabled:")
    logger.info("  RPS limit: %d (smoothed)", rate_limiter.rps_limit)
    logger.info("  RPM limit: %d", rate_limiter.rpm_limit)
    logger.info("  TPM limit: %d", rate_limiter.tpm_limit)
    logger.info("  5-hour quota: %d", rate_limiter.hour5_limit)
    logger.info("  Weekly quota: %d", rate_limiter.week_limit)
    logger.info("  Monthly quota: %d", rate_limiter.month_limit)
    logger.info("  Safety factor: %.0f%%", config["safety_factor"] * 100)
    logger.info("Proxy status: GET http://%s:%d/v1/proxy/status", PROXY_HOST, PROXY_PORT)
    logger.info("Health: GET http://%s:%d/health", PROXY_HOST, PROXY_PORT)
    logger.info("Ready: GET http://%s:%d/ready", PROXY_HOST, PROXY_PORT)
    logger.info("Press Ctrl+C to stop")

    shutdown_event = app["shutting_down"]

    def _handle_signal():
        if not shutdown_event.is_set():
            logger.info("Received shutdown signal")
            shutdown_event.set()

    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, _handle_signal)
        loop.add_signal_handler(signal.SIGINT, _handle_signal)
    except (NotImplementedError, OSError):
        logger.info("Signal handlers not supported on this platform (SIGTERM/SIGINT via KeyboardInterrupt only)")

    try:
        await shutdown_event.wait()
    except KeyboardInterrupt:
        logger.info("Shutting down...")

    logger.info("Draining in-flight requests...")
    await asyncio.sleep(2)

    session = app.get("client_session")
    if session and not session.closed:
        await session.close()
    await runner.cleanup()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
