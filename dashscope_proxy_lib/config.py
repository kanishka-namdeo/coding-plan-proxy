"""Configuration, constants, and environment setup for the DashScope proxy."""

import math
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
    """Read an env var as float, falling back to default on parse error or
    non-finite values (``nan``/``inf`` parse cleanly via ``float()`` but would
    poison downstream math such as ``int(rpm_limit * safety_factor)``)."""
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return default
    if not math.isfinite(value):
        return default
    return value

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_BUFFER_SIZE = max(1, _safe_int("LOG_BUFFER_SIZE", 2000))  # floor: zero-capacity deques/Log widgets are unusable

# ---------------------------------------------------------------------------
# Network configuration
# ---------------------------------------------------------------------------
PROXY_HOST = os.environ.get("DASHSCOPE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = _safe_int("DASHSCOPE_PROXY_PORT", 8899)
TARGET_BASE = os.environ.get("TARGET_BASE", "https://coding-intl.dashscope.aliyuncs.com/v1")

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
# Tertiary provider configuration (optional - OpenLux)
# Only used if both KEY and BASE_URL are set
# ---------------------------------------------------------------------------
TERTIARY_API_KEY = os.environ.get("OPENLUX_API_KEY", "").strip()
TERTIARY_BASE_URL = os.environ.get("OPENLUX_TARGET_BASE", "https://api.openlux.ai/v1").strip()

# ---------------------------------------------------------------------------
# Quaternary provider configuration (optional - ARK / BytePlus)
# Only used if both KEY and BASE_URL are set
# ---------------------------------------------------------------------------
QUATERNARY_API_KEY = os.environ.get("MODEL_ARK_API_KEY", "").strip()
QUATERNARY_BASE_URL = os.environ.get("MODEL_ARK_TARGET_BASE", "").strip()

# ---------------------------------------------------------------------------
# Quinary provider configuration (optional - Meta AI / Muse Spark)
# Only used if both KEY and BASE_URL are set
# ---------------------------------------------------------------------------
QUINARY_API_KEY = os.environ.get("META_AI_API_KEY", "").strip()
QUINARY_BASE_URL = os.environ.get("META_AI_TARGET_BASE", "https://api.meta.ai/v1").strip()

# ---------------------------------------------------------------------------
# Senary provider configuration (optional - DeepSeek)
# Only used if both KEY and BASE_URL are set
# ---------------------------------------------------------------------------
SENARY_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
SENARY_BASE_URL = os.environ.get("DEEPSEEK_TARGET_BASE", "https://api.deepseek.com").strip()

# ---------------------------------------------------------------------------
# Septenary provider configuration (optional - GLM / Z.ai)
# Only used if both KEY and BASE_URL are set
# ---------------------------------------------------------------------------
SEPTENARY_API_KEY = os.environ.get("GLM_API_KEY", "").strip()
SEPTENARY_BASE_URL = os.environ.get("GLM_TARGET_BASE", "https://api.z.ai/api/paas/v4").strip()

# ---------------------------------------------------------------------------
# Octonary/Nonary/Decenary provider configuration (optional - Agnes AI)
# Only used if both KEY and BASE_URL are set. A single Agnes Token Plan key
# serves all three modalities (text/image/video), so the key and base URL are
# aliased to three ordinals and the router/limiter/server can treat them
# uniformly.
# ---------------------------------------------------------------------------
AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "").strip()
AGNES_BASE_URL = os.environ.get("AGNES_TARGET_BASE", "https://apihub.agnes-ai.com/v1").strip()

OCTONARY_API_KEY = AGNES_API_KEY
OCTONARY_BASE_URL = AGNES_BASE_URL

NONARY_API_KEY = AGNES_API_KEY
NONARY_BASE_URL = AGNES_BASE_URL

DECENARY_API_KEY = AGNES_API_KEY
DECENARY_BASE_URL = AGNES_BASE_URL

# ---------------------------------------------------------------------------
# Timeout and connection limits
# ---------------------------------------------------------------------------
UPSTREAM_TIMEOUT_TOTAL = _safe_int("UPSTREAM_TIMEOUT_TOTAL", 300)
UPSTREAM_TIMEOUT_CONNECT = _safe_int("UPSTREAM_TIMEOUT_CONNECT", 10)
MAX_CONNECTIONS = _safe_int("MAX_CONNECTIONS", 200)
MAX_CONNECTIONS_PER_HOST = _safe_int("MAX_CONNECTIONS_PER_HOST", 50)
MAX_BODY_SIZE = _safe_int("MAX_BODY_SIZE", 50 * 1024 * 1024)
MAX_STREAM_BUFFER = _safe_int("MAX_STREAM_BUFFER", 50 * 1024 * 1024)  # 50 MB cap for streaming response buffer
UPSTREAM_MAX_BODY_SIZE = _safe_int("UPSTREAM_MAX_BODY_SIZE", 50 * 1024 * 1024)  # cap for a single upstream response/error body read into memory
MAX_5XX_RETRIES = _safe_int("MAX_5XX_RETRIES", 3)
DEQUE_MAX_SIZE = 100_000
STREAM_CHUNK_IDLE_TIMEOUT = 60  # seconds between chunks before stream is considered stalled
STREAM_TAIL_BUFFER_SIZE = 8192  # enough for the final SSE data line

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
    "rpm_limit": 9,
    "tpm_limit": 4_000_000,
    "safety_factor": 0.8,
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
    "max_queue_size": _safe_int("SECONDARY_MAX_QUEUE_SIZE", CODING_PLAN_CONFIG["max_queue_size"]),
    "max_retries": _safe_int("SECONDARY_MAX_RETRIES", CODING_PLAN_CONFIG["max_retries"]),
    "base_backoff": _safe_float("SECONDARY_BASE_BACKOFF", CODING_PLAN_CONFIG["base_backoff"]),
}

# ---------------------------------------------------------------------------
# Tertiary provider rate limits (OpenLux - independent defaults)
# ---------------------------------------------------------------------------
TERTIARY_DEFAULTS = {
    "rpm_limit": 40,
    "tpm_limit": 6_000_000,
    "safety_factor": 0.8,
    "max_queue_size": 200,
    "max_retries": 20,
    "base_backoff": 1.0,
}

TERTIARY_CODING_PLAN_CONFIG = {
    "rpm_limit": _safe_int("TERTIARY_RPM_LIMIT", TERTIARY_DEFAULTS["rpm_limit"]),
    "tpm_limit": _safe_int("TERTIARY_TPM_LIMIT", TERTIARY_DEFAULTS["tpm_limit"]),
    "safety_factor": _safe_float("TERTIARY_SAFETY_FACTOR", TERTIARY_DEFAULTS["safety_factor"]),
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
    "max_queue_size": 200,
    "max_retries": 20,
    "base_backoff": 1.0,
}

QUATERNARY_CODING_PLAN_CONFIG = {
    "rpm_limit": _safe_int("QUATERNARY_RPM_LIMIT", QUATERNARY_DEFAULTS["rpm_limit"]),
    "tpm_limit": _safe_int("QUATERNARY_TPM_LIMIT", QUATERNARY_DEFAULTS["tpm_limit"]),
    "safety_factor": _safe_float("QUATERNARY_SAFETY_FACTOR", QUATERNARY_DEFAULTS["safety_factor"]),
    "max_queue_size": _safe_int("QUATERNARY_MAX_QUEUE_SIZE", QUATERNARY_DEFAULTS["max_queue_size"]),
    "max_retries": _safe_int("QUATERNARY_MAX_RETRIES", QUATERNARY_DEFAULTS["max_retries"]),
    "base_backoff": _safe_float("QUATERNARY_BASE_BACKOFF", QUATERNARY_DEFAULTS["base_backoff"]),
}

# ---------------------------------------------------------------------------
# Quinary provider rate limits (Meta AI - independent defaults)
# ---------------------------------------------------------------------------
QUINARY_DEFAULTS = {
    "rpm_limit": 3000,
    "tpm_limit": 4_000_000,
    "safety_factor": 0.8,
    "max_queue_size": 500,
    "max_retries": 20,
    "base_backoff": 0.5,
}

QUINARY_CODING_PLAN_CONFIG = {
    "rpm_limit": _safe_int("QUINARY_RPM_LIMIT", QUINARY_DEFAULTS["rpm_limit"]),
    "tpm_limit": _safe_int("QUINARY_TPM_LIMIT", QUINARY_DEFAULTS["tpm_limit"]),
    "safety_factor": _safe_float("QUINARY_SAFETY_FACTOR", QUINARY_DEFAULTS["safety_factor"]),
    "max_queue_size": _safe_int("QUINARY_MAX_QUEUE_SIZE", QUINARY_DEFAULTS["max_queue_size"]),
    "max_retries": _safe_int("QUINARY_MAX_RETRIES", QUINARY_DEFAULTS["max_retries"]),
    "base_backoff": _safe_float("QUINARY_BASE_BACKOFF", QUINARY_DEFAULTS["base_backoff"]),
}

# ---------------------------------------------------------------------------
# Senary provider rate limits (DeepSeek - independent defaults)
# ---------------------------------------------------------------------------
SENARY_DEFAULTS = {
    "rpm_limit": 60,
    "tpm_limit": 4_000_000,
    "safety_factor": 0.8,
    "max_queue_size": 500,
    "max_retries": 20,
    "base_backoff": 0.5,
}

SENARY_CODING_PLAN_CONFIG = {
    "rpm_limit": _safe_int("SENARY_RPM_LIMIT", SENARY_DEFAULTS["rpm_limit"]),
    "tpm_limit": _safe_int("SENARY_TPM_LIMIT", SENARY_DEFAULTS["tpm_limit"]),
    "safety_factor": _safe_float("SENARY_SAFETY_FACTOR", SENARY_DEFAULTS["safety_factor"]),
    "max_queue_size": _safe_int("SENARY_MAX_QUEUE_SIZE", SENARY_DEFAULTS["max_queue_size"]),
    "max_retries": _safe_int("SENARY_MAX_RETRIES", SENARY_DEFAULTS["max_retries"]),
    "base_backoff": _safe_float("SENARY_BASE_BACKOFF", SENARY_DEFAULTS["base_backoff"]),
}

# ---------------------------------------------------------------------------
# Septenary provider rate limits (GLM - independent defaults)
# ---------------------------------------------------------------------------
SEPTENARY_DEFAULTS = {
    "rpm_limit": 60,
    "tpm_limit": 4_000_000,
    "safety_factor": 0.8,
    "max_queue_size": 500,
    "max_retries": 20,
    "base_backoff": 0.5,
}

SEPTENARY_CODING_PLAN_CONFIG = {
    "rpm_limit": _safe_int("SEPTENARY_RPM_LIMIT", SEPTENARY_DEFAULTS["rpm_limit"]),
    "tpm_limit": _safe_int("SEPTENARY_TPM_LIMIT", SEPTENARY_DEFAULTS["tpm_limit"]),
    "safety_factor": _safe_float("SEPTENARY_SAFETY_FACTOR", SEPTENARY_DEFAULTS["safety_factor"]),
    "max_queue_size": _safe_int("SEPTENARY_MAX_QUEUE_SIZE", SEPTENARY_DEFAULTS["max_queue_size"]),
    "max_retries": _safe_int("SEPTENARY_MAX_RETRIES", SEPTENARY_DEFAULTS["max_retries"]),
    "base_backoff": _safe_float("SEPTENARY_BASE_BACKOFF", SEPTENARY_DEFAULTS["base_backoff"]),
}

# ---------------------------------------------------------------------------
# Octonary provider rate limits (Agnes Text - independent defaults)
# ---------------------------------------------------------------------------
OCTONARY_DEFAULTS = {
    "rpm_limit": 1000,
    "tpm_limit": 4_000_000,
    "safety_factor": 0.8,
    "max_queue_size": 500,
    "max_retries": 40,
    "base_backoff": 1.0,
}

OCTONARY_CODING_PLAN_CONFIG = {
    "rpm_limit": _safe_int("OCTONARY_RPM_LIMIT", OCTONARY_DEFAULTS["rpm_limit"]),
    "tpm_limit": _safe_int("OCTONARY_TPM_LIMIT", OCTONARY_DEFAULTS["tpm_limit"]),
    "safety_factor": _safe_float("OCTONARY_SAFETY_FACTOR", OCTONARY_DEFAULTS["safety_factor"]),
    "max_queue_size": _safe_int("OCTONARY_MAX_QUEUE_SIZE", OCTONARY_DEFAULTS["max_queue_size"]),
    "max_retries": _safe_int("OCTONARY_MAX_RETRIES", OCTONARY_DEFAULTS["max_retries"]),
    "base_backoff": _safe_float("OCTONARY_BASE_BACKOFF", OCTONARY_DEFAULTS["base_backoff"]),
}

# ---------------------------------------------------------------------------
# Nonary provider rate limits (Agnes Image - independent defaults)
# ---------------------------------------------------------------------------
NONARY_DEFAULTS = {
    "rpm_limit": 120,
    "tpm_limit": 4_000_000,
    "safety_factor": 0.8,
    "max_queue_size": 200,
    "max_retries": 20,
    "base_backoff": 1.0,
}

NONARY_CODING_PLAN_CONFIG = {
    "rpm_limit": _safe_int("NONARY_RPM_LIMIT", NONARY_DEFAULTS["rpm_limit"]),
    "tpm_limit": _safe_int("NONARY_TPM_LIMIT", NONARY_DEFAULTS["tpm_limit"]),
    "safety_factor": _safe_float("NONARY_SAFETY_FACTOR", NONARY_DEFAULTS["safety_factor"]),
    "max_queue_size": _safe_int("NONARY_MAX_QUEUE_SIZE", NONARY_DEFAULTS["max_queue_size"]),
    "max_retries": _safe_int("NONARY_MAX_RETRIES", NONARY_DEFAULTS["max_retries"]),
    "base_backoff": _safe_float("NONARY_BASE_BACKOFF", NONARY_DEFAULTS["base_backoff"]),
}

# ---------------------------------------------------------------------------
# Decenary provider rate limits (Agnes Video - independent defaults)
# ---------------------------------------------------------------------------
DECENARY_DEFAULTS = {
    "rpm_limit": 6,
    "tpm_limit": 4_000_000,
    "safety_factor": 0.8,
    "max_queue_size": 200,
    "max_retries": 20,
    "base_backoff": 1.0,
}

DECENARY_CODING_PLAN_CONFIG = {
    "rpm_limit": _safe_int("DECENARY_RPM_LIMIT", DECENARY_DEFAULTS["rpm_limit"]),
    "tpm_limit": _safe_int("DECENARY_TPM_LIMIT", DECENARY_DEFAULTS["tpm_limit"]),
    "safety_factor": _safe_float("DECENARY_SAFETY_FACTOR", DECENARY_DEFAULTS["safety_factor"]),
    "max_queue_size": _safe_int("DECENARY_MAX_QUEUE_SIZE", DECENARY_DEFAULTS["max_queue_size"]),
    "max_retries": _safe_int("DECENARY_MAX_RETRIES", DECENARY_DEFAULTS["max_retries"]),
    "base_backoff": _safe_float("DECENARY_BASE_BACKOFF", DECENARY_DEFAULTS["base_backoff"]),
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


def _env_source(env_names: list[str]) -> str:
    return "env" if any(n in os.environ for n in env_names) else "default"


def _load_display_config() -> list[tuple[str, str, str, str]]:
    """Rows for the TUI Config tab. Does not affect rate-limiter construction."""
    from dashscope_proxy_lib.session_log import SESSION_LOG_DIR, SESSION_LOG_ENABLED

    rows: list[tuple[str, str, str, str]] = []

    def add(group, key, value, env_names):
        rows.append((group, key, str(value), _env_source(env_names)))

    add("Network", "proxy_host", os.environ.get("DASHSCOPE_PROXY_HOST", PROXY_HOST), ["DASHSCOPE_PROXY_HOST"])
    add("Network", "proxy_port", os.environ.get("DASHSCOPE_PROXY_PORT", PROXY_PORT), ["DASHSCOPE_PROXY_PORT"])
    add("Network", "target_base", os.environ.get("TARGET_BASE", TARGET_BASE), ["TARGET_BASE"])
    add("Timeouts", "upstream_timeout_total", os.environ.get("UPSTREAM_TIMEOUT_TOTAL", UPSTREAM_TIMEOUT_TOTAL), ["UPSTREAM_TIMEOUT_TOTAL"])
    add("Timeouts", "upstream_timeout_connect", os.environ.get("UPSTREAM_TIMEOUT_CONNECT", UPSTREAM_TIMEOUT_CONNECT), ["UPSTREAM_TIMEOUT_CONNECT"])
    add("Connection", "max_connections", os.environ.get("MAX_CONNECTIONS", MAX_CONNECTIONS), ["MAX_CONNECTIONS"])
    add("Connection", "max_connections_per_host", os.environ.get("MAX_CONNECTIONS_PER_HOST", MAX_CONNECTIONS_PER_HOST), ["MAX_CONNECTIONS_PER_HOST"])
    add("Buffering", "max_body_size", os.environ.get("MAX_BODY_SIZE", MAX_BODY_SIZE), ["MAX_BODY_SIZE"])
    add("Buffering", "max_stream_buffer", os.environ.get("MAX_STREAM_BUFFER", MAX_STREAM_BUFFER), ["MAX_STREAM_BUFFER"])
    add("Buffering", "upstream_max_body_size", os.environ.get("UPSTREAM_MAX_BODY_SIZE", UPSTREAM_MAX_BODY_SIZE), ["UPSTREAM_MAX_BODY_SIZE"])
    add("Buffering", "max_5xx_retries", os.environ.get("MAX_5XX_RETRIES", MAX_5XX_RETRIES), ["MAX_5XX_RETRIES"])
    add("Logging", "log_level", os.environ.get("LOG_LEVEL", LOG_LEVEL), ["LOG_LEVEL"])
    add("Logging", "log_buffer_size", os.environ.get("LOG_BUFFER_SIZE", LOG_BUFFER_SIZE), ["LOG_BUFFER_SIZE"])
    add("Logging", "session_log_enabled", os.environ.get("SESSION_LOG_ENABLED", SESSION_LOG_ENABLED), ["SESSION_LOG_ENABLED"])
    add("Logging", "session_log_dir", os.environ.get("SESSION_LOG_DIR", SESSION_LOG_DIR), ["SESSION_LOG_DIR"])

    provider_cfgs = [
        ("Primary Limits", CODING_PLAN_CONFIG, "PROXY_", None),
        ("MIMO Limits", SECONDARY_CODING_PLAN_CONFIG, "SECONDARY_", "mimo"),
        ("OpenLux Limits", TERTIARY_CODING_PLAN_CONFIG, "TERTIARY_", "openlux"),
        ("ARK Limits", QUATERNARY_CODING_PLAN_CONFIG, "QUATERNARY_", "ark"),
        ("Meta AI Limits", QUINARY_CODING_PLAN_CONFIG, "QUINARY_", "meta"),
        ("DeepSeek Limits", SENARY_CODING_PLAN_CONFIG, "SENARY_", "deepseek"),
        ("GLM Limits", SEPTENARY_CODING_PLAN_CONFIG, "SEPTENARY_", "glm"),
        ("Agnes Text Limits", OCTONARY_CODING_PLAN_CONFIG, "OCTONARY_", "agnes_text"),
        ("Agnes Image Limits", NONARY_CODING_PLAN_CONFIG, "NONARY_", "agnes_image"),
        ("Agnes Video Limits", DECENARY_CODING_PLAN_CONFIG, "DECENARY_", "agnes_video"),
    ]
    for group, cfg, prefix, key_prefix in provider_cfgs:
        for key, value in cfg.items():
            add(group, f"{key_prefix}.{key}" if key_prefix else key,
                value, [f"{prefix}{key.upper()}"])

    add("Providers", "secondary_base_url", SECONDARY_BASE_URL or "(unset)", ["MIMO_CODING_PLAN_TARGET_BASE"])
    add("Providers", "tertiary_base_url", TERTIARY_BASE_URL or "(unset)", ["OPENLUX_TARGET_BASE"])
    add("Providers", "quaternary_base_url", QUATERNARY_BASE_URL or "(unset)", ["MODEL_ARK_TARGET_BASE"])
    add("Providers", "quinary_base_url", QUINARY_BASE_URL or "(unset)", ["META_AI_TARGET_BASE"])
    add("Providers", "senary_base_url", SENARY_BASE_URL or "(unset)", ["DEEPSEEK_TARGET_BASE"])
    add("Providers", "septenary_base_url", SEPTENARY_BASE_URL or "(unset)", ["GLM_TARGET_BASE"])
    add("Providers", "octonary_base_url", OCTONARY_BASE_URL or "(unset)", ["AGNES_TARGET_BASE"])
    add("Providers", "nonary_base_url", NONARY_BASE_URL or "(unset)", ["AGNES_TARGET_BASE"])
    add("Providers", "decenary_base_url", DECENARY_BASE_URL or "(unset)", ["AGNES_TARGET_BASE"])
    add("Providers", "model_fallback_order", ",".join(MODEL_FALLBACK_ORDER) or "(default)", ["MODEL_FALLBACK_ORDER"])
    return rows


# ---------------------------------------------------------------------------
# Mock models for /v1/models endpoint
# ---------------------------------------------------------------------------
MOCK_MODELS = {
    "object": "list",
    "data": [
        {"id": "qwen3.6-plus", "object": "model"},
        {"id": "qwen3.7-plus", "object": "model"},
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
        {"id": "mimo-v2.5-pro", "object": "model"}
    ]
}

# ---------------------------------------------------------------------------
# Tertiary provider models (OpenLux)
# ---------------------------------------------------------------------------
TERTIARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "gpt-5.6-sol", "object": "model"},
        {"id": "gemini-3.7-flash", "object": "model"},
        {"id": "gpt-5.6-terra", "object": "model"},
        {"id": "qwen3.8-max", "object": "model"},
        {"id": "qwen3.8-max-0902", "object": "model"},
        {"id": "gpt-5.6-luna", "object": "model"},
        {"id": "gemini-3.8-flash", "object": "model"},
        {"id": "grok-4.6", "object": "model"},
        {"id": "MiniMax-M3", "object": "model"},
        {"id": "mimo-v2.5", "object": "model"},
        {"id": "glm-5.3-flash", "object": "model"},
        {"id": "gpt-6-sol", "object": "model"},
        {"id": "gpt-6-astra", "object": "model"},
        {"id": "mimo-v2.6-flash", "object": "model"},
        {"id": "jev-1.13.0", "object": "model"},
    ]
}

# ---------------------------------------------------------------------------
# Quaternary provider models (ARK / BytePlus)
# ---------------------------------------------------------------------------
QUATERNARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "glm-5.2", "object": "model"},
        {"id": "glm-5.1", "object": "model"},
    ]
}

# ---------------------------------------------------------------------------
# Quinary provider models (Meta AI / Muse Spark)
# ---------------------------------------------------------------------------
QUINARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "muse-spark-1.3", "object": "model"},
        {"id": "muse-spark-1.3-contributor", "object": "model"}
    ]
}

# ---------------------------------------------------------------------------
# Senary provider models (DeepSeek)
# ---------------------------------------------------------------------------
SENARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "deepseek-flash", "object": "model"},
    ]
}

# ---------------------------------------------------------------------------
# Septenary provider models (GLM / Z.ai)
# ---------------------------------------------------------------------------
SEPTENARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "glm-5.3", "object": "model"},
        {"id": "glm-5.3-flash", "object": "model"},
        {"id": "glm-5.2", "object": "model"},
        {"id": "glm-5.1", "object": "model"},
        {"id": "glm-5-turbo", "object": "model"},
        {"id": "glm-5", "object": "model"},
        {"id": "glm-4.7", "object": "model"},
        {"id": "glm-4.6", "object": "model"},
        {"id": "glm-4.5", "object": "model"},
        {"id": "glm-4.5-air", "object": "model"},
    ]
}

# ---------------------------------------------------------------------------
# Octonary provider models (Agnes Text)
# ---------------------------------------------------------------------------
OCTONARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "agnes-3.0-flash", "object": "model"},
    ]
}

# ---------------------------------------------------------------------------
# Nonary provider models (Agnes Image)
# ---------------------------------------------------------------------------
NONARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "agnes-image-2.1-flash", "object": "model"},
    ]
}

# ---------------------------------------------------------------------------
# Decenary provider models (Agnes Video)
# ---------------------------------------------------------------------------
DECENARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "agnes-video-2.5-flash", "object": "model"},
    ]
}

# ---------------------------------------------------------------------------
# Explicit model-to-provider mapping (optional overrides)
# Keys are model names, values are "primary", "secondary", "tertiary", "quaternary", "quinary", "senary", or "septenary".
# When a model is listed here, this mapping takes priority over
# the model list lookups for routing decisions.
# ---------------------------------------------------------------------------
MODEL_PROVIDER_MAP: dict[str, str] = {}

PROVIDER_SLUGS: dict[str, str] = {
    "dashscope": "primary",
    "mimo": "secondary",
    "openlux": "tertiary",
    "ark": "quaternary",
    "metaspark": "quinary",
    "deepseek": "senary",
    "glm": "septenary",
    "zai": "septenary",
    "agnes": "octonary",
    "agnes-image": "nonary",
    "agnes-video": "decenary",
}

def _normalize_fallback_entry(entry: str) -> str:
    """Map a provider slug to its canonical provider name; pass through
    canonical names and unknown values (the router ignores unknown entries)."""
    slug = entry.strip().lower()
    return PROVIDER_SLUGS.get(slug, slug)

MODEL_FALLBACK_ORDER: list[str] = [
    _normalize_fallback_entry(s)
    for s in os.environ.get("MODEL_FALLBACK_ORDER", "").split(",")
    if s.strip()
]
