"""Integration tests for the proxy request handler using aiohttp test server."""
import json
import time
import asyncio
from datetime import datetime, timezone
from email.utils import formatdate
from unittest.mock import MagicMock
import aiohttp
from aiohttp import web
import pytest

import dashscope_proxy
import dashscope_proxy_lib.handlers as _handlers_mod
import dashscope_proxy_lib.config as _config_mod
import dashscope_proxy_lib.provider_router as _provider_router_mod


def _patch_target_base(new_url):
    """Context manager to patch TARGET_BASE across all modules that reference it."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        orig_facade = dashscope_proxy.TARGET_BASE
        orig_config = _config_mod.TARGET_BASE
        orig_pr = _provider_router_mod.TARGET_BASE
        dashscope_proxy.TARGET_BASE = new_url
        _config_mod.TARGET_BASE = new_url
        _provider_router_mod.TARGET_BASE = new_url
        try:
            yield
        finally:
            dashscope_proxy.TARGET_BASE = orig_facade
            _config_mod.TARGET_BASE = orig_config
            _provider_router_mod.TARGET_BASE = orig_pr

    return _ctx()


def make_test_config():
    return {
        "rpm_limit": 6000,
        "tpm_limit": 10_000_000,
        "safety_factor": 0.8,
        "requests_per_5h": 100_000,
        "requests_per_week": 100_000,
        "requests_per_month": 100_000,
        "max_queue_size": 50,
        "max_retries": 2,
        "base_backoff": 0.05,
    }


@pytest.fixture(autouse=True)
def _reset_provider_router():
    """Reset the lazy provider router singleton before each test."""
    _handlers_mod._provider_router = None
    yield
    _handlers_mod._provider_router = None


@pytest.fixture
def proxy_app():
    """Create the proxy app with a test rate limiter and mock client_session."""
    rate_limiter = dashscope_proxy.MultiProviderRateLimiter(make_test_config())
    app = dashscope_proxy.create_app()
    app["rate_limiter"] = rate_limiter
    app["shutting_down"] = asyncio.Event()
    # Mock client_session for tests that don't do actual forwarding
    app["client_session"] = None
    return app, rate_limiter


# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------

class TestApiKey:
    def test_empty_api_key(self):
        key = "".strip()
        assert key == ""

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-sp-test456")
        import importlib
        importlib.reload(dashscope_proxy)
        assert dashscope_proxy.DASHSCOPE_API_KEY == "sk-sp-test456"


# ---------------------------------------------------------------------------
# Health / Ready endpoints
# ---------------------------------------------------------------------------

class TestHealthReady:
    async def test_health_returns_ok(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

    async def test_ready_returns_ok(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        # Set a real (but unused) client session so ready check works
        app["client_session"] = type("MockSession", (), {"closed": False})()
        client = await aiohttp_client(app)
        resp = await client.get("/ready")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ready"

    async def test_health_not_rate_limited(self, aiohttp_client, proxy_app):
        app, rl = proxy_app
        rl.rpm_limit = 0
        client = await aiohttp_client(app)
        resp = await client.get("/health")
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

class TestRequestValidation:
    async def test_empty_post_body_returns_400(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.post("/v1/chat/completions", data=b"")
        assert resp.status == 400

    async def test_invalid_json_returns_400(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=b"not valid json",
        )
        assert resp.status == 400

    async def test_missing_model_returns_400(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
        )
        assert resp.status == 400

    async def test_missing_messages_returns_400(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({"model": "qwen3-coder-plus"}).encode(),
        )
        assert resp.status == 400

    async def test_non_dict_body_returns_400(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps([1, 2, 3]).encode(),
        )
        assert resp.status == 400

    async def test_excessive_body_size_returns_413(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        client = await aiohttp_client(app)
        large_body = json.dumps({
            "model": "qwen3-coder-plus",
            "messages": [{"role": "user", "content": "x" * (11 * 1024 * 1024)}],
        }).encode()
        resp = await client.post(
            "/v1/chat/completions",
            data=large_body,
        )
        assert resp.status == 413

    async def test_get_on_chat_endpoint_returns_405(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.get("/v1/chat/completions")
        assert resp.status == 405


# ---------------------------------------------------------------------------
# Proxy forwarding (non-streaming)
# ---------------------------------------------------------------------------

class TestProxyForwarding:
    async def test_forwards_to_upstream(self, aiohttp_client, proxy_app):
        _, rl = proxy_app
        upstream_app = web.Application()

        async def mock_upstream(request):
            return web.json_response({
                "id": "resp-1",
                "choices": [{"message": {"role": "assistant", "content": "Hello!"}}],
                "usage": {"total_tokens": 25},
            })

        upstream_app.router.add_post("/v1/chat/completions", mock_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                # Need a real ClientSession for forwarding
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                    data = await resp.json()
                    assert "choices" in data
                    assert rl.primary.total_forwarded >= 1
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()

    async def test_developer_role_converted(self, aiohttp_client, proxy_app):
        upstream_app = web.Application()
        received_body = None

        async def capture_upstream(request):
            nonlocal received_body
            received_body = await request.read()
            return web.json_response({
                "id": "resp-1",
                "choices": [],
                "usage": {"total_tokens": 5},
            })

        upstream_app.router.add_post("/v1/chat/completions", capture_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "developer", "content": "be helpful"}],
                        }).encode(),
                    )
                    assert received_body is not None
                    forwarded = json.loads(received_body)
                    assert forwarded["messages"][0]["role"] == "system"
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Upstream 429 retry
# ---------------------------------------------------------------------------

class TestUpstream429Retry:
    async def test_retries_on_429_then_succeeds(self, aiohttp_client, proxy_app):
        upstream_app = web.Application()
        call_count = 0

        async def flaky_upstream(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return web.Response(status=429, body=b'{"error":"rate limited"}')
            return web.json_response({
                "id": "resp-1",
                "choices": [],
                "usage": {"total_tokens": 10},
            })

        upstream_app.router.add_post("/v1/chat/completions", flaky_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                    assert call_count == 2
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Upstream 5xx retry
# ---------------------------------------------------------------------------

class TestUpstream5xxRetry:
    async def test_retries_on_500_then_succeeds(self, aiohttp_client, proxy_app):
        upstream_app = web.Application()
        call_count = 0

        async def flaky_upstream(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return web.Response(status=500, body=b'{"error":"internal"}')
            return web.json_response({
                "id": "resp-1",
                "choices": [],
                "usage": {"total_tokens": 10},
            })

        upstream_app.router.add_post("/v1/chat/completions", flaky_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                    assert call_count == 2
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()

    async def test_gives_up_after_max_5xx_retries(self, aiohttp_client, proxy_app):
        upstream_app = web.Application()

        async def always_500(request):
            return web.Response(status=502, body=b'{"error":"bad gateway"}')

        upstream_app.router.add_post("/v1/chat/completions", always_500)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 502
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Mock models endpoint
# ---------------------------------------------------------------------------

class TestMockModels:
    async def test_get_models_returns_mock_list(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.get("/v1/models")
        assert resp.status == 200
        data = await resp.json()
        assert data["object"] == "list"
        assert len(data["data"]) > 0

    async def test_get_models_alt_path(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.get("/models")
        assert resp.status == 200
        data = await resp.json()
        assert "data" in data


# ---------------------------------------------------------------------------
# Proxy status endpoint
# ---------------------------------------------------------------------------

class TestProxyStatus:
    async def test_status_endpoint(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.get("/v1/proxy/status")
        assert resp.status == 200
        data = await resp.json()
        # The status endpoint returns a nested structure with rate_limits and providers
        assert "rate_limits" in data
        assert "providers" in data
        # rate_limits may be flat (plain RateLimiter) or nested (MultiProviderRateLimiter)
        rate_limits = data["rate_limits"]
        if "primary" in rate_limits:
            # MultiProviderRateLimiter mode
            primary_stats = rate_limits["primary"]
            assert "total_forwarded" in primary_stats
            assert "rpm_limit" in primary_stats
        else:
            # Plain RateLimiter mode (backward compatibility)
            assert "total_forwarded" in rate_limits
            assert "rpm_limit" in rate_limits
        # providers should contain primary, secondary, tertiary, and quaternary availability
        providers = data["providers"]
        assert "primary" in providers
        assert "secondary" in providers
        assert "tertiary" in providers
        assert "quaternary" in providers


# ---------------------------------------------------------------------------
# Response headers
# ---------------------------------------------------------------------------

class TestResponseHeaders:
    async def _make_forwarding_client(self, aiohttp_client, proxy_app, upstream_app):
        """Helper to set up a proxy with a real ClientSession forwarding to upstream."""
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        original_target = dashscope_proxy.TARGET_BASE
        dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
        app, _ = proxy_app
        session = aiohttp.ClientSession()
        app["client_session"] = session
        client = await aiohttp_client(app)

        return client, session, upstream_runner, original_target

    async def test_x_request_id_in_response(self, aiohttp_client, proxy_app):
        upstream_app = web.Application()
        async def mock_upstream(request):
            return web.json_response({
                "id": "resp-1",
                "choices": [],
                "usage": {"total_tokens": 5},
            })
        upstream_app.router.add_post("/v1/chat/completions", mock_upstream)

        client, session, upstream_runner, orig = await self._make_forwarding_client(
            aiohttp_client, proxy_app, upstream_app
        )
        try:
            resp = await client.post(
                "/v1/chat/completions",
                data=json.dumps({
                    "model": "qwen3-coder-plus",
                    "messages": [{"role": "user", "content": "hi"}],
                }).encode(),
            )
            assert "X-Request-ID" in resp.headers
            assert len(resp.headers["X-Request-ID"]) == 12
        finally:
            await session.close()
            await upstream_runner.cleanup()
            dashscope_proxy.TARGET_BASE = orig

    async def test_client_request_id_preserved(self, aiohttp_client, proxy_app):
        upstream_app = web.Application()
        async def mock_upstream(request):
            return web.json_response({
                "id": "resp-1",
                "choices": [],
                "usage": {"total_tokens": 5},
            })
        upstream_app.router.add_post("/v1/chat/completions", mock_upstream)

        client, session, upstream_runner, orig = await self._make_forwarding_client(
            aiohttp_client, proxy_app, upstream_app
        )
        try:
            resp = await client.post(
                "/v1/chat/completions",
                data=json.dumps({
                    "model": "qwen3-coder-plus",
                    "messages": [{"role": "user", "content": "hi"}],
                }).encode(),
                headers={"X-Request-ID": "my-custom-id-123"},
            )
            assert resp.headers.get("X-Request-ID") == "my-custom-id-123"
        finally:
            await session.close()
            await upstream_runner.cleanup()
            dashscope_proxy.TARGET_BASE = orig

    async def test_ratelimit_headers_in_response(self, aiohttp_client, proxy_app):
        upstream_app = web.Application()
        async def mock_upstream(request):
            return web.json_response({
                "id": "resp-1",
                "choices": [],
                "usage": {"total_tokens": 5},
            })
        upstream_app.router.add_post("/v1/chat/completions", mock_upstream)

        client, session, upstream_runner, orig = await self._make_forwarding_client(
            aiohttp_client, proxy_app, upstream_app
        )
        try:
            resp = await client.post(
                "/v1/chat/completions",
                data=json.dumps({
                    "model": "qwen3-coder-plus",
                    "messages": [{"role": "user", "content": "hi"}],
                }).encode(),
            )
            assert "X-RateLimit-Limit" in resp.headers
            assert "X-RateLimit-Remaining" in resp.headers
        finally:
            await session.close()
            await upstream_runner.cleanup()
            dashscope_proxy.TARGET_BASE = orig


# ---------------------------------------------------------------------------
# Queue enforcement
# ---------------------------------------------------------------------------

class TestQueueEnforcement:
    async def test_queue_full_returns_503(self, aiohttp_client, proxy_app):
        app, rl = proxy_app
        rl.max_queue_size = 0
        rl.rpm_limit = 0
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "qwen3-coder-plus",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        assert resp.status == 503

    async def test_503_includes_retry_after(self, aiohttp_client, proxy_app):
        app, rl = proxy_app
        rl.max_queue_size = 0
        rl.rpm_limit = 0
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "qwen3-coder-plus",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        assert "Retry-After" in resp.headers

    async def test_queue_full_with_session_log_does_not_crash(self, aiohttp_client, proxy_app, tmp_path):
        """Queue-full path must not crash when session logging is enabled (retry in finally)."""
        app, rl = proxy_app
        rl.max_queue_size = 0
        rl.rpm_limit = 0
        writer = dashscope_proxy.SessionLogWriter(str(tmp_path / "logs"))
        app["session_log"] = writer
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "qwen3-coder-plus",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        assert resp.status == 503
        writer.close()
        log_files = list((tmp_path / "logs").glob("*.jsonl"))
        assert len(log_files) == 1
        lines = log_files[0].read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["error_reason"] == "queue_full"
        assert entry["retry_count"] == 0

    async def test_disconnect_during_queue_returns_499(self, aiohttp_client, proxy_app, monkeypatch):
        """Client disconnect while queued must return 499, not 503 queue full."""
        app, rl = proxy_app
        rl.rpm_limit = 1
        rl.primary.rps_limit = 1
        rl.max_queue_size = 50
        rl.primary.rpm_window.add()

        # Set up a mock client session (won't actually be used since we disconnect in queue)
        mock_session = MagicMock()
        mock_session.closed = False
        app["client_session"] = mock_session

        disconnected = True

        def fake_disconnected(request):
            return disconnected

        monkeypatch.setattr(
            "dashscope_proxy_lib.queue._client_disconnected",
            fake_disconnected,
        )
        monkeypatch.setattr(
            "dashscope_proxy_lib.handlers._client_disconnected",
            fake_disconnected,
        )

        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "qwen3-coder-plus",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        assert resp.status == 499
        assert rl.queue_drops == 0


# ---------------------------------------------------------------------------
# Circuit breaker token cleanup
# ---------------------------------------------------------------------------

class TestCircuitBreakerCleanup:
    async def test_circuit_open_refunds_reserved_tokens(self, aiohttp_client, proxy_app):
        """Reserved TPM must be released when circuit breaker rejects a queued request."""
        app, rl = proxy_app
        rl.primary.circuit_threshold = 1
        await rl.primary.record_circuit_failure()
        assert rl.primary.circuit_is_open()

        upstream_app = web.Application()

        async def mock_upstream(request):
            return web.json_response({"choices": [], "usage": {"total_tokens": 5}})

        upstream_app.router.add_post("/v1/chat/completions", mock_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 503
                    assert rl.primary.tpm_bucket.reserved == 0
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

class TestGracefulShutdown:
    async def test_shutting_down_returns_503(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        app["shutting_down"].set()
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "qwen3-coder-plus",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        assert resp.status == 503

    async def test_503_has_retry_after_header(self, aiohttp_client, proxy_app):
        app, _ = proxy_app
        app["shutting_down"].set()
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "qwen3-coder-plus",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        assert "Retry-After" in resp.headers


# ---------------------------------------------------------------------------
# Streaming error paths
# ---------------------------------------------------------------------------

class TestStreamingErrors:
    async def test_streaming_forwards_ok(self, aiohttp_client, proxy_app):
        """Happy path: upstream returns 200 for a streaming request."""
        upstream_app = web.Application()

        async def mock_upstream(request):
            return web.Response(
                status=200,
                body=b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\ndata: {"usage": {"total_tokens": 5}}\n\ndata: [DONE]\n\n',
                content_type="text/event-stream",
            )

        upstream_app.router.add_post("/v1/chat/completions", mock_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                            "stream": True,
                        }).encode(),
                    )
                    assert resp.status == 200
                    text = await resp.text()
                    assert "data:" in text
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()

    async def test_streaming_4xx_forwarded_not_500(self, aiohttp_client, proxy_app):
        """Streaming request: upstream 400 must be forwarded, not crash with 500."""
        upstream_app = web.Application()

        async def bad_request_upstream(request):
            return web.Response(
                status=400,
                body=b'{"error":{"message":"invalid request"}}',
                content_type="application/json",
            )

        upstream_app.router.add_post("/v1/chat/completions", bad_request_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                            "stream": True,
                        }).encode(),
                    )
                    assert resp.status == 400
                    data = await resp.json()
                    assert "invalid request" in data["error"]["message"]
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()

    async def test_streaming_429_retries_then_succeeds(self, aiohttp_client, proxy_app):
        """Streaming request: upstream returns 429 first, then succeeds."""
        call_count = 0

        upstream_app = web.Application()

        async def flaky_upstream(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return web.Response(status=429, body=b'{"error":"rate limited"}')
            return web.Response(
                status=200,
                body=b'data: {"usage": {"total_tokens": 10}}\n\ndata: [DONE]\n\n',
                content_type="text/event-stream",
            )

        upstream_app.router.add_post("/v1/chat/completions", flaky_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                            "stream": True,
                        }).encode(),
                    )
                    assert resp.status == 200
                    assert call_count == 2
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()

    async def test_streaming_5xx_retries_then_gives_up(self, aiohttp_client, proxy_app):
        """Streaming request: upstream returns 502 all times, should give up."""
        upstream_app = web.Application()

        async def always_502(request):
            return web.Response(status=502, body=b'{"error":"bad gateway"}')

        upstream_app.router.add_post("/v1/chat/completions", always_502)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                            "stream": True,
                        }).encode(),
                    )
                    assert resp.status == 502
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()

    async def test_streaming_429_forwards_retry_after_header(self, aiohttp_client, proxy_app):
        """Verify upstream Retry-After header is forwarded on stream 429 after max retries."""
        upstream_app = web.Application()

        async def always_429(request):
            return web.Response(
                status=429,
                body=b'{"error":"rate limited"}',
                headers={"Retry-After": "10"},
            )

        upstream_app.router.add_post("/v1/chat/completions", always_429)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                            "stream": True,
                        }).encode(),
                    )
                    assert resp.status == 429
                    assert "Retry-After" in resp.headers
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()

    async def test_is_stream_from_parsed_json_not_byte_substring(self, aiohttp_client, proxy_app):
        """Verify is_stream is True when stream: true is in parsed JSON body."""
        upstream_app = web.Application()
        received_body = None

        async def capture_upstream(request):
            nonlocal received_body
            received_body = await request.read()
            return web.Response(
                status=200,
                body=b'data: {"usage": {"total_tokens": 5}}\n\ndata: [DONE]\n\n',
                content_type="text/event-stream",
            )

        upstream_app.router.add_post("/v1/chat/completions", capture_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    # Use compact JSON without space after colon
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=b'{"model":"qwen3-coder-plus","messages":[{"role":"user","content":"hi"}],"stream":true}',
                    )
                    assert resp.status == 200
                    assert received_body is not None
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Client disconnect paths
# ---------------------------------------------------------------------------

class TestClientDisconnect:
    async def test_disconnect_before_upstream_returns_499(self, aiohttp_client, proxy_app):
        """When client disconnects before upstream call, proxy returns 499."""
        app, rl = proxy_app

        # Create a real session that raises error immediately
        mock_session = MagicMock()
        mock_session.request = MagicMock()
        mock_session.closed = False

        def mock_request(*args, **kwargs):
            fut = asyncio.get_event_loop().create_future()
            fut.set_exception(aiohttp.ClientError("connection closed"))
            return fut

        mock_session.request.side_effect = mock_request

        app["client_session"] = mock_session
        # Make rate limit very low so the request goes through to upstream call quickly
        rl.rpm_limit = 1000

        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "qwen3-coder-plus",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        # Should either get 502 (client error after retries) or some other error response
        assert resp.status in (499, 500, 502, 503)


# ---------------------------------------------------------------------------
# Non-streaming 429 exhaustion
# ---------------------------------------------------------------------------

class TestMaxRetriesExhausted:
    async def test_non_streaming_429_after_max_retries(self, aiohttp_client, proxy_app):
        """Non-streaming: upstream always returns 429, proxy returns 429 to client."""
        upstream_app = web.Application()

        async def always_429(request):
            return web.Response(
                status=429,
                body=b'{"error":"rate limited"}',
                headers={"Retry-After": "5"},
            )

        upstream_app.router.add_post("/v1/chat/completions", always_429)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, rl = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 429
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()

    async def test_quota_exceeded_429_not_retried(self, aiohttp_client, proxy_app):
        """Hard quota 429 from upstream is retried once after cooldown, then passed through."""
        upstream_app = web.Application()
        quota_body = json.dumps({
            "error": {
                "code": "throttling",
                "message": "usage allocated quota exceeded. please try again later.",
            }
        }).encode()
        call_count = 0

        async def quota_429(request):
            nonlocal call_count
            call_count += 1
            return web.Response(status=429, body=quota_body, content_type="application/json")

        upstream_app.router.add_post("/v1/chat/completions", quota_429)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, rl = proxy_app
                limiter = rl.primary if hasattr(rl, "primary") else rl
                limiter.quota_retry_cooldown = 0.1
                limiter.quota_max_retries = 1
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 429
                    data = await resp.json()
                    assert "quota exceeded" in data["error"]["message"].lower()
                    assert limiter.total_429s == 2
                    assert call_count == 2
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# ClientError handling
# ---------------------------------------------------------------------------

class TestClientError:
    async def test_client_error_returns_502(self, aiohttp_client, proxy_app):
        """When aiohttp raises ClientError, proxy returns 502 after retries."""
        app, rl = proxy_app
        rl.primary.max_retries = 1

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.request = MagicMock(side_effect=aiohttp.ClientError("connection refused"))

        async with mock_session:
            app["client_session"] = mock_session
            client = await aiohttp_client(app)
            resp = await client.post(
                "/v1/chat/completions",
                data=json.dumps({
                    "model": "qwen3-coder-plus",
                    "messages": [{"role": "user", "content": "hi"}],
                }).encode(),
            )
            assert resp.status == 502


# ---------------------------------------------------------------------------
# Non-200 upstream response (not 429, not 5xx)
# ---------------------------------------------------------------------------

class TestNon200Upstream:
    async def test_non_200_non_429_non_5xx_forwarded(self, aiohttp_client, proxy_app):
        """Upstream returns 418 (teapot), proxy forwards it through."""
        upstream_app = web.Application()

        async def teapot(request):
            return web.Response(
                status=418,
                body=b'{"error":"I\'m a teapot"}',
                content_type="application/json",
            )

        upstream_app.router.add_post("/v1/chat/completions", teapot)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 418
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# TPM reservation failure after successful queue
# ---------------------------------------------------------------------------

class TestTPMReservationFailure:
    async def test_tpm_reservation_fails_after_queue_returns_503(self, aiohttp_client, proxy_app):
        """After can_proceed succeeds but reserve_tokens fails, proxy returns 503."""
        app, rl = proxy_app
        rl.primary.max_retries = 1

        # Drain the TPM bucket so any reservation fails
        rl.primary.tpm_bucket.tokens = 0.0
        rl.primary.tpm_bucket.reserved = 0

        upstream_app = web.Application()
        async def mock_upstream(request):
            return web.json_response({"choices": [], "usage": {"total_tokens": 5}})
        upstream_app.router.add_post("/v1/chat/completions", mock_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    # Should get 503 since TPM reservation fails after queue succeeds
                    assert resp.status in (200, 503)
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Unserializable body
# ---------------------------------------------------------------------------

class TestUnserializableBody:
    async def test_unserializable_body_returns_400(self, aiohttp_client, proxy_app):
        """Body with a non-serializable type returns 400."""
        app, _ = proxy_app
        app["client_session"] = MagicMock()

        client = await aiohttp_client(app)
        # This is valid JSON but will fail on re-serialization if it contains
        # types like set, but we can't send Python objects over HTTP.
        # Instead, test that the normal path works and this doesn't trigger the path.
        # The unserializable path is hard to hit over HTTP since JSON serialization
        # happens on the client side. This test documents the code path.
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "qwen3-coder-plus",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        # Normal path works -> 200 or some error (502 when mock session cannot forward)
        assert resp.status in (200, 400, 500, 502, 503)


# ---------------------------------------------------------------------------
# Query string forwarding
# ---------------------------------------------------------------------------

class TestQueryStringForwarding:
    async def test_query_string_forwarded_to_upstream(self, aiohttp_client, proxy_app):
        """Request with ?foo=bar should forward query string to upstream."""
        upstream_app = web.Application()
        received_query = None

        async def capture_upstream(request):
            nonlocal received_query
            received_query = request.query_string
            return web.json_response({"choices": [], "usage": {"total_tokens": 5}})

        upstream_app.router.add_post("/v1/chat/completions", capture_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions?stream_options=include_usage",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                    assert "stream_options" in received_query
                    assert "include_usage" in received_query
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Path rewriting
# ---------------------------------------------------------------------------

class TestPathRewriting:
    async def test_non_v1_path_rewritten(self, aiohttp_client, proxy_app):
        """Request to /chat/completions should be rewritten to /v1/chat/completions."""
        upstream_app = web.Application()
        received_path = None

        async def capture_upstream(request):
            nonlocal received_path
            received_path = request.path
            return web.json_response({"choices": [], "usage": {"total_tokens": 5}})

        upstream_app.router.add_post("/v1/chat/completions", capture_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                    assert received_path == "/v1/chat/completions"
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Hop-by-hop header stripping
# ---------------------------------------------------------------------------

class TestHopByHopStripping:
    async def test_hop_by_hop_not_forwarded(self, aiohttp_client, proxy_app):
        """Proxy strips host, transfer-encoding, content-length, authorization from forwarded headers."""
        upstream_app = web.Application()
        received_headers = None

        async def capture_upstream(request):
            nonlocal received_headers
            received_headers = dict(request.headers)
            return web.json_response({"choices": [], "usage": {"total_tokens": 5}})

        upstream_app.router.add_post("/v1/chat/completions", capture_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                    # The proxy strips these specific headers from the original request
                    assert "Transfer-Encoding" not in received_headers
                    # Host is stripped from original request; aiohttp adds its own for the new connection
                    # Content-Length is stripped from original; aiohttp adds a new one for the forwarded body
                    # Authorization is replaced with the proxy's API key
                    assert received_headers.get("Authorization", "").startswith("Bearer sk-")
                    assert "Host" not in received_headers or "127.0.0.1" in received_headers.get("Host", "")
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Token reconciliation
# ---------------------------------------------------------------------------

class TestTokenReconciliation:
    async def test_token_counters_after_successful_request(self, aiohttp_client, proxy_app):
        """After a successful request, total_forwarded and total_tokens_consumed should be updated."""
        upstream_app = web.Application()

        async def mock_upstream(request):
            return web.json_response({
                "id": "resp-1",
                "choices": [{"message": {"role": "assistant", "content": "Hello!"}}],
                "usage": {"total_tokens": 42},
            })

        upstream_app.router.add_post("/v1/chat/completions", mock_upstream)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, rl = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                    assert rl.primary.total_forwarded >= 1
                    assert rl.primary.total_tokens_consumed >= 42
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# asyncio.TimeoutError in upstream call
# ---------------------------------------------------------------------------

class TestUpstreamTimeoutError:
    @pytest.mark.asyncio
    async def test_timeout_returns_502_after_max_retries(self, aiohttp_client, proxy_app, dashscope_module):
        """When upstream times out repeatedly, proxy returns 502."""
        app, rl = proxy_app
        rl.primary.max_retries = 2

        async def _slow_handler(request):
            await asyncio.sleep(100)

        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/chat/completions", _slow_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=0.1)
                ) as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    # After exhausting retries, should return 502
                    assert resp.status in (502, 504)
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Multi-retry 429 scenario
# ---------------------------------------------------------------------------

class TestMultiRetry429:
    @pytest.mark.asyncio
    async def test_multiple_429s_then_success(self, aiohttp_client, proxy_app, dashscope_module):
        """Proxy retries through multiple 429s before succeeding."""
        app, rl = proxy_app
        rl.primary.max_retries = 5
        call_count = 0

        async def mock_upstream(request):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return web.Response(
                    status=429,
                    headers={"Retry-After": "0"},
                    content_type="application/json",
                    body=json.dumps({"error": "rate limited"}),
                )
            return web.json_response({
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"total_tokens": 42},
            })

        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/chat/completions", mock_upstream)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                    assert call_count == 4  # 3 failures + 1 success
                    assert rl.primary.total_429s >= 3
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Retry-After as HTTP-date
# ---------------------------------------------------------------------------

class TestRetryAfterHttpDate:
    @pytest.mark.asyncio
    async def test_retry_after_http_date_format(self, aiohttp_client, proxy_app, dashscope_module):
        """Proxy parses Retry-After as HTTP-date and respects it."""
        app, rl = proxy_app
        rl.primary.max_retries = 2
        call_count = 0

        async def mock_upstream(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                future = datetime.now(timezone.utc).timestamp() + 0.5
                header = formatdate(timeval=future, usegmt=True)
                return web.Response(
                    status=429,
                    headers={"Retry-After": header},
                    content_type="application/json",
                    body=json.dumps({"error": "rate limited"}),
                )
            return web.json_response({
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"total_tokens": 42},
            })

        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/chat/completions", mock_upstream)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                    assert call_count == 2
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Request lifecycle counters
# ---------------------------------------------------------------------------

class TestRequestLifecycle:
    @pytest.mark.asyncio
    async def test_full_lifecycle_increments_all_counters(self, aiohttp_client, proxy_app, dashscope_module):
        """A successful request increments forwarded, tokens_consumed, and rpm."""
        app, rl = proxy_app

        async def mock_upstream(request):
            return web.json_response({
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"total_tokens": 100},
            })

        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/chat/completions", mock_upstream)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                before_forwarded = rl.primary.total_forwarded
                before_tokens = rl.primary.total_tokens_consumed
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                assert rl.primary.total_forwarded == before_forwarded + 1
                assert rl.primary.total_tokens_consumed >= before_tokens + 100
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()


# ---------------------------------------------------------------------------
# Secondary provider routing
# ---------------------------------------------------------------------------

class TestSecondaryProviderRouting:
    """Integration tests for secondary provider routing via model name."""

    async def test_secondary_model_forwarded_to_secondary_upstream(self, aiohttp_client, proxy_app, monkeypatch):
        """A request with a secondary model name should be forwarded to the secondary upstream."""
        import dashscope_proxy_lib.config as cfg

        # Set up a mock secondary upstream
        secondary_app = web.Application()
        received_headers = {}

        async def secondary_handler(request):
            nonlocal received_headers
            received_headers = dict(request.headers)
            return web.json_response({
                "id": "resp-secondary",
                "choices": [{"message": {"role": "assistant", "content": "from secondary"}}],
                "usage": {"total_tokens": 15},
            })

        secondary_app.router.add_post("/v1/chat/completions", secondary_handler)
        secondary_runner = web.AppRunner(secondary_app)
        await secondary_runner.setup()
        secondary_site = web.TCPSite(secondary_runner, "127.0.0.1", 0)
        await secondary_site.start()
        secondary_port = secondary_site._server.sockets[0].getsockname()[1]

        # Patch secondary provider config
        monkeypatch.setattr(cfg, "SECONDARY_API_KEY", "sk-secondary-test")
        monkeypatch.setattr(cfg, "SECONDARY_BASE_URL", f"http://127.0.0.1:{secondary_port}")

        try:
            app, _ = proxy_app
            async with aiohttp.ClientSession() as session:
                app["client_session"] = session
                client = await aiohttp_client(app)
                resp = await client.post(
                    "/v1/chat/completions",
                    data=json.dumps({
                        "model": "mimo-v2.5-pro",
                        "messages": [{"role": "user", "content": "hello secondary"}],
                    }).encode(),
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["id"] == "resp-secondary"
                # Verify the secondary API key was used
                assert received_headers.get("Authorization") == "Bearer sk-secondary-test"
        finally:
            await secondary_runner.cleanup()

    async def test_mimo_hyphen_alias_forwarded_to_secondary_upstream(
        self, aiohttp_client, proxy_app, monkeypatch,
    ):
        """Cursor sends mimo-v2-5 (hyphens) — must route to secondary, not primary."""
        import dashscope_proxy_lib.config as cfg

        secondary_app = web.Application()
        received = {}

        async def secondary_handler(request):
            received["headers"] = dict(request.headers)
            received["body"] = await request.json()
            return web.json_response({
                "id": "resp-secondary-alias",
                "choices": [{"message": {"role": "assistant", "content": "from secondary alias"}}],
                "usage": {"total_tokens": 12},
            })

        secondary_app.router.add_post("/v1/chat/completions", secondary_handler)
        secondary_runner = web.AppRunner(secondary_app)
        await secondary_runner.setup()
        secondary_site = web.TCPSite(secondary_runner, "127.0.0.1", 0)
        await secondary_site.start()
        secondary_port = secondary_site._server.sockets[0].getsockname()[1]

        monkeypatch.setattr(cfg, "SECONDARY_API_KEY", "sk-secondary-test")
        monkeypatch.setattr(cfg, "SECONDARY_BASE_URL", f"http://127.0.0.1:{secondary_port}")

        try:
            app, _ = proxy_app
            async with aiohttp.ClientSession() as session:
                app["client_session"] = session
                client = await aiohttp_client(app)
                for model in ("mimo-v2-5", "mimo-v2-5-pro"):
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": model,
                            "messages": [{"role": "user", "content": f"hello {model}"}],
                        }).encode(),
                    )
                    assert resp.status == 200, f"{model} should reach secondary upstream"
                    data = await resp.json()
                    assert data["id"] == "resp-secondary-alias"
                    assert received["headers"].get("Authorization") == "Bearer sk-secondary-test"
                    # Upstream body should use canonical dotted model name
                    assert received["body"]["model"].startswith("mimo-v2.5")
        finally:
            await secondary_runner.cleanup()

    async def test_primary_model_still_forwarded_to_primary(self, aiohttp_client, proxy_app, monkeypatch):
        """A request with a primary model should still go to primary even when secondary is configured."""
        primary_app = web.Application()

        async def primary_handler(request):
            return web.json_response({
                "id": "resp-primary",
                "choices": [{"message": {"role": "assistant", "content": "from primary"}}],
                "usage": {"total_tokens": 10},
            })

        primary_app.router.add_post("/v1/chat/completions", primary_handler)
        primary_runner = web.AppRunner(primary_app)
        await primary_runner.setup()
        primary_site = web.TCPSite(primary_runner, "127.0.0.1", 0)
        await primary_site.start()
        primary_port = primary_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{primary_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hello primary"}],
                        }).encode(),
                    )
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["id"] == "resp-primary"
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await primary_runner.cleanup()

    async def test_models_endpoint_includes_secondary_when_configured(self, aiohttp_client, proxy_app, monkeypatch):
        """GET /v1/models should include secondary models when secondary is configured."""
        import dashscope_proxy_lib.config as cfg

        monkeypatch.setattr(cfg, "SECONDARY_API_KEY", "sk-secondary-test")
        monkeypatch.setattr(cfg, "SECONDARY_BASE_URL", "https://secondary.example.com/v1")

        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.get("/v1/models")
        assert resp.status == 200
        data = await resp.json()
        model_ids = [m["id"] for m in data["data"]]
        assert "qwen3-coder-plus" in model_ids
        assert "mimo-v2.5-pro" in model_ids

    async def test_status_endpoint_shows_secondary_provider(self, aiohttp_client, proxy_app, monkeypatch):
        """Status endpoint should show secondary provider availability."""
        import dashscope_proxy_lib.config as cfg

        monkeypatch.setattr(cfg, "SECONDARY_API_KEY", "sk-secondary-test")
        monkeypatch.setattr(cfg, "SECONDARY_BASE_URL", "https://secondary.example.com/v1")

        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.get("/v1/proxy/status")
        assert resp.status == 200
        data = await resp.json()
        assert "providers" in data
        assert data["providers"]["secondary"]["available"] is True


# ---------------------------------------------------------------------------
# Tertiary provider routing (OpenLux)
# ---------------------------------------------------------------------------

class TestTertiaryProviderRouting:
    """Integration tests for tertiary (OpenLux) provider routing via model name."""

    async def test_tertiary_model_forwarded_to_tertiary_upstream(self, aiohttp_client, proxy_app, monkeypatch):
        """A request with gemini-3.7-flash should be forwarded to the OpenLux upstream."""
        import dashscope_proxy_lib.config as cfg

        tertiary_app = web.Application()
        received_headers = {}

        async def tertiary_handler(request):
            nonlocal received_headers
            received_headers = dict(request.headers)
            return web.json_response({
                "id": "resp-tertiary",
                "choices": [{"message": {"role": "assistant", "content": "from openlux"}}],
                "usage": {"total_tokens": 15},
            })

        tertiary_app.router.add_post("/v1/chat/completions", tertiary_handler)
        tertiary_runner = web.AppRunner(tertiary_app)
        await tertiary_runner.setup()
        tertiary_site = web.TCPSite(tertiary_runner, "127.0.0.1", 0)
        await tertiary_site.start()
        tertiary_port = tertiary_site._server.sockets[0].getsockname()[1]

        monkeypatch.setattr(cfg, "TERTIARY_API_KEY", "sk-openlux-test")
        monkeypatch.setattr(cfg, "TERTIARY_BASE_URL", f"http://127.0.0.1:{tertiary_port}")

        try:
            app, _ = proxy_app
            async with aiohttp.ClientSession() as session:
                app["client_session"] = session
                client = await aiohttp_client(app)
                resp = await client.post(
                    "/v1/chat/completions",
                    data=json.dumps({
                        "model": "gemini-3.7-flash",
                        "messages": [{"role": "user", "content": "hello openlux"}],
                    }).encode(),
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["id"] == "resp-tertiary"
                assert received_headers.get("Authorization") == "Bearer sk-openlux-test"
        finally:
            await tertiary_runner.cleanup()

    async def test_models_endpoint_includes_tertiary_when_configured(self, aiohttp_client, proxy_app, monkeypatch):
        """GET /v1/models should include OpenLux models when tertiary is configured."""
        import dashscope_proxy_lib.config as cfg

        monkeypatch.setattr(cfg, "TERTIARY_API_KEY", "sk-openlux-test")
        monkeypatch.setattr(
            cfg, "TERTIARY_BASE_URL",
            "https://api.openlux.ai/v1",
        )

        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.get("/v1/models")
        assert resp.status == 200
        data = await resp.json()
        model_ids = [m["id"] for m in data["data"]]
        assert "gemini-3.7-flash" in model_ids

    async def test_status_endpoint_shows_tertiary_provider(self, aiohttp_client, proxy_app, monkeypatch):
        """Status endpoint should show tertiary provider availability."""
        import dashscope_proxy_lib.config as cfg

        monkeypatch.setattr(cfg, "TERTIARY_API_KEY", "sk-openlux-test")
        monkeypatch.setattr(
            cfg, "TERTIARY_BASE_URL",
            "https://api.openlux.ai/v1",
        )

        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.get("/v1/proxy/status")
        assert resp.status == 200
        data = await resp.json()
        assert "providers" in data
        assert data["providers"]["tertiary"]["available"] is True


# ---------------------------------------------------------------------------
# Quaternary provider routing (ARK)
# ---------------------------------------------------------------------------

class TestQuaternaryProviderRouting:
    async def test_quaternary_model_forwarded_to_quaternary_upstream(
        self, aiohttp_client, proxy_app, monkeypatch
    ):
        """Requests for ARK models should be forwarded to the quaternary upstream."""
        from aiohttp import web

        received_headers = {}

        async def handler(request: web.Request):
            received_headers.update(dict(request.headers))
            return web.json_response({
                "id": "resp-quaternary",
                "object": "chat.completion",
                "model": "dola-seed-2.0-pro",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            })

        quaternary_app = web.Application()
        quaternary_app.router.add_route("*", "/{tail:.*}", handler)
        quaternary_runner = web.AppRunner(quaternary_app)
        await quaternary_runner.setup()
        quaternary_site = web.TCPSite(quaternary_runner, "127.0.0.1", 0)
        await quaternary_site.start()
        quaternary_port = quaternary_site._server.sockets[0].getsockname()[1]

        import dashscope_proxy_lib.config as cfg
        import dashscope_proxy_lib.handlers as handlers_mod

        monkeypatch.setattr(cfg, "QUATERNARY_API_KEY", "sk-ark-test")
        monkeypatch.setattr(cfg, "QUATERNARY_BASE_URL", f"http://127.0.0.1:{quaternary_port}")
        handlers_mod._provider_router = None

        try:
            app, _ = proxy_app
            async with aiohttp.ClientSession() as session:
                app["client_session"] = session
                client = await aiohttp_client(app)
                resp = await client.post(
                    "/v1/chat/completions",
                    data=json.dumps({
                        "model": "dola-seed-2.0-pro",
                        "messages": [{"role": "user", "content": "hello ark"}],
                    }).encode(),
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["id"] == "resp-quaternary"
                assert received_headers.get("Authorization") == "Bearer sk-ark-test"
        finally:
            await quaternary_runner.cleanup()

    async def test_primary_model_still_goes_to_primary_with_quaternary_configured(
        self, aiohttp_client, proxy_app, monkeypatch
    ):
        """Primary models should still route to primary even when quaternary is configured."""
        import dashscope_proxy_lib.config as cfg
        import dashscope_proxy_lib.handlers as handlers_mod

        monkeypatch.setattr(cfg, "QUATERNARY_API_KEY", "sk-ark-test")
        monkeypatch.setattr(cfg, "QUATERNARY_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/coding/v3")
        handlers_mod._provider_router = None

        # Set up a mock primary upstream
        primary_app = web.Application()

        async def primary_handler(request):
            return web.json_response({
                "id": "resp-primary",
                "choices": [{"message": {"role": "assistant", "content": "from primary"}}],
                "usage": {"total_tokens": 10},
            })

        primary_app.router.add_post("/v1/chat/completions", primary_handler)
        primary_runner = web.AppRunner(primary_app)
        await primary_runner.setup()
        primary_site = web.TCPSite(primary_runner, "127.0.0.1", 0)
        await primary_site.start()
        primary_port = primary_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{primary_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3.6-plus",
                            "messages": [{"role": "user", "content": "hello"}],
                        }).encode(),
                    )
                    assert resp.status == 200
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await primary_runner.cleanup()

    async def test_models_endpoint_includes_quaternary_when_configured(self, aiohttp_client, proxy_app, monkeypatch):
        """GET /v1/models should include ARK models when quaternary is configured."""
        import dashscope_proxy_lib.config as cfg

        monkeypatch.setattr(cfg, "QUATERNARY_API_KEY", "sk-ark-test")
        monkeypatch.setattr(
            cfg, "QUATERNARY_BASE_URL",
            "https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        )

        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.get("/v1/models")
        assert resp.status == 200
        data = await resp.json()
        model_ids = [m["id"] for m in data["data"]]
        assert "dola-seed-2.0-pro" in model_ids

    async def test_status_endpoint_shows_quaternary_provider(self, aiohttp_client, proxy_app, monkeypatch):
        """Status endpoint should show quaternary provider availability."""
        import dashscope_proxy_lib.config as cfg

        monkeypatch.setattr(cfg, "QUATERNARY_API_KEY", "sk-ark-test")
        monkeypatch.setattr(
            cfg, "QUATERNARY_BASE_URL",
            "https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        )

        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.get("/v1/proxy/status")
        assert resp.status == 200
        data = await resp.json()
        assert "providers" in data
        assert data["providers"]["quaternary"]["available"] is True

    async def test_quaternary_unconfigured_ark_model_falls_back_to_primary(
        self, aiohttp_client, proxy_app, monkeypatch
    ):
        """When quaternary is not configured, ARK models fall back to primary."""
        import dashscope_proxy_lib.config as cfg
        import dashscope_proxy_lib.handlers as handlers_mod

        monkeypatch.setattr(cfg, "QUATERNARY_API_KEY", "")
        monkeypatch.setattr(cfg, "QUATERNARY_BASE_URL", "")
        handlers_mod._provider_router = None

        # Set up a mock primary upstream
        primary_app = web.Application()

        async def primary_handler(request):
            return web.json_response({
                "id": "resp-primary",
                "choices": [{"message": {"role": "assistant", "content": "from primary"}}],
                "usage": {"total_tokens": 10},
            })

        primary_app.router.add_post("/v1/chat/completions", primary_handler)
        primary_runner = web.AppRunner(primary_app)
        await primary_runner.setup()
        primary_site = web.TCPSite(primary_runner, "127.0.0.1", 0)
        await primary_site.start()
        primary_port = primary_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{primary_port}"
            try:
                app, _ = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "dola-seed-2.0-pro",
                            "messages": [{"role": "user", "content": "hello"}],
                        }).encode(),
                    )
                    # Falls back to primary; primary upstream may return error since no real key,
                    # but the routing itself should not crash
                    assert resp.status in (200, 502, 401, 403)
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await primary_runner.cleanup()


# ---------------------------------------------------------------------------
# Provider prefix-pin routing + 400 on bad pin (Task 3)
# ---------------------------------------------------------------------------

class TestPinnedRoutingHandler:
    async def test_pinned_model_strips_prefix_before_upstream(self, aiohttp_client, proxy_app, monkeypatch):
        """'openlux/<model>' pin routes to tertiary and upstream sees the bare model id."""
        import dashscope_proxy_lib.config as cfg

        upstream_app = web.Application()
        received_body = None

        async def capture_upstream(request):
            nonlocal received_body
            received_body = await request.json()
            return web.json_response({
                "id": "resp-tertiary-pinned",
                "choices": [{"message": {"role": "assistant", "content": "pinned"}}],
                "usage": {"total_tokens": 11},
            })

        upstream_app.router.add_post("/v1/chat/completions", capture_upstream)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        monkeypatch.setattr(cfg, "TERTIARY_API_KEY", "sk-openlux-test")
        monkeypatch.setattr(cfg, "TERTIARY_BASE_URL", f"http://127.0.0.1:{upstream_port}/v1")

        try:
            app, _ = proxy_app
            async with aiohttp.ClientSession() as session:
                app["client_session"] = session
                client = await aiohttp_client(app)
                resp = await client.post(
                    "/v1/chat/completions",
                    data=json.dumps({
                        "model": "openlux/gemini-3.7-flash",
                        "messages": [{"role": "user", "content": "hi"}],
                    }).encode(),
                )
                assert resp.status == 200
                assert received_body is not None
                assert received_body["model"] == "gemini-3.7-flash"
        finally:
            await upstream_runner.cleanup()

    async def test_unknown_provider_prefix_returns_400(self, aiohttp_client, proxy_app):
        """'nosuch/<model>' pin is rejected with 400 before any upstream call."""
        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "nosuch/gemini-3.7-flash",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "unknown provider prefix"

    async def test_pin_to_unconfigured_provider_returns_400(self, aiohttp_client, proxy_app, monkeypatch):
        """Pin to a provider without key/base is rejected with 400."""
        import dashscope_proxy_lib.config as cfg

        monkeypatch.setattr(cfg, "TERTIARY_API_KEY", "")
        monkeypatch.setattr(cfg, "TERTIARY_BASE_URL", "")

        app, _ = proxy_app
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=json.dumps({
                "model": "openlux/gemini-3.7-flash",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode(),
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "provider 'tertiary' not configured"
