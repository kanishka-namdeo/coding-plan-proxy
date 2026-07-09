"""Real end-to-end tests against the actual DashScope API.

These tests start a real proxy server locally and forward requests to
the real DashScope upstream. They consume API quota and are skipped by
default. Run with: pytest tests/test_e2e_real.py -v --run-real
"""
import json
import os
import asyncio
import signal
import pytest
from aiohttp import web
import aiohttp

import dashscope_proxy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

E2E_ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")


def _load_env():
    """Load API key from .env file (simple KEY=VALUE format)."""
    env = {}
    if os.path.exists(E2E_ENV_FILE):
        with open(E2E_ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip()
    return env


@pytest.fixture(scope="module")
def real_api_key():
    """Load the real API key from .env."""
    env = _load_env()
    key = env.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        pytest.skip("DASHSCOPE_API_KEY not found in .env")
    return key


@pytest.fixture(scope="module")
def e2e_config():
    """Config with high limits so we don't hit rate limits during tests."""
    return {
        "rpm_limit": 2400,
        "tpm_limit": 5_000_000,
        "safety_factor": 0.8,
        "requests_per_5h": 6000,
        "requests_per_week": 45000,
        "requests_per_month": 90000,
        "max_queue_size": 200,
        "max_retries": 2,
        "base_backoff": 1.0,
    }


@pytest.fixture(scope="module")
async def proxy_server(real_api_key, e2e_config):
    """Start a real proxy server on a random port for the duration of the module."""
    import importlib

    # Set the API key in the module before loading
    dashscope_proxy.DASHSCOPE_API_KEY = real_api_key

    rate_limiter = dashscope_proxy.MultiProviderRateLimiter(e2e_config)
    app = dashscope_proxy.create_app()
    app["rate_limiter"] = rate_limiter
    app["client_session"] = aiohttp.ClientSession()
    app["shutting_down"] = asyncio.Event()

    runner = web.AppRunner(app)
    await runner.setup()
    # Bind to port 0 to get a random available port
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    # Get the actual port
    sock = site._server.sockets[0]
    port = sock.getsockname()[1]

    yield port, rate_limiter

    # Cleanup
    session = app.get("client_session")
    if session and not session.closed:
        await session.close()
    await runner.cleanup()


def _make_request(port, payload):
    """Make a POST request to the local proxy."""
    async def _do():
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json=payload,
            ) as resp:
                body = await resp.read()
                return resp.status, body, dict(resp.headers)
    return asyncio.get_event_loop().run_until_complete(_do())


# ---------------------------------------------------------------------------
# Real E2E Tests
# ---------------------------------------------------------------------------

class TestRealProxyForwarding:
    """Tests that actually hit the DashScope API through the proxy."""

    def test_non_streaming_request(self, proxy_server):
        port, rl = proxy_server
        status, body, headers = _make_request(port, {
            "model": "qwen3-coder-plus",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Say hello in exactly 3 words."},
            ],
            "stream": False,
            "max_tokens": 50,
        })
        assert status == 200, f"Unexpected status {status}: {body.decode(errors='replace')[:500]}"
        data = json.loads(body)
        assert "choices" in data
        assert len(data["choices"]) > 0
        assert "usage" in data
        assert data["usage"]["total_tokens"] > 0
        # Rate limiter should have recorded the request
        assert rl.primary.total_forwarded >= 1

    def test_streaming_request(self, proxy_server):
        port, rl = proxy_server
        status, body, headers = _make_request(port, {
            "model": "qwen3-coder-plus",
            "messages": [
                {"role": "user", "content": "Count from 1 to 3."},
            ],
            "stream": True,
            "max_tokens": 50,
        })
        assert status == 200, f"Unexpected status {status}: {body.decode(errors='replace')[:500]}"
        # Streaming responses are text/event-stream
        text = body.decode(errors="replace")
        assert "data:" in text
        # Should contain a [DONE] marker
        assert "[DONE]" in text

    def test_developer_role_converted(self, proxy_server):
        """Verify that 'developer' role is converted to 'system' before forwarding."""
        port, rl = proxy_server
        status, body, headers = _make_request(port, {
            "model": "qwen3-coder-plus",
            "messages": [
                {"role": "developer", "content": "You are a code reviewer. Be concise."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            "stream": False,
            "max_tokens": 50,
        })
        assert status == 200, f"Unexpected status {status}: {body.decode(errors='replace')[:500]}"
        data = json.loads(body)
        assert "choices" in data
        assert len(data["choices"]) > 0

    def test_response_has_request_id(self, proxy_server):
        """Verify X-Request-ID header is present in real responses."""
        port, _ = proxy_server
        status, body, headers = _make_request(port, {
            "model": "qwen3-coder-plus",
            "messages": [
                {"role": "user", "content": "Hi"},
            ],
            "stream": False,
            "max_tokens": 10,
        })
        assert status == 200
        assert "X-Request-ID" in headers
        assert len(headers["X-Request-ID"]) == 12

    def test_response_has_ratelimit_headers(self, proxy_server):
        """Verify X-RateLimit-* headers are present."""
        port, _ = proxy_server
        status, body, headers = _make_request(port, {
            "model": "qwen3-coder-plus",
            "messages": [
                {"role": "user", "content": "Hi"},
            ],
            "stream": False,
            "max_tokens": 10,
        })
        assert status == 200
        assert "X-RateLimit-Limit" in headers
        assert "X-RateLimit-Remaining" in headers

    def test_mock_models_endpoint(self, proxy_server):
        """Verify /v1/models returns the mock model list."""
        async def _do(port):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/v1/models") as resp:
                    return resp.status, await resp.json()
        status, data = asyncio.get_event_loop().run_until_complete(_do(proxy_server[0]))
        assert status == 200
        assert data["object"] == "list"
        assert len(data["data"]) > 0

    def test_health_endpoint(self, proxy_server):
        async def _do(port):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                    return resp.status, await resp.json()
        status, data = asyncio.get_event_loop().run_until_complete(_do(proxy_server[0]))
        assert status == 200
        assert data["status"] == "ok"

    def test_ready_endpoint(self, proxy_server):
        async def _do(port):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/ready") as resp:
                    return resp.status, await resp.json()
        status, data = asyncio.get_event_loop().run_until_complete(_do(proxy_server[0]))
        assert status == 200
        assert data["status"] == "ready"

    def test_proxy_status_endpoint(self, proxy_server):
        async def _do(port):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/v1/proxy/status") as resp:
                    return resp.status, await resp.json()
        status, data = asyncio.get_event_loop().run_until_complete(_do(proxy_server[0]))
        assert status == 200
        # New multi-provider response structure
        assert "rate_limits" in data
        assert "providers" in data
        assert "primary" in data["rate_limits"]
        assert "total_forwarded" in data["rate_limits"]["primary"]
        assert "rpm_limit" in data["rate_limits"]["primary"]

    def test_validation_empty_body(self, proxy_server):
        """Verify 400 for empty POST body."""
        async def _do(port):
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    data=b"",
                ) as resp:
                    return resp.status
        status = asyncio.get_event_loop().run_until_complete(_do(proxy_server[0]))
        assert status == 400

    def test_validation_missing_model(self, proxy_server):
        """Verify 400 for missing model field."""
        async def _do(port):
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                ) as resp:
                    return resp.status
        status = asyncio.get_event_loop().run_until_complete(_do(proxy_server[0]))
        assert status == 400

    def test_concurrent_requests(self, proxy_server):
        """Send 3 concurrent requests and verify all succeed."""
        port, rl = proxy_server

        async def _do(req_id, port):
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json={
                        "model": "qwen3-coder-plus",
                        "messages": [{"role": "user", "content": f"Request {req_id}"}],
                        "stream": False,
                        "max_tokens": 10,
                    },
                ) as resp:
                    return resp.status, await resp.json()

        async def _run_all():
            tasks = [_do(i, port) for i in range(3)]
            return await asyncio.gather(*tasks)

        results = asyncio.get_event_loop().run_until_complete(_run_all())
        for status, data in results:
            assert status == 200, f"Request failed with status {status}"
            assert "choices" in data
