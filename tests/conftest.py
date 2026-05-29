import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure the project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def dashscope_module():
    """Return the already-imported module."""
    import dashscope_proxy
    return dashscope_proxy


@pytest.fixture
def rate_limiter(dashscope_module):
    """Create a RateLimiter with tiny limits for fast tests."""
    config = {
        "rpm_limit": 60,
        "tpm_limit": 100_000,
        "safety_factor": 0.8,
        "requests_per_5h": 100,
        "requests_per_week": 100,
        "requests_per_month": 100,
        "max_queue_size": 5,
        "max_retries": 3,
        "base_backoff": 0.1,
    }
    return dashscope_module.RateLimiter(config)


@pytest.fixture
def mock_app(rate_limiter):
    """Create a mock aiohttp app with rate_limiter and client_session."""
    app = MagicMock()
    app.__getitem__ = lambda self, key: {
        "rate_limiter": rate_limiter,
        "client_session": AsyncMock(),
        "shutting_down": MagicMock(is_set=lambda: False),
        "get": lambda k, d=None: {
            "shutting_down": MagicMock(is_set=lambda: False),
            "client_session": AsyncMock(),
        }.get(k, d),
    }[key]
    app.get = lambda k, d=None: {
        "shutting_down": MagicMock(is_set=lambda: False),
        "client_session": AsyncMock(),
    }.get(k, d)
    return app


@pytest.fixture
def mock_request(mock_app):
    """Create a mock aiohttp Request."""
    req = MagicMock()
    req.app = mock_app
    req.path = "/v1/chat/completions"
    req.method = "POST"
    req.query_string = ""
    req.headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer user-key",
        "Host": "localhost:8899",
    }
    req.read = AsyncMock(return_value=b'{"model":"qwen3-coder-plus","messages":[{"role":"user","content":"hello"}],"stream":false}')
    req.transport = MagicMock()
    req.transport.is_closing.return_value = False
    return req


@pytest.fixture
def make_test_config(dashscope_module):
    """Return a config dict with large limits for fast tests."""
    def _make():
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
    return _make
