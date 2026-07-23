"""Configuration, constants, and environment setup for the DashScope proxy."""

import os
from dotenv import load_dotenv

load_dotenv()

def _safe_int(env_name: str, default: int) -> int:
    """Read an env var as int, falling back to default on any parse error."""
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default

def _safe_float(env_name: str, default: float) -> float:
    """Read an env var as float, falling back to default on any parse error."""
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_BUFFER_SIZE = _safe_int("LOG_BUFFER_SIZE", 2000)

# ---------------------------------------------------------------------------
# Network configuration
# ---------------------------------------------------------------------------
PROXY_HOST = os.environ.get("DASHSCOPE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = _safe_int("DASHSCOPE_PROXY_PORT", 8899)
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
# Quaternary provider configuration (optional - ARK / BytePlus)
# Only used if both KEY and BASE_URL are set
# ---------------------------------------------------------------------------
QUATERNARY_API_KEY = os.environ.get("MODEL_ARK_API_KEY", "").strip()
QUATERNARY_BASE_URL = os.environ.get("MODEL_ARK_TARGET_BASE", "").strip()

# ---------------------------------------------------------------------------
# Timeout and connection limits
# ---------------------------------------------------------------------------
UPSTREAM_TIMEOUT_TOTAL = _safe_int("UPSTREAM_TIMEOUT_TOTAL", 300)
UPSTREAM_TIMEOUT_CONNECT = _safe_int("UPSTREAM_TIMEOUT_CONNECT", 10)
MAX_CONNECTIONS = _safe_int("MAX_CONNECTIONS", 200)
MAX_CONNECTIONS_PER_HOST = _safe_int("MAX_CONNECTIONS_PER_HOST", 50)
MAX_BODY_SIZE = _safe_int("MAX_BODY_SIZE", 50 * 1024 * 1024)
MAX_STREAM_BUFFER = _safe_int("MAX_STREAM_BUFFER", 50 * 1024 * 1024)  # 50 MB cap for streaming response buffer
MAX_5XX_RETRIES = _safe_int("MAX_5XX_RETRIES", 3)
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
    "rpm_limit": 12,
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
    "rpm_limit": _safe_int("SECONDARY_RPM_LIMIT", CODING_PLAN_CONFIG["rpm_limit"]),
    "tpm_limit": _safe_int("SECONDARY_TPM_LIMIT", CODING_PLAN_CONFIG["tpm_limit"]),
    "safety_factor": _safe_float("SECONDARY_SAFETY_FACTOR", CODING_PLAN_CONFIG["safety_factor"]),
    "requests_per_5h": _safe_int("SECONDARY_REQUESTS_PER_5H", CODING_PLAN_CONFIG["requests_per_5h"]),
    "requests_per_week": _safe_int("SECONDARY_REQUESTS_PER_WEEK", CODING_PLAN_CONFIG["requests_per_week"]),
    "requests_per_month": _safe_int("SECONDARY_REQUESTS_PER_MONTH", CODING_PLAN_CONFIG["requests_per_month"]),
    "max_queue_size": _safe_int("SECONDARY_MAX_QUEUE_SIZE", CODING_PLAN_CONFIG["max_queue_size"]),
    "max_retries": _safe_int("SECONDARY_MAX_RETRIES", CODING_PLAN_CONFIG["max_retries"]),
    "base_backoff": _safe_float("SECONDARY_BASE_BACKOFF", CODING_PLAN_CONFIG["base_backoff"]),
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
    "rpm_limit": _safe_int("TERTIARY_RPM_LIMIT", TERTIARY_DEFAULTS["rpm_limit"]),
    "tpm_limit": _safe_int("TERTIARY_TPM_LIMIT", TERTIARY_DEFAULTS["tpm_limit"]),
    "safety_factor": _safe_float("TERTIARY_SAFETY_FACTOR", TERTIARY_DEFAULTS["safety_factor"]),
    "requests_per_5h": _safe_int("TERTIARY_REQUESTS_PER_5H", TERTIARY_DEFAULTS["requests_per_5h"]),
    "requests_per_week": _safe_int("TERTIARY_REQUESTS_PER_WEEK", TERTIARY_DEFAULTS["requests_per_week"]),
    "requests_per_month": _safe_int("TERTIARY_REQUESTS_PER_MONTH", TERTIARY_DEFAULTS["requests_per_month"]),
    "max_queue_size": _safe_int("TERTIARY_MAX_QUEUE_SIZE", TERTIARY_DEFAULTS["max_queue_size"]),
    "max_retries": _safe_int("TERTIARY_MAX_RETRIES", TERTIARY_DEFAULTS["max_retries"]),
    "base_backoff": _safe_float("TERTIARY_BASE_BACKOFF", TERTIARY_DEFAULTS["base_backoff"]),
}

# ---------------------------------------------------------------------------
# Quaternary provider rate limits (ARK - independent defaults)
# ---------------------------------------------------------------------------
QUATERNARY_DEFAULTS = {
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

QUATERNARY_CODING_PLAN_CONFIG = {
    "rpm_limit": _safe_int("QUATERNARY_RPM_LIMIT", QUATERNARY_DEFAULTS["rpm_limit"]),
    "tpm_limit": _safe_int("QUATERNARY_TPM_LIMIT", QUATERNARY_DEFAULTS["tpm_limit"]),
    "safety_factor": _safe_float("QUATERNARY_SAFETY_FACTOR", QUATERNARY_DEFAULTS["safety_factor"]),
    "requests_per_5h": _safe_int("QUATERNARY_REQUESTS_PER_5H", QUATERNARY_DEFAULTS["requests_per_5h"]),
    "requests_per_week": _safe_int("QUATERNARY_REQUESTS_PER_WEEK", QUATERNARY_DEFAULTS["requests_per_week"]),
    "requests_per_month": _safe_int("QUATERNARY_REQUESTS_PER_MONTH", QUATERNARY_DEFAULTS["requests_per_month"]),
    "max_queue_size": _safe_int("QUATERNARY_MAX_QUEUE_SIZE", QUATERNARY_DEFAULTS["max_queue_size"]),
    "max_retries": _safe_int("QUATERNARY_MAX_RETRIES", QUATERNARY_DEFAULTS["max_retries"]),
    "base_backoff": _safe_float("QUATERNARY_BASE_BACKOFF", QUATERNARY_DEFAULTS["base_backoff"]),
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
# Quaternary provider models (ARK / BytePlus)
# ---------------------------------------------------------------------------
QUATERNARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "dola-seed-2.0-pro", "object": "model"},
        {"id": "dola-seed-2.0-lite", "object": "model"},
        {"id": "dola-seed-2.0-code", "object": "model"},
        {"id": "bytedance-seed-code", "object": "model"},
        {"id": "glm-5.2", "object": "model"},
        {"id": "glm-5.1", "object": "model"},
        {"id": "ark-code-latest", "object": "model"},
    ]
}

# ---------------------------------------------------------------------------
# Explicit model-to-provider mapping (optional overrides)
# Keys are model names, values are "primary", "secondary", "tertiary", or "quaternary".
# When a model is listed here, this mapping takes priority over
# the model list lookups for routing decisions.
# ---------------------------------------------------------------------------
MODEL_PROVIDER_MAP: dict[str, str] = {}
