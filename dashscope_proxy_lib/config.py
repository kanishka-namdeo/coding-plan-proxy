"""Configuration, constants, and environment setup for the DashScope proxy."""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_BUFFER_SIZE = 2000

# ---------------------------------------------------------------------------
# Network configuration
# ---------------------------------------------------------------------------
PROXY_HOST = os.environ.get("DASHSCOPE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("DASHSCOPE_PROXY_PORT", "8899"))
TARGET_BASE = "https://coding-intl.dashscope.aliyuncs.com"

# ---------------------------------------------------------------------------
# Security: API key from environment
# ---------------------------------------------------------------------------
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# Timeout and connection limits
# ---------------------------------------------------------------------------
UPSTREAM_TIMEOUT_TOTAL = 120
UPSTREAM_TIMEOUT_CONNECT = 10
MAX_CONNECTIONS = 200
MAX_CONNECTIONS_PER_HOST = 50
MAX_BODY_SIZE = 10 * 1024 * 1024
MAX_STREAM_BUFFER = 50 * 1024 * 1024  # 50 MB cap for streaming response buffer
MAX_5XX_RETRIES = 3
DEQUE_MAX_SIZE = 100_000

# ---------------------------------------------------------------------------
# Hop-by-hop headers (must not be forwarded)
# ---------------------------------------------------------------------------
HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "transfer-encoding", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "upgrade", "content-length",
})

# ---------------------------------------------------------------------------
# Rate limiting configuration (DashScope Coding Plan)
# ---------------------------------------------------------------------------
CODING_PLAN_CONFIG = {
    "rpm_limit": 15,
    "tpm_limit": 4_000_000,
    "safety_factor": 0.8,
    "requests_per_5h": 6000,
    "requests_per_week": 45000,
    "requests_per_month": 90000,
    "max_queue_size": 500,
    "max_retries": 40,
    "base_backoff": 1.0,
}


def _load_config() -> dict:
    """Load rate limiter config with environment variable overrides.

    Any key in CODING_PLAN_CONFIG can be overridden via PROXY_<KEY_UPPER> env var.
    Example: PROXY_RPM_LIMIT=24 overrides rpm_limit to 24.
    """
    from dashscope_proxy_lib.logging_config import _log
    import logging

    base = CODING_PLAN_CONFIG.copy()
    for key in base:
        env_val = os.environ.get(f"PROXY_{key.upper()}")
        if env_val is not None:
            try:
                base[key] = type(base[key])(env_val)
            except (ValueError, TypeError):
                _log(logging.WARNING, f"Invalid PROXY_{key.upper()} value '{env_val}', using default")
    return base


# ---------------------------------------------------------------------------
# Mock models for /v1/models endpoint
# ---------------------------------------------------------------------------
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
