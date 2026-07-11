# dashscope_proxy_lib

## Purpose

Core proxy implementation: rate limiting, request queuing, HTTP handlers, provider routing, request transformation, token utilities, session logging, and logging infrastructure. All proxy logic lives here; the root `dashscope_proxy.py` is a thin facade that re-exports everything for backward compatibility.

## Ownership

This directory owns all proxy core logic. The root `dashscope_proxy.py` facade re-exports symbols so that `import dashscope_proxy` continues to work for tests, TUI, and external callers.

## Local Contracts

- **Facade pattern**: `dashscope_proxy.py` re-exports all public symbols from this lib. Tests patch the facade (e.g., `dashscope_proxy.TARGET_BASE`), so constants must resolve at runtime via `_cfg()` helpers, not at import time.
- **Module structure**:
  - `config.py` — configuration constants, environment variables, rate limit defaults, mock models
  - `rate_limiter.py` — `SlidingWindowCounter`, `TokenWindowCounter`, `RateLimiter`, `MultiProviderRateLimiter`
  - `queue.py` — `wait_for_slot()` deadline-bounded queue wait with client disconnect detection
  - `handlers.py` — `handle_request()` main request handler with retry logic, streaming, session logging
  - `provider_router.py` — `ProviderRouter` routes requests to primary/secondary/tertiary providers based on model name
  - `request_transform.py` — `map_developer_to_system()`, `normalize_model_name()`, endpoint detection
  - `token_utils.py` — token extraction from responses/streams, request token estimation
  - `http_helpers.py` — HTTP utilities: header stripping, error responses, backoff computation, disconnect detection
  - `session_log.py` — `SessionLogWriter` appends JSON-line entries to daily-rotating files in `session_logs/`
  - `logging_config.py` — `StructuredLogFormatter`, `TUILogHandler` (thread-safe deque for TUI), `_log()` helper
  - `server.py` — `create_app()`, resource lifecycle, `main()` entry point

- **Rate limiting architecture**:
  - `SlidingWindowCounter` — bounded-memory event counter for RPS/RPM
  - `TokenWindowCounter` — token bucket with reserve/reconcile/refund for TPM
  - `RateLimiter` — combines sliding window + token bucket + quota windows (5h/week/month) + circuit breaker
  - `MultiProviderRateLimiter` — wraps multiple `RateLimiter` instances (one per provider) with global queue tracking

- **Provider routing**: model name determines provider. Explicit mapping in `MODEL_PROVIDER_MAP` takes priority; otherwise model lists in `SECONDARY_MODELS` / `TERTIARY_MODELS` determine routing. MIMO v2.5 hyphen aliases (`mimo-v2-5-*`) normalized to dots (`mimo-v2.5-*`).

- **Session logging**: writes to `session_logs/YYYY-MM-DD.jsonl` with daily rotation. Uses thread pool executor for async I/O. Each entry is a JSON line with request/response metadata.

- **Test patching convention**: tests patch `dashscope_proxy.CONSTANT` (facade), so modules must resolve constants via `_cfg("CONSTANT")` which reads from the facade at runtime. Direct imports like `from dashscope_proxy_lib.config import CONSTANT` break test patching.

## Work Guidance

- All proxy logic belongs here; do not add proxy logic to root-level files
- When adding a new constant, add it to `config.py` and resolve via `_cfg()` in handlers/helpers
- When adding a new provider, update `config.py` (models, rate limits), `provider_router.py` (routing logic), and `server.py` (resource initialization)
- Session log format: JSON lines with `request_id`, `timestamp`, `model`, `is_stream`, `request_body`, `response_status`, `response_body`, `tokens`, `latency`, `provider`
- Logging: use `_log(level, msg, **extra)` for structured logs with context; TUI consumes via `TUILogHandler`

## Verification

- `py -m pytest tests/` — full test suite
- Integration tests in `tests/test_integration.py` exercise the handler with mock upstream servers
- Unit tests in `tests/test_units.py` cover rate limiter, token utils, request transform, HTTP helpers

## Child DOX Index

No child directories.
