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
  - `provider_router.py` — `ProviderRouter` routes requests to one of six providers (primary/secondary/tertiary/quaternary/quinary/senary) based on model name
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

- **Provider routing**: model name determines provider. A `<provider>/<model>` pin (`openlux/…`, `mimo/…`, `ark/…`, `metaspark/…`, `deepseek/…`, `dashscope/…`, or canonical names) forces that provider when configured; unknown pins or pins to unconfigured providers are rejected with HTTP 400 before queue/TPM, and the prefix is stripped before upstream. Bare names: explicit mapping in `MODEL_PROVIDER_MAP` takes priority; otherwise model lists determine routing, checked in order: senary → quinary → quaternary → tertiary → secondary → primary (default). Six providers: primary (DashScope), secondary (MIMO), tertiary (OpenLux), quaternary (ARK/BytePlus), quinary (Meta AI/Muse Spark), senary (DeepSeek). MIMO v2.5 hyphen aliases (`mimo-v2-5-*`) normalized to dots (`mimo-v2.5-*`). Overlapping models (present in multiple provider lists) support cross-provider failover on 429/5xx/timeout: after per-provider retries are exhausted, the handler advances to the next available provider with a closed circuit. TPM is refunded to the old limiter before failover and reserved from the new limiter after. Session entries include `attempted_providers: list[str]` tracking the failover chain. Upstream 4xx (non-429) is terminal and does not trigger failover. `/v1/models` returns deduped model IDs with `providers` and `provider_models` fields indicating which providers serve each model; overlapping models list all available providers. `GET /v1/proxy/status` includes `model_overlaps: {model_id: [provider names]}` for models served by 2+ providers.

- **URL construction**: base URLs must include an explicit version suffix (e.g., `/v1`, `/v3`) if the upstream requires it. The proxy detects version suffixes in base URLs and uses them; if no version is present, no `/vN` prefix is added to the path. This supports APIs like DeepSeek (`https://api.deepseek.com`) which use unversioned paths.

- **Session logging**: writes to `session_logs/YYYY-MM-DD.jsonl` with daily rotation. Uses thread pool executor for async I/O. Each entry is a JSON line with request/response metadata.

- **Test patching convention**: tests patch `dashscope_proxy.CONSTANT` (facade), so modules must resolve constants via `_cfg("CONSTANT")` which reads from the facade at runtime. Direct imports like `from dashscope_proxy_lib.config import CONSTANT` break test patching.

- **Provider naming convention**: When adding a new provider, assign a **provider slug** — a short, lowercase, hyphen-free identifier used in the `<provider>/<model>` pin syntax (e.g., `openlux`, `deepseek`). Slugs must be added to `PROVIDER_SLUGS` in `config.py` (slug → canonical provider name like `tertiary`) and `PROVIDER_SLUG_MAP` in `request_transform.py` (slug + canonical name → canonical name for both). Clients use `<slug>/<model>` to pin a provider; the slug is stripped before forwarding upstream. Avoid ambiguous names; prefer provider-specific identifiers over generic terms.

## Work Guidance

- All proxy logic belongs here; do not add proxy logic to root-level files
- When adding a new constant, add it to `config.py` and resolve via `_cfg()` in handlers/helpers
- When adding a new provider:
  1. Define a **provider slug** (short, lowercase, no hyphens) in `config.py` → `PROVIDER_SLUGS` dict
  2. Add slug + canonical name to `request_transform.py` → `PROVIDER_SLUG_MAP` dict
  3. Update `config.py`: API key env var, base URL env var, rate limit config, model list constant
  4. Update `provider_router.py`: build function for model IDs, cfg helper, `ProviderConfig` in `__init__`, routing priority in `get_provider_for_model`
  5. Update `server.py`: import new rate limit config, pass to `MultiProviderRateLimiter` constructor
  6. Update `proxy_tui.py`: add UI section for the new provider's metrics
  7. Update `dashscope_proxy.py`: re-export new config constants
  8. Update `tests/`: add unit tests for routing, integration tests for forwarding
  9. Update `.env.example`: document new env vars with the provider slug
- When adding a new model to an existing provider:
  1. Add model ID to the appropriate model list constant in `config.py` (e.g., `TERTIARY_MODELS`)
  2. Model will automatically appear in `/v1/models` with `providers` and `provider_models` fields
  3. No slug changes needed — use the existing provider slug in `provider_models` field
- Session log format: JSON lines with `request_id`, `timestamp`, `model`, `is_stream`, `request_body`, `response_status`, `response_body`, `tokens`, `latency`, `provider`
- Logging: use `_log(level, msg, **extra)` for structured logs with context; TUI consumes via `TUILogHandler`

## Verification

- `py -m pytest tests/` — full test suite
- Integration tests in `tests/test_integration.py` exercise the handler with mock upstream servers
- Unit tests in `tests/test_units.py` cover rate limiter, token utils, request transform, HTTP helpers

## Child DOX Index

No child directories.
