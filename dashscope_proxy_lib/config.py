"""Configuration, constants, and environment setup for the DashScope proxy."""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_BUFFER_SIZE = int(os.environ.get("LOG_BUFFER_SIZE", "2000"))

# ---------------------------------------------------------------------------
# Network configuration
# ---------------------------------------------------------------------------
PROXY_HOST = os.environ.get("DASHSCOPE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("DASHSCOPE_PROXY_PORT", "8899"))
TARGET_BASE = os.environ.get("TARGET_BASE", "https://coding-intl.dashscope.aliyuncs.com")

# ---------------------------------------------------------------------------
# Security: API key from environment
# ---------------------------------------------------------------------------
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# Secondary provider configuration (optional - MIMO Coding Plan)
# Only used if both KEY and BASE_URL are set
# ---------------------------------------------------------------------------
SECONDARY_API_KEY = os.environ.get("MIMO_CODING_PLAN_API_KEY", "").strip()
SECONDARY_BASE_URL = os.environ.get("MIMO_CODING_PLAN_TARGET_BASE", "").strip()

# ---------------------------------------------------------------------------
# Tertiary provider configuration (optional - StreamLake)
# Only used if both KEY and BASE_URL are set
# ---------------------------------------------------------------------------
TERTIARY_API_KEY = os.environ.get("STREAMLAKE_API_KEY", "").strip()
TERTIARY_BASE_URL = os.environ.get("STREAMLAKE_TARGET_BASE", "").strip()

# ---------------------------------------------------------------------------
# Timeout and connection limits
# ---------------------------------------------------------------------------
UPSTREAM_TIMEOUT_TOTAL = int(os.environ.get("UPSTREAM_TIMEOUT_TOTAL", "300"))
UPSTREAM_TIMEOUT_CONNECT = int(os.environ.get("UPSTREAM_TIMEOUT_CONNECT", "10"))
MAX_CONNECTIONS = int(os.environ.get("MAX_CONNECTIONS", "200"))
MAX_CONNECTIONS_PER_HOST = int(os.environ.get("MAX_CONNECTIONS_PER_HOST", "50"))
MAX_BODY_SIZE = int(os.environ.get("MAX_BODY_SIZE", str(50 * 1024 * 1024)))
MAX_STREAM_BUFFER = int(os.environ.get("MAX_STREAM_BUFFER", str(50 * 1024 * 1024)))  # 50 MB cap for streaming response buffer
MAX_5XX_RETRIES = int(os.environ.get("MAX_5XX_RETRIES", "3"))
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
    "rpm_limit": 10,
    "tpm_limit": 4_000_000,
    "safety_factor": 0.8,
    "requests_per_5h": 6000,
    "requests_per_week": 45000,
    "requests_per_month": 90000,
    "max_queue_size": 500,
    "max_retries": 40,
    "base_backoff": 1.0,
}

# ---------------------------------------------------------------------------
# Secondary provider rate limits (optional - falls back to primary limits if not set)
# ---------------------------------------------------------------------------
SECONDARY_CODING_PLAN_CONFIG = {
    "rpm_limit": int(os.environ.get("SECONDARY_RPM_LIMIT", str(CODING_PLAN_CONFIG["rpm_limit"]))),
    "tpm_limit": int(os.environ.get("SECONDARY_TPM_LIMIT", str(CODING_PLAN_CONFIG["tpm_limit"]))),
    "safety_factor": float(os.environ.get("SECONDARY_SAFETY_FACTOR", str(CODING_PLAN_CONFIG["safety_factor"]))),
    "requests_per_5h": int(os.environ.get("SECONDARY_REQUESTS_PER_5H", str(CODING_PLAN_CONFIG["requests_per_5h"]))),
    "requests_per_week": int(os.environ.get("SECONDARY_REQUESTS_PER_WEEK", str(CODING_PLAN_CONFIG["requests_per_week"]))),
    "requests_per_month": int(os.environ.get("SECONDARY_REQUESTS_PER_MONTH", str(CODING_PLAN_CONFIG["requests_per_month"]))),
    "max_queue_size": int(os.environ.get("SECONDARY_MAX_QUEUE_SIZE", str(CODING_PLAN_CONFIG["max_queue_size"]))),
    "max_retries": int(os.environ.get("SECONDARY_MAX_RETRIES", str(CODING_PLAN_CONFIG["max_retries"]))),
    "base_backoff": float(os.environ.get("SECONDARY_BASE_BACKOFF", str(CODING_PLAN_CONFIG["base_backoff"]))),
}

# ---------------------------------------------------------------------------
# Tertiary provider rate limits (StreamLake - independent defaults)
# ---------------------------------------------------------------------------
TERTIARY_DEFAULTS = {
    "rpm_limit": 40,
    "tpm_limit": 6_000_000,
    "safety_factor": 0.8,
    "requests_per_5h": 3000,
    "requests_per_week": 20000,
    "requests_per_month": 50000,
    "max_queue_size": 200,
    "max_retries": 20,
    "base_backoff": 1.0,
}

TERTIARY_CODING_PLAN_CONFIG = {
    "rpm_limit": int(os.environ.get("TERTIARY_RPM_LIMIT", str(TERTIARY_DEFAULTS["rpm_limit"]))),
    "tpm_limit": int(os.environ.get("TERTIARY_TPM_LIMIT", str(TERTIARY_DEFAULTS["tpm_limit"]))),
    "safety_factor": float(os.environ.get("TERTIARY_SAFETY_FACTOR", str(TERTIARY_DEFAULTS["safety_factor"]))),
    "requests_per_5h": int(os.environ.get("TERTIARY_REQUESTS_PER_5H", str(TERTIARY_DEFAULTS["requests_per_5h"]))),
    "requests_per_week": int(os.environ.get("TERTIARY_REQUESTS_PER_WEEK", str(TERTIARY_DEFAULTS["requests_per_week"]))),
    "requests_per_month": int(os.environ.get("TERTIARY_REQUESTS_PER_MONTH", str(TERTIARY_DEFAULTS["requests_per_month"]))),
    "max_queue_size": int(os.environ.get("TERTIARY_MAX_QUEUE_SIZE", str(TERTIARY_DEFAULTS["max_queue_size"]))),
    "max_retries": int(os.environ.get("TERTIARY_MAX_RETRIES", str(TERTIARY_DEFAULTS["max_retries"]))),
    "base_backoff": float(os.environ.get("TERTIARY_BASE_BACKOFF", str(TERTIARY_DEFAULTS["base_backoff"]))),
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
        {"id": "qwen3.7-plus", "object": "model"},
        {"id": "qwen3.5-plus", "object": "model"},
        {"id": "qwen3-max", "object": "model"},
        {"id": "qwen3-coder-plus", "object": "model"},
        {"id": "qwen3-coder-next", "object": "model"},
        {"id": "kimi-k2-5", "object": "model"},
        {"id": "glm-5-0", "object": "model"},
        {"id": "MiniMax-M2.5", "object": "model"},
    ]
}

# ---------------------------------------------------------------------------
# Secondary provider models (MIMO Coding Plan)
# ---------------------------------------------------------------------------
SECONDARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "mimo-v2.5-pro", "object": "model"},
        {"id": "mimo-v2.5", "object": "model"},
        {"id": "mimo-v2.5-asr", "object": "model"},
        {"id": "mimo-v2.5-tts-voiceclone", "object": "model"},
        {"id": "mimo-v2.5-tts-voicedesign", "object": "model"},
        {"id": "mimo-v2.5-tts", "object": "model"},
        {"id": "mimo-v2-pro", "object": "model"},
        {"id": "mimo-v2-omni", "object": "model"},
        {"id": "mimo-v2-tts", "object": "model"},
    ]
}

# ---------------------------------------------------------------------------
# Tertiary provider models (StreamLake)
# ---------------------------------------------------------------------------
TERTIARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "kat-coder-pro-v2.5", "object": "model"},
    ]
}

# ---------------------------------------------------------------------------
# Explicit model-to-provider mapping (optional overrides)
# Keys are model names, values are "primary", "secondary", or "tertiary".
# When a model is listed here, this mapping takes priority over
# the SECONDARY_MODELS list for routing decisions.
# ---------------------------------------------------------------------------
MODEL_PROVIDER_MAP: dict[str, str] = {}
