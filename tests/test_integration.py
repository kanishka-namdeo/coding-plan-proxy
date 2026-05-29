"""Integration tests for the proxy request handler using aiohttp test server."""
import json
import asyncio
import aiohttp
from aiohttp import web
import pytest

import dashscope_proxy


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


@pytest.fixture
def proxy_app():
    """Create the proxy app with a test rate limiter and mock client_session."""
    rate_limiter = dashscope_proxy.RateLimiter(make_test_config())
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
                    assert rl.total_forwarded >= 1
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
        assert "total_forwarded" in data
        assert "rpm_limit" in data


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
