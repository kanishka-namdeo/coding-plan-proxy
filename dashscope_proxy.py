# DashScope API Proxy — Backward-compatible facade.
#
# All implementation lives in dashscope_proxy_lib/.
# This module re-exports everything so that `import dashscope_proxy`
# continues to work unchanged for tests, TUI, and external callers.
#
# When importlib.reload(dashscope_proxy) is called (as done in tests),
# we cascade-reload all sub-modules so that environment-variable overrides
# (e.g. DASHSCOPE_API_KEY) take effect.

import sys

# --- stdlib re-exports (tests reference these directly) ----------------------
import random
from datetime import datetime, timezone

# --- reload sub-modules when this facade is reloaded -------------------------
_LIB_MODULES = [
    "dashscope_proxy_lib.config",
    "dashscope_proxy_lib.logging_config",
    "dashscope_proxy_lib.session_log",
    "dashscope_proxy_lib.rate_limiter",
    "dashscope_proxy_lib.token_utils",
    "dashscope_proxy_lib.request_transform",
    "dashscope_proxy_lib.http_helpers",
    "dashscope_proxy_lib.queue",
    "dashscope_proxy_lib.handlers",
    "dashscope_proxy_lib.server",
]

for _mod_name in _LIB_MODULES:
    _existing = sys.modules.get(_mod_name)
    if _existing is not None:
        import importlib as _il
        _il.reload(_existing)

# --- configuration -----------------------------------------------------------
from dashscope_proxy_lib.config import (
    PROXY_HOST,
    PROXY_PORT,
    TARGET_BASE,
    DASHSCOPE_API_KEY,
    UPSTREAM_TIMEOUT_TOTAL,
    UPSTREAM_TIMEOUT_CONNECT,
    MAX_CONNECTIONS,
    MAX_CONNECTIONS_PER_HOST,
    MAX_BODY_SIZE,
    MAX_STREAM_BUFFER,
    MAX_5XX_RETRIES,
    DEQUE_MAX_SIZE,
    HOP_BY_HOP_HEADERS,
    CODING_PLAN_CONFIG,
    MOCK_MODELS,
    _load_config,
)

# --- logging -----------------------------------------------------------------
from dashscope_proxy_lib.logging_config import (
    LOG_LEVEL,
    LOG_BUFFER_SIZE,
    StructuredLogFormatter,
    TUILogHandler,
    _log,
    logger,
    tui_handler,
)

# --- session log -------------------------------------------------------------
from dashscope_proxy_lib.session_log import (
    SESSION_LOG_DIR,
    SESSION_LOG_ENABLED,
    SessionLogWriter,
)

# --- rate limiter ------------------------------------------------------------
from dashscope_proxy_lib.rate_limiter import (
    SlidingWindowCounter,
    TokenWindowCounter,
    ModelStats,
    RateLimiter,
)

# --- token utils -------------------------------------------------------------
from dashscope_proxy_lib.token_utils import (
    extract_tokens_from_response,
    extract_tokens_from_stream,
    estimate_tokens_for_request,
)

# --- request transform -------------------------------------------------------
from dashscope_proxy_lib.request_transform import (
    map_developer_to_system,
    _is_chat_endpoint,
)

# --- http helpers ------------------------------------------------------------
from dashscope_proxy_lib.http_helpers import (
    parse_retry_after,
    _make_error_response,
    _add_ratelimit_headers,
    _strip_hop_by_hop,
    _client_disconnected,
    _compute_backoff,
    _add_forwarded_headers,
)

# --- queue -------------------------------------------------------------------
from dashscope_proxy_lib.queue import (
    wait_for_slot,
)

# --- handlers ----------------------------------------------------------------
from dashscope_proxy_lib.handlers import (
    handle_request,
    _maybe_flush_session_log,
)

# --- server ------------------------------------------------------------------
from dashscope_proxy_lib.server import (
    create_app,
    create_proxy_resources,
    cleanup_proxy_resources,
    main,
)

__all__ = [
    "PROXY_HOST", "PROXY_PORT", "TARGET_BASE", "DASHSCOPE_API_KEY",
    "UPSTREAM_TIMEOUT_TOTAL", "UPSTREAM_TIMEOUT_CONNECT", "MAX_CONNECTIONS",
    "MAX_CONNECTIONS_PER_HOST", "MAX_BODY_SIZE", "MAX_STREAM_BUFFER",
    "MAX_5XX_RETRIES", "DEQUE_MAX_SIZE", "HOP_BY_HOP_HEADERS",
    "CODING_PLAN_CONFIG", "MOCK_MODELS", "_load_config",
    # logging
    "LOG_LEVEL", "LOG_BUFFER_SIZE",
    "StructuredLogFormatter", "TUILogHandler", "_log", "logger", "tui_handler",
    # session log
    "SESSION_LOG_DIR", "SESSION_LOG_ENABLED", "SessionLogWriter",
    # rate limiter
    "SlidingWindowCounter", "TokenWindowCounter", "ModelStats", "RateLimiter",
    # token utils
    "extract_tokens_from_response", "extract_tokens_from_stream",
    "estimate_tokens_for_request",
    # request transform
    "map_developer_to_system", "_is_chat_endpoint",
    # http helpers
    "parse_retry_after", "_make_error_response", "_add_ratelimit_headers",
    "_strip_hop_by_hop", "_client_disconnected", "_compute_backoff",
    "_add_forwarded_headers",
    # queue
    "wait_for_slot",
    # handlers
    "handle_request", "_maybe_flush_session_log",
    # server
    "create_app", "create_proxy_resources", "cleanup_proxy_resources", "main",
]

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="DashScope API Proxy")
    parser.add_argument("--headless", action="store_true", help="Run without TUI")
    args = parser.parse_args()
    import asyncio
    asyncio.run(main(headless=args.headless))
