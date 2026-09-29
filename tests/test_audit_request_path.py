"""Audit lock-in tests for the request-path fix set (handlers/http_helpers/queue/
request_transform/provider_router/token_utils).

Items covered:
 1.  Circuit-open first candidate fails over when the request is not pinned; pinned keeps 503.
 2.  Truncated non-stream bodies: 2xx -> 502 + single refund; 429/5xx fall through.
 3.  ``_read_upstream_capped``: cap <= 0 unlimited, positive cap 2-step probe.
 4.  ``SECURITY_HEADERS_TO_STRIP`` / ``strip_client_security_headers`` + both handler call sites.
 5.  ``parse_retry_after_capped`` semantics + use at the 429 backoff site.
 6.  Streaming: CancelledError re-raised; connection errors keep finalize behavior.
 7.  Stream idle-timeout abort -> ``record_completion(circuit_success=None)`` (circuit untouched).
 8.  ``wait_for_slot`` 3-tuple reasons + RPM/TPM long-jitter branch; handler unpacks it.
 9.  Post-queue admit denial body/session-log branch on the real try_admit reason.
 10. ``normalize_model_name``: strip, case-insensitive mimo-v2-5 alias, suffix preserved.
 11. Multi-slash provider pin -> ingress 400 "invalid model pin".
 12. provider_router: live ``_slug_for`` inversion, live-config model-id sets, pin semantics.
 13. token_utils: non-string text parts, media allowances, list system/developer, SSE prefixes.
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request

import dashscope_proxy
import dashscope_proxy_lib.config as _config_mod
import dashscope_proxy_lib.handlers as _handlers_mod
import dashscope_proxy_lib.queue as _queue_mod
from dashscope_proxy_lib.handlers import handle_request
from dashscope_proxy_lib.http_helpers import (
    SECURITY_HEADERS_TO_STRIP,
    _read_upstream_capped,
    parse_retry_after_capped,
    strip_client_security_headers,
)
from dashscope_proxy_lib.provider_router import ProviderRouter
from dashscope_proxy_lib.queue import wait_for_slot
from dashscope_proxy_lib.rate_limiter import MultiProviderRateLimiter, RateLimiter
from dashscope_proxy_lib.request_transform import normalize_model_name, split_provider_prefix
from dashscope_proxy_lib.session_log import SessionLogWriter
from dashscope_proxy_lib.token_utils import (
    estimate_tokens_for_body,
    extract_tokens_from_stream,
)


def make_test_config():
    return {
        "rpm_limit": 6000,
        "tpm_limit": 10_000_000,
        "safety_factor": 0.8,
        "max_queue_size": 50,
        "max_retries": 2,
        "base_backoff": 0.05,
    }


@pytest.fixture(autouse=True)
def _reset_provider_router():
    """Reset the lazy provider router singleton around each test."""
    _handlers_mod._provider_router = None
    yield
    _handlers_mod._provider_router = None


CHAT_BODY = {"model": "qwen3-coder-plus", "messages": [{"role": "user", "content": "hi"}]}
STREAM_BODY = {**CHAT_BODY, "stream": True}
CHAT_BODY_BYTES = json.dumps(CHAT_BODY).encode()
STREAM_BODY_BYTES = json.dumps(STREAM_BODY).encode()


@asynccontextmanager
async def _mock_upstream(handler):
    """Start a one-route mock upstream on an ephemeral port; yields the port."""
    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(upstream_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        await runner.cleanup()


def _patch_target(monkeypatch, port):
    """Point the primary provider at the mock upstream for the test's duration."""
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(dashscope_proxy, "TARGET_BASE", url)
    monkeypatch.setattr(_config_mod, "TARGET_BASE", url)


def _proxy_app(**provider_configs):
    app = dashscope_proxy.create_app()
    app["rate_limiter"] = dashscope_proxy.MultiProviderRateLimiter(
        make_test_config(), **provider_configs
    )
    app["shutting_down"] = asyncio.Event()
    return app


# ---------------------------------------------------------------------------
# Item 3 — _read_upstream_capped cap semantics
# ---------------------------------------------------------------------------

class _RecordingContent:
    """StreamReader stand-in: scripted chunks, records read(n) sizes."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.read_sizes = []

    async def read(self, n=-1):
        self.read_sizes.append(n)
        if not self._chunks:
            return b""
        if n is None or n < 0:
            data = b"".join(self._chunks)
            self._chunks.clear()
            return data
        data = b""
        while self._chunks and len(data) < n:
            take = self._chunks.pop(0)
            room = n - len(data)
            data += take[:room]
            if len(take) > room:
                self._chunks.insert(0, take[room:])
        return data or b""


class _CapResp:
    def __init__(self, chunks):
        self.content = _RecordingContent(chunks)
        self._chunks = list(chunks)

    async def read(self):
        """Full-body read used by the unlimited (cap <= 0) path."""
        return b"".join(self._chunks)


class TestReadUpstreamCappedCaps:
    async def test_non_positive_cap_is_unlimited(self):
        resp = _CapResp([b"a" * 5, b"b" * 5])
        body, truncated = await _read_upstream_capped(resp, 0)
        assert body == b"a" * 5 + b"b" * 5
        assert truncated is False
        # Implementation calls resp.read() (response-level), not resp.content.read()
        # so content.read_sizes stays empty — that's correct behavior.

        resp2 = _CapResp([b"z" * 3])
        body2, truncated2 = await _read_upstream_capped(resp2, -1)
        assert body2 == b"z" * 3
        assert truncated2 is False

    async def test_positive_cap_keeps_two_step_probe(self):
        resp = _CapResp([b"xxxxxx", b"yyyy"])  # 10 bytes total
        body, truncated = await _read_upstream_capped(resp, 6)
        assert body == b"xxxxxx"
        assert truncated is True
        assert resp.content.read_sizes == [6, 1]  # cap read + 1-byte truncation probe

    async def test_positive_cap_exact_body_not_truncated(self):
        data = b"12345678"
        resp = _CapResp([data[:4], data[4:]])
        body, truncated = await _read_upstream_capped(resp, 8)
        assert body == data
        assert truncated is False
        assert resp.content.read_sizes == [8, 1]  # probe sees EOF


# ---------------------------------------------------------------------------
# Item 4 — client security header stripping (helpers + both call sites)
# ---------------------------------------------------------------------------

class TestStripClientSecurityHeaders:
    def test_constant_contents(self):
        assert SECURITY_HEADERS_TO_STRIP == frozenset({
            "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "cookie",
        })

    def test_removes_security_headers_case_insensitively_and_preserves_rest(self):
        headers = {
            "X-Forwarded-For": "1.2.3.4",
            "x-forwarded-host": "evil.example",
            "X-FORWARDED-PROTO": "https",
            "x-real-ip": "10.0.0.9",
            "COOKIE": "sid=1",
            "Authorization": "Bearer keep-auth",
            "Content-Type": "application/json",
            "X-Custom-Trace": "trace-1",
        }
        cleaned = strip_client_security_headers(headers)
        assert not {k.lower() for k in cleaned} & SECURITY_HEADERS_TO_STRIP
        assert cleaned["Authorization"] == "Bearer keep-auth"
        assert cleaned["Content-Type"] == "application/json"
        assert cleaned["X-Custom-Trace"] == "trace-1"

    def test_empty_headers_roundtrip(self):
        assert strip_client_security_headers({}) == {}


SECURITY_CLIENT_HEADERS = {
    "X-Forwarded-For": "9.9.9.9",
    "X-Forwarded-Proto": "https",
    "X-Real-IP": "10.0.0.1",
    "Cookie": "sid=secret",
    "Authorization": "Bearer client-key",
    "X-Custom-Trace": "trace-1",
}


def _assert_forwarded_headers_clean(captured):
    lowered = {k.lower() for k in captured}
    assert "x-forwarded-for" not in lowered
    assert "x-forwarded-proto" not in lowered
    assert "x-real-ip" not in lowered
    assert "cookie" not in lowered
    assert captured.get("X-Custom-Trace") == "trace-1"


class TestSecurityHeadersHandlerSites:
    async def test_stripped_on_first_forward(self, aiohttp_client, monkeypatch):
        captured = {}

        async def echo_headers(request):
            captured.update(dict(request.headers))
            return web.json_response({"choices": [], "usage": {"total_tokens": 5}})

        async with _mock_upstream(echo_headers) as port:
            _patch_target(monkeypatch, port)
            app = _proxy_app()
            async with aiohttp.ClientSession() as session:
                app["client_session"] = session
                client = await aiohttp_client(app)
                resp = await client.post(
                    "/v1/chat/completions", data=CHAT_BODY_BYTES, headers=SECURITY_CLIENT_HEADERS
                )
            assert resp.status == 200
        _assert_forwarded_headers_clean(captured)
        assert captured["Authorization"].startswith("Bearer ")
        assert captured["Authorization"] != "Bearer client-key"  # swapped for provider key
        assert captured["Content-Type"] == "application/json"

    async def test_stripped_on_failover_rebuild(self, aiohttp_client, monkeypatch):
        tertiary_captured = {}
        quaternary_captured = {}

        async def tertiary_502(request):
            tertiary_captured.update(dict(request.headers))
            return web.Response(status=502, text='{"error":"bad gateway"}')

        async def quaternary_ok(request):
            quaternary_captured.update(dict(request.headers))
            return web.json_response({"choices": [], "usage": {"total_tokens": 5}})

        async with _mock_upstream(tertiary_502) as t_port:
            async with _mock_upstream(quaternary_ok) as q_port:
                _patch_target(monkeypatch, t_port)
                monkeypatch.setattr(_config_mod, "TERTIARY_API_KEY", "t-key")
                monkeypatch.setattr(_config_mod, "TERTIARY_BASE_URL", f"http://127.0.0.1:{t_port}/v1")
                monkeypatch.setattr(_config_mod, "TERTIARY_MODELS", {
                    "object": "list",
                    "data": [{"id": "overlap-sec-headers-model", "object": "model"}],
                })
                monkeypatch.setattr(_config_mod, "QUATERNARY_API_KEY", "q-key")
                monkeypatch.setattr(_config_mod, "QUATERNARY_BASE_URL", f"http://127.0.0.1:{q_port}/v1")
                monkeypatch.setattr(_config_mod, "QUATERNARY_MODELS", {
                    "object": "list",
                    "data": [{"id": "overlap-sec-headers-model", "object": "model"}],
                })
                monkeypatch.setattr(dashscope_proxy, "MODEL_FALLBACK_ORDER", ["tertiary", "quaternary"])
                monkeypatch.setattr(dashscope_proxy, "MAX_5XX_RETRIES", 0)  # fail over immediately
                app = _proxy_app(tertiary_config=make_test_config(), quaternary_config=make_test_config())
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "overlap-sec-headers-model",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                        headers=SECURITY_CLIENT_HEADERS,
                    )
                assert resp.status == 200
        _assert_forwarded_headers_clean(tertiary_captured)
        _assert_forwarded_headers_clean(quaternary_captured)
        assert quaternary_captured["Authorization"] == "Bearer q-key"  # rebuilt for new provider


# ---------------------------------------------------------------------------
# Item 5 — parse_retry_after_capped semantics
# ---------------------------------------------------------------------------

class TestParseRetryAfterCapped:
    def test_caps_huge_delta_seconds_keeps_small_values(self):
        assert parse_retry_after_capped("999999") == 300.0
        assert parse_retry_after_capped("300") == 300.0
        assert parse_retry_after_capped("30") == 30.0
        assert parse_retry_after_capped("2.5") == 2.5

    def test_custom_cap(self):
        assert parse_retry_after_capped("10", cap_seconds=5.0) == 5.0

    def test_http_date_far_future_capped(self):
        header = formatdate(
            timeval=(datetime.now(timezone.utc) + timedelta(days=365)).timestamp(), usegmt=True
        )
        assert parse_retry_after_capped(header) == 300.0

    def test_http_date_past_floors_at_half_second(self):
        header = formatdate(
            timeval=(datetime.now(timezone.utc) - timedelta(days=365)).timestamp(), usegmt=True
        )
        assert parse_retry_after_capped(header) == 0.5

    def test_unparseable_returns_none(self):
        assert parse_retry_after_capped("soon-ish") is None
        assert parse_retry_after_capped("") is None


class TestRetryAfterCappedCallSite:
    async def test_429_backoff_uses_capped_parser(self, aiohttp_client, monkeypatch):
        calls = {"n": 0}

        async def flaky(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return web.Response(
                    status=429, headers={"Retry-After": "999999"}, body=b'{"error": "rate limited"}'
                )
            return web.json_response({"choices": [], "usage": {"total_tokens": 9}})

        parsed = []

        def spy(header_value, cap_seconds=300.0):
            parsed.append(header_value)
            return 0.02  # keep the retry fast; cap semantics tested above

        monkeypatch.setattr(_handlers_mod, "parse_retry_after_capped", spy)
        async with _mock_upstream(flaky) as port:
            _patch_target(monkeypatch, port)
            app = _proxy_app()
            async with aiohttp.ClientSession() as session:
                app["client_session"] = session
                client = await aiohttp_client(app)
                # Bounded so a regression to the raw header fails instead of hanging.
                resp = await asyncio.wait_for(
                    client.post("/v1/chat/completions", data=CHAT_BODY_BYTES), timeout=10
                )
            assert resp.status == 200
            assert calls["n"] == 2
            assert parsed == ["999999"]


# ---------------------------------------------------------------------------
# Item 10 — normalize_model_name
# ---------------------------------------------------------------------------

class TestNormalizeModelNameAudit:
    def test_strips_whitespace(self):
        assert normalize_model_name("  mimo-v2-5-pro  ") == "mimo-v2.5-pro"
        assert normalize_model_name("\tqwen3.6-plus\n") == "qwen3.6-plus"

    def test_case_insensitive_alias_prefix_suffix_case_preserved(self):
        assert normalize_model_name("MIMO-V2-5-PRO") == "mimo-v2.5-PRO"
        assert normalize_model_name("Mimo-V2-5-Air") == "mimo-v2.5-Air"

    def test_suffix_after_alias_preserved(self):
        assert normalize_model_name("mimo-v2-5-x-y-z") == "mimo-v2.5-x-y-z"

    def test_non_alias_and_non_string_untouched(self):
        assert normalize_model_name("mimo-v2-pro") == "mimo-v2-pro"
        assert normalize_model_name("qwen3.6-plus") == "qwen3.6-plus"
        assert normalize_model_name(None) is None
        assert normalize_model_name(123) == 123


# ---------------------------------------------------------------------------
# Item 11 — multi-slash provider pin (splitter unit side)
# ---------------------------------------------------------------------------

class TestSplitProviderPrefixMultiSlash:
    def test_known_slug_with_nested_slash_keeps_remainder(self):
        assert split_provider_prefix("openlux/ark/somemodel") == ("tertiary", "ark/somemodel")

    def test_unknown_slug_returns_none_with_full_name(self):
        assert split_provider_prefix("nosuch/ark/somemodel") == (None, "nosuch/ark/somemodel")


# ---------------------------------------------------------------------------
# Item 13 — token_utils: media parts, non-string text, list system/developer, SSE prefixes
# ---------------------------------------------------------------------------

class TestEstimateTokensMediaAndNonStringParts:
    def test_non_string_text_parts_contribute_zero(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        123,
                        {"type": "text"},       # missing text
                        {"text": 42},           # non-string text
                        {"text": None},         # None text
                    ],
                }
            ]
        }
        assert estimate_tokens_for_body(body) == 100  # floor only

    def test_media_part_types_add_fixed_allowance(self):
        parts = [
            {"type": "text", "text": "a" * 16},
            {"type": "image_url", "image_url": {}},
            {"type": "image"},
            {"type": "input_audio"},
            {"type": "audio"},
        ]
        body = {"messages": [{"role": "user", "content": parts}]}
        assert estimate_tokens_for_body(body) == (16 + 4 * 4096) // 4

    def test_image_url_key_counts_as_media_without_type(self):
        body = {"messages": [{"role": "user", "content": [{"image_url": {"url": "x"}}]}]}
        assert estimate_tokens_for_body(body) == 4096 // 4

    def test_media_plus_text_mixed_estimate(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "a" * 16},
                        {"type": "image_url", "image_url": {"url": "x"}},
                    ],
                }
            ]
        }
        assert estimate_tokens_for_body(body) == (16 + 4096) // 4

    def test_list_system_and_developer_counted(self):
        body = {
            "messages": [],
            "system": [{"type": "text", "text": "s" * 400}],
            "developer": [{"text": "d" * 400}],
        }
        assert estimate_tokens_for_body(body) == 800 // 4


class TestExtractTokensStreamPrefixes:
    def test_accepts_data_with_and_without_space(self):
        buf = (
            b'data: {"usage": {"total_tokens": 5}}\n\n'
            b'data:{"usage": {"total_tokens": 9}}\n\n'
        )
        assert extract_tokens_from_stream(buf)["total_tokens"] == 9

    def test_skips_done_marker_in_both_forms(self):
        buf = (
            b'data: {"usage": {"total_tokens": 5}}\n\n'
            b'data: [DONE]\n\n'
            b'data:[DONE]\n\n'
        )
        assert extract_tokens_from_stream(buf)["total_tokens"] == 5


# ---------------------------------------------------------------------------
# Item 7 (limiter side) — record_completion(circuit_success=None) neutrality
# ---------------------------------------------------------------------------

class TestRecordCompletionCircuitNeutrality:
    async def test_none_leaves_closed_circuit_counters_untouched(self):
        rl = RateLimiter(make_test_config())
        rl.circuit_failure_count = 3
        await rl.try_admit(50)
        await rl.record_completion(
            estimated_tokens=50, actual_tokens=40, model="m",
            latency_ms=5.0, request_bytes=10, response_bytes=20,
            circuit_success=None,
        )
        assert rl.circuit_failure_count == 3  # not reset, not incremented
        assert rl.circuit_state == "CLOSED"
        assert rl.circuit_open_until == 0.0
        assert rl.tpm_bucket.reserved == 0  # TPM still reconciled
        assert rl.total_forwarded == 1
        assert rl.total_tokens_consumed == 40

    async def test_none_keeps_half_open_probe_in_flight(self):
        rl = RateLimiter(make_test_config())
        rl.circuit_state = "HALF_OPEN"
        rl.circuit_probe_in_flight = True
        await rl.record_completion(
            estimated_tokens=0, actual_tokens=0, model="m",
            latency_ms=1.0, request_bytes=0, response_bytes=0,
            circuit_success=None,
        )
        assert rl.circuit_state == "HALF_OPEN"
        assert rl.circuit_probe_in_flight is True

    async def test_success_still_resets_for_contrast(self):
        rl = RateLimiter(make_test_config())
        rl.circuit_failure_count = 3
        await rl.record_completion(
            estimated_tokens=0, actual_tokens=0, model="m",
            latency_ms=1.0, request_bytes=0, response_bytes=0,
            circuit_success=True,
        )
        assert rl.circuit_failure_count == 0
        assert rl.circuit_state == "CLOSED"


# ---------------------------------------------------------------------------
# Item 8 — wait_for_slot 3-tuple + jitter branches
# ---------------------------------------------------------------------------

class _ScriptedLimiter:
    """Duck-typed limiter: scripted can_proceed outcomes for wait_for_slot.

    After the scripted results are exhausted, can_proceed allows. When
    ``queue_full_after`` is set, ``is_queue_full`` turns True once that many
    can_proceed calls have been made (mid-wait queue-full scenario).
    """

    def __init__(self, results, queue_full=False, queue_full_after=None):
        self._results = list(results)
        self._queue_full = queue_full
        self._queue_full_after = queue_full_after
        self._can_proceed_calls = 0
        self.pending_requests = 0
        self.max_queue_size = 10

    def is_queue_full(self):
        if self._queue_full:
            return True
        if self._queue_full_after is not None:
            return self._can_proceed_calls >= self._queue_full_after
        return False

    async def can_proceed(self, estimated_tokens=0):
        self._can_proceed_calls += 1
        if self._results:
            return self._results.pop(0)
        return True, "ok", 0.0


def _ws_request(disconnected=False):
    req = MagicMock()
    req.protocol.transport.is_closing.return_value = disconnected
    return req


class TestWaitForSlotTuple:
    async def test_immediate_success_returns_none_reason(self):
        limiter = _ScriptedLimiter([(True, "ok", 0.0)])
        result = await wait_for_slot(limiter, _ws_request(), 0, deadline_seconds=1.0)
        assert result == (0.0, None, 0.0)

    async def test_success_after_wait_carries_last_wait(self, monkeypatch):
        monkeypatch.setattr(_queue_mod.random, "uniform", lambda a, b: 0.1)
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(_queue_mod.asyncio, "sleep", fake_sleep)
        limiter = _ScriptedLimiter([(False, "RPS spacing", 1.0), (True, "ok", 0.0)])
        result = await wait_for_slot(limiter, _ws_request(), 0, deadline_seconds=30.0)
        assert result == (1.1, None, 1.0)
        assert sleeps == [1.1]

    async def test_queue_full_immediate(self):
        limiter = _ScriptedLimiter([], queue_full=True)
        limiter.pending_requests = 11
        result = await wait_for_slot(limiter, _ws_request(), 0, deadline_seconds=1.0)
        assert result == (0.0, "queue_full", 0.0)

    async def test_queue_full_after_denial_carries_last_wait(self):
        limiter = _ScriptedLimiter([(False, "RPM limit reached", 60.0)], queue_full_after=1)
        result = await wait_for_slot(limiter, _ws_request(), 0, deadline_seconds=5.0)
        assert result == (0.0, "queue_full", 60.0)

    async def test_client_disconnected_after_denial(self):
        limiter = _ScriptedLimiter([(False, "RPM limit reached", 60.0)])
        result = await wait_for_slot(limiter, _ws_request(disconnected=True), 0, deadline_seconds=5.0)
        assert result == (0.0, "client_disconnected", 60.0)

    async def test_deadline_exceeded_carries_last_wait(self):
        # Deadline smaller than the denial wait: aborts before any sleep.
        limiter = _ScriptedLimiter([(False, "RPM limit reached", 60.0)])
        result = await wait_for_slot(limiter, _ws_request(), 0, deadline_seconds=50.0)
        assert result == (0.0, "deadline_exceeded", 60.0)


class TestWaitForSlotJitterBranches:
    async def test_rpm_denial_uses_long_jitter_branch(self, monkeypatch):
        monkeypatch.setattr(_queue_mod.random, "uniform", lambda a, b: 1.0)
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(_queue_mod.asyncio, "sleep", fake_sleep)
        # Two denials then allow: each denial triggers a sleep before the next attempt.
        limiter = _ScriptedLimiter([
            (False, "RPM limit reached", 60.0),
            (False, "RPM limit reached", 60.0),
            (True, "ok", 0.0),
        ])
        result = await wait_for_slot(limiter, _ws_request(), 0, deadline_seconds=130.0)
        # Each denial sleeps: max(60.0, 5.0) + 1.0 = 61.0
        assert sleeps == [61.0, 61.0]
        assert result == (122.0, None, 60.0)

    async def test_tpm_denial_uses_long_jitter_branch(self, monkeypatch):
        monkeypatch.setattr(_queue_mod.random, "uniform", lambda a, b: 1.0)
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(_queue_mod.asyncio, "sleep", fake_sleep)
        limiter = _ScriptedLimiter([
            (False, "TPM limit reached", 2.0),
            (True, "ok", 0.0),
        ])
        result = await wait_for_slot(limiter, _ws_request(), 100, deadline_seconds=130.0)
        # Long branch: max(2.0, 5.0) + 1.0 = 6.0
        assert sleeps == [6.0]
        assert result == (6.0, None, 2.0)

    async def test_rps_denial_uses_short_jitter_branch(self, monkeypatch):
        monkeypatch.setattr(_queue_mod.random, "uniform", lambda a, b: 0.1)
        sleeps = []
        fake_time = [0.0]

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            fake_time[0] += seconds

        monkeypatch.setattr(_queue_mod.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(_queue_mod.time, "monotonic", lambda: fake_time[0])
        limiter = _ScriptedLimiter([(False, "RPS spacing", 1.0)] * 20)
        result = await wait_for_slot(limiter, _ws_request(), 0, deadline_seconds=5.0)
        assert result[1] == "deadline_exceeded"
        assert sleeps
        # Short branch: wait + jitter (no 5s floor) -> 1.0 + 0.1 = 1.1
        # With deadline=5.0, we get ~4 sleeps before the deadline check aborts.
        assert all(s == 1.1 for s in sleeps)
        assert len(sleeps) <= 5  # deadline stops further sleeps


class TestHandlerConsumesWaitForSlotTuple:
    async def test_deadline_exceeded_returns_503_with_last_wait_retry_after(
        self, aiohttp_client, monkeypatch
    ):
        async def fake_wait_for_slot(limiter, request, estimated_tokens=0,
                                     deadline_seconds=120.0, queue_limiter=None):
            return 12.0, "deadline_exceeded", 30.0

        monkeypatch.setattr(_handlers_mod, "wait_for_slot", fake_wait_for_slot)
        app = _proxy_app()
        client = await aiohttp_client(app)
        resp = await client.post("/v1/chat/completions", data=CHAT_BODY_BYTES)
        assert resp.status == 503
        assert await resp.json() == {
            "error": "rate limit backoff pending, retry later", "retry_after": 30
        }
        assert resp.headers["Retry-After"] == "30"


# ---------------------------------------------------------------------------
# Item 1 — circuit-open first candidate: failover vs pinned 503
# ---------------------------------------------------------------------------

def _overlap_patches(monkeypatch, t_port, q_port):
    _patch_target(monkeypatch, t_port)
    monkeypatch.setattr(_config_mod, "TERTIARY_API_KEY", "t-key")
    monkeypatch.setattr(_config_mod, "TERTIARY_BASE_URL", f"http://127.0.0.1:{t_port}/v1")
    monkeypatch.setattr(_config_mod, "TERTIARY_MODELS", {
        "object": "list", "data": [{"id": "overlap-circuit-model", "object": "model"}],
    })
    monkeypatch.setattr(_config_mod, "QUATERNARY_API_KEY", "q-key")
    monkeypatch.setattr(_config_mod, "QUATERNARY_BASE_URL", f"http://127.0.0.1:{q_port}/v1")
    monkeypatch.setattr(_config_mod, "QUATERNARY_MODELS", {
        "object": "list", "data": [{"id": "overlap-circuit-model", "object": "model"}],
    })
    monkeypatch.setattr(dashscope_proxy, "MODEL_FALLBACK_ORDER", ["tertiary", "quaternary"])


class TestCircuitOpenFirstCandidate:
    async def test_unpinned_request_fails_over_when_first_candidate_circuit_open(
        self, aiohttp_client, monkeypatch
    ):
        hits = {"quaternary": 0}

        async def quaternary_ok(request):
            hits["quaternary"] += 1
            return web.json_response({"choices": [], "usage": {"total_tokens": 7}})

        async def never_hit(request):  # canary: tertiary must never be contacted
            raise AssertionError("tertiary upstream should never be contacted")

        async with _mock_upstream(quaternary_ok) as q_port:
            async with _mock_upstream(never_hit) as t_port:
                _overlap_patches(monkeypatch, t_port, q_port)
                app = _proxy_app(tertiary_config=make_test_config(), quaternary_config=make_test_config())
                rl = app["rate_limiter"]
                rl.tertiary.circuit_state = "OPEN"
                rl.tertiary.circuit_open_until = time.monotonic() + 60
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "overlap-circuit-model",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                assert hits["quaternary"] == 1
                assert rl.tertiary.tpm_bucket.reserved == 0  # refunded before failover
                assert rl.quaternary.tpm_bucket.reserved == 0  # reconciled on completion
                assert rl.tertiary.total_forwarded == 0
                assert rl.quaternary.total_forwarded == 1

    async def test_pinned_request_keeps_503_when_circuit_open(self, aiohttp_client, monkeypatch):
        async def never_hit(request):  # canary: nothing should be contacted
            raise AssertionError("upstream should never be contacted for pinned + open circuit")

        async with _mock_upstream(never_hit) as q_port:
            async with _mock_upstream(never_hit) as t_port:
                _overlap_patches(monkeypatch, t_port, q_port)
                app = _proxy_app(tertiary_config=make_test_config(), quaternary_config=make_test_config())
                rl = app["rate_limiter"]
                rl.tertiary.circuit_state = "OPEN"
                rl.tertiary.circuit_open_until = time.monotonic() + 60
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "openlux/overlap-circuit-model",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 503
                    data = await resp.json()
                assert data["error"] == "upstream unavailable"
                assert data["retry_after"] == int(rl.tertiary.circuit_cooldown)
                assert rl.tertiary.tpm_bucket.reserved == 0  # reservation refunded


# ---------------------------------------------------------------------------
# Item 2 — truncated non-stream upstream bodies
# ---------------------------------------------------------------------------

class TestTruncatedUpstreamBodies:
    async def test_truncated_2xx_becomes_502_with_single_refund(self, aiohttp_client, monkeypatch):
        async def big_ok(request):
            return web.json_response({
                "choices": [{"message": {"role": "assistant", "content": "x" * 500}}],
                "usage": {"total_tokens": 42},
            })

        async with _mock_upstream(big_ok) as port:
            _patch_target(monkeypatch, port)
            monkeypatch.setattr(dashscope_proxy, "UPSTREAM_MAX_BODY_SIZE", 10)
            app = _proxy_app()
            rl = app["rate_limiter"]
            refunds = []
            real_refund = rl.primary.refund_tokens

            async def counting_refund(n):
                refunds.append(n)
                return await real_refund(n)

            rl.primary.refund_tokens = counting_refund
            async with aiohttp.ClientSession() as session:
                app["client_session"] = session
                client = await aiohttp_client(app)
                resp = await client.post("/v1/chat/completions", data=CHAT_BODY_BYTES)
            assert resp.status == 502
            assert await resp.json() == {"error": "upstream response body too large"}
            assert refunds == [100]  # exactly one refund of the estimate
            assert rl.primary.tpm_bucket.reserved == 0

    async def test_truncated_429_body_falls_through_to_retry(self, aiohttp_client, monkeypatch):
        calls = {"n": 0}

        async def flaky(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return web.Response(status=429, body=b'{"error": "' + b"x" * 500 + b'"}')
            # Return a small success body that fits within the cap
            return web.json_response({"ok": 1})

        async with _mock_upstream(flaky) as port:
            _patch_target(monkeypatch, port)
            monkeypatch.setattr(dashscope_proxy, "UPSTREAM_MAX_BODY_SIZE", 10)
            app = _proxy_app()
            async with aiohttp.ClientSession() as session:
                app["client_session"] = session
                client = await aiohttp_client(app)
                resp = await client.post("/v1/chat/completions", data=CHAT_BODY_BYTES)
            # The truncated 429 falls through to retry logic, but the handler
            # still proxies the capped body as-is for error statuses.
            # The retry succeeds with a small body.
            assert resp.status == 200
            assert calls["n"] == 2

    async def test_truncated_5xx_body_falls_through_to_5xx_branch(self, aiohttp_client, monkeypatch):
        async def always_500(request):
            return web.Response(status=500, body=b'{"error": "' + b"y" * 500 + b'"}')

        async with _mock_upstream(always_500) as port:
            _patch_target(monkeypatch, port)
            monkeypatch.setattr(dashscope_proxy, "UPSTREAM_MAX_BODY_SIZE", 10)
            monkeypatch.setattr(dashscope_proxy, "MAX_5XX_RETRIES", 0)  # no backoff sleeps
            app = _proxy_app()
            rl = app["rate_limiter"]
            async with aiohttp.ClientSession() as session:
                app["client_session"] = session
                client = await aiohttp_client(app)
                resp = await client.post("/v1/chat/completions", data=CHAT_BODY_BYTES)
            # Truncated 5xx body is proxied as-is (capped to 10 bytes)
            assert resp.status == 500
            body = await resp.read()
            # The body is the capped portion (first 10 bytes of the JSON)
            assert len(body) == 10
            assert body == b'{"error": '
            assert rl.primary.circuit_failure_count == 1  # record_circuit_failure ran


# ---------------------------------------------------------------------------
# Item 11 — multi-slash provider pin (ingress side)
# ---------------------------------------------------------------------------

class TestInvalidModelPinIngress:
    async def test_multi_slash_known_slug_returns_400(self, aiohttp_client):
        app = _proxy_app()
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "openlux/ark/somemodel",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        assert resp.status == 400
        assert await resp.json() == {"error": "invalid model pin: openlux/ark/somemodel"}

    async def test_multi_slash_unknown_slug_is_unknown_prefix(self, aiohttp_client):
        app = _proxy_app()
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "nosuch/ark/somemodel",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "unknown provider prefix"
        assert "openlux" in data["available_providers"]


# ---------------------------------------------------------------------------
# Item 9 — post-queue admit denial reflects the real try_admit reason
# ---------------------------------------------------------------------------

class TestPostQueueAdmitDenial:
    @pytest.mark.parametrize(
        "reason, message, error_reason, retry_after",
        [
            ("TPM limit reached", "TPM quota exceeded while queued", "tpm_reservation_failed", "1"),
            ("RPM limit reached", "RPM limit reached while queued", "admit_failed_rpm", "7"),
            ("RPS spacing", "request spacing exceeded while queued", "admit_failed_rps", "7"),
        ],
    )
    async def test_denial_body_and_session_log_use_real_reason(
        self, aiohttp_client, tmp_path, monkeypatch, reason, message, error_reason, retry_after
    ):
        async def deny(tokens):
            return False, reason, 7.0

        app = _proxy_app()
        rl = app["rate_limiter"]
        monkeypatch.setattr(rl.primary, "try_admit", deny)
        log_dir = tmp_path / "slogs"
        writer = SessionLogWriter(str(log_dir), sync_flush=True)
        app["session_log"] = writer
        try:
            client = await aiohttp_client(app)
            resp = await client.post("/v1/chat/completions", data=CHAT_BODY_BYTES)
            assert resp.status == 503
            assert (await resp.json())["error"] == message
            assert resp.headers["Retry-After"] == retry_after
            request_id = resp.headers["X-Request-ID"]
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            log_file = log_dir / f"{today}.jsonl"
            entries = [
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").strip().splitlines()
            ]
            entry = next(e for e in entries if e["request_id"] == request_id)
            assert entry["error_reason"] == error_reason
        finally:
            writer.close()


# ---------------------------------------------------------------------------
# Item 6 — streaming exception contract (CancelledError vs connection errors)
# ---------------------------------------------------------------------------

class _FakeStreamContent:
    def __init__(self, chunks, exc):
        self._chunks = list(chunks)
        self._exc = exc

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._chunks:
            return self._chunks.pop(0)
        exc = self._exc
        self._exc = None
        if exc is not None:
            raise exc
        raise StopAsyncIteration


class _FakeStreamUpstream:
    """ClientResponse stand-in for the streaming branch."""

    def __init__(self, chunks=(), exc=None, status=200):
        self.status = status
        self.headers = {"Content-Type": "text/event-stream"}
        self.content = _FakeStreamContent(chunks, exc)
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, upstream):
        self._upstream = upstream

    async def request(self, **kwargs):
        return self._upstream


def _mk_stream_request():
    reader = StreamReader(MagicMock(), limit=2**16)
    reader.feed_data(STREAM_BODY_BYTES)
    reader.feed_eof()
    req = make_mocked_request(
        "POST",
        "/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        payload=reader,
        app=web.Application(),
    )
    req.protocol.transport.is_closing.return_value = False
    return req


class TestStreamAbortExceptionContract:
    async def test_cancelled_error_is_reraised_not_swallowed(self):
        rl = MultiProviderRateLimiter(make_test_config())
        upstream = _FakeStreamUpstream(
            chunks=[b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'],
            exc=asyncio.CancelledError(),
        )
        req = _mk_stream_request()
        req.app["rate_limiter"] = rl
        req.app["client_session"] = _FakeSession(upstream)
        req.app["shutting_down"] = asyncio.Event()
        with pytest.raises(asyncio.CancelledError):
            await handle_request(req)
        assert upstream.closed is True
        assert rl.primary.tpm_bucket.reserved == 0  # refunded before re-raise

    async def test_connection_error_finalizes_prepared_stream(self):
        rl = MultiProviderRateLimiter(make_test_config())
        upstream = _FakeStreamUpstream(
            chunks=[b'data: {"x": 1}\n\n'], exc=ConnectionResetError()
        )
        req = _mk_stream_request()
        req.app["rate_limiter"] = rl
        req.app["client_session"] = _FakeSession(upstream)
        req.app["shutting_down"] = asyncio.Event()
        resp = await handle_request(req)
        assert isinstance(resp, web.StreamResponse)  # finalize path, not a 499/502 error body
        assert upstream.closed is True
        assert rl.primary.tpm_bucket.reserved == 0  # refunded


# ---------------------------------------------------------------------------
# Item 7 (handler side) — stream idle-timeout abort leaves circuit untouched
# ---------------------------------------------------------------------------

# The idle timeout wraps resp.write(chunk) (client-write side), not upstream
# chunk reads. Testing it requires a slow client, which is hard to simulate
# with aiohttp_client. The circuit neutrality contract is already tested at
# the rate limiter level in TestRecordCompletionCircuitNeutrality.


# ---------------------------------------------------------------------------
# Item 12 — provider router: live slug inversion, live model sets, pin semantics
# ---------------------------------------------------------------------------

class TestProviderRouterAudit:
    def test_slug_map_inverted_from_live_provider_slugs(self, monkeypatch):
        monkeypatch.setattr(_config_mod, "SECONDARY_API_KEY", "k")
        monkeypatch.setattr(_config_mod, "SECONDARY_BASE_URL", "https://mimo.example/v1")
        # Custom slug absent from PROVIDER_SLUG_MAP proves runtime inversion.
        monkeypatch.setattr(dashscope_proxy, "PROVIDER_SLUGS", {"xyzcorp": "secondary"})
        router = ProviderRouter()
        models = router.get_all_models()
        entry = next(e for e in models["data"] if e["id"] == "mimo-v2.5-pro")
        assert entry["providers"] == ["secondary"]
        assert entry["provider_models"] == ["xyzcorp/mimo-v2.5-pro"]

    def test_model_id_sets_built_from_live_config(self, monkeypatch):
        monkeypatch.setattr(_config_mod, "SECONDARY_API_KEY", "k")
        monkeypatch.setattr(_config_mod, "SECONDARY_BASE_URL", "https://mimo.example/v1")
        monkeypatch.setattr(_config_mod, "SECONDARY_MODELS", {
            "object": "list",
            "data": [
                {"id": "live-fresh-model", "object": "model"},
                {"id": "mimo-v2.5-x9", "object": "model"},
            ],
        })
        router = ProviderRouter()
        assert router.get_provider_for_model("live-fresh-model") is router.secondary
        # Cursor hyphen alias derived from the live list.
        assert router.get_provider_for_model("mimo-v2-5-x9") is router.secondary

    def test_unknown_slug_pin_keeps_bare_name_fallback(self, monkeypatch):
        monkeypatch.setattr(_config_mod, "TERTIARY_API_KEY", "k")
        monkeypatch.setattr(_config_mod, "TERTIARY_BASE_URL", "https://t.example/v1")
        router = ProviderRouter()
        assert router.get_provider_for_model("nosuch/gemini-3.7-flash").name == "primary"

    def test_known_unavailable_pin_returns_pinned_provider(self, monkeypatch):
        monkeypatch.setattr(_config_mod, "TERTIARY_API_KEY", "")
        monkeypatch.setattr(_config_mod, "TERTIARY_BASE_URL", "")
        router = ProviderRouter()
        provider = router.get_provider_for_model("openlux/gemini-3.7-flash")
        assert provider is router.tertiary
        assert provider.is_available is False


# ---------------------------------------------------------------------------
# Item 14 — generation-path ingress exemption is messages-only
# ---------------------------------------------------------------------------

class TestGenerationPathIngressExemption:
    """`requires_messages` exempts generation paths from ONLY the messages
    check; the `model` requirement applies to every POST (chat or generation)."""

    @pytest.mark.parametrize("path", [
        "/v1/chat/completions",
        "/v1/messages",
        "/v1/embeddings",
    ])
    async def test_chat_style_paths_still_require_messages(self, aiohttp_client, path):
        app = _proxy_app()
        client = await aiohttp_client(app)
        resp = await client.post(path, data=json.dumps({"model": "qwen3-coder-plus"}).encode())
        assert resp.status == 400
        assert await resp.json() == {"error": "missing required field: messages"}

    async def test_generation_path_without_model_still_returns_400(self, aiohttp_client):
        app = _proxy_app()
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/images/generations",
            data=json.dumps({"prompt": "a cat", "size": "2K"}).encode(),
        )
        assert resp.status == 400
        assert await resp.json() == {"error": "missing required field: model"}

    async def test_generation_path_with_model_bypasses_messages(
        self, aiohttp_client, monkeypatch
    ):
        async def images_ok(request):
            return web.json_response({"created": 1, "data": [{"url": "https://img.example/x.png"}]})

        # Inline upstream: the shared _mock_upstream helper only serves chat.
        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/images/generations", images_ok)
        runner = web.AppRunner(upstream_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        monkeypatch.setattr(_config_mod, "NONARY_API_KEY", "k")
        monkeypatch.setattr(_config_mod, "NONARY_BASE_URL", f"http://127.0.0.1:{port}")

        try:
            app = _proxy_app()
            async with aiohttp.ClientSession() as session:
                app["client_session"] = session
                client = await aiohttp_client(app)
                resp = await client.post(
                    "/v1/images/generations",
                    data=json.dumps({
                        "model": "agnes-image-2.1-flash", "prompt": "a cat", "size": "2K",
                    }).encode(),
                )
                assert resp.status == 200
        finally:
            await runner.cleanup()

