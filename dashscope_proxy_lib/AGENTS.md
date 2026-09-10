# dashscope_proxy_lib

## Purpose

Core proxy implementation: rate limiting, request queuing, HTTP handlers, provider routing, request transformation, token utilities, session logging, and logging infrastructure. All proxy logic lives here; the root `dashscope_proxy.py` is a thin facade that re-exports everything for backward compatibility.

## Ownership

This directory owns all proxy core logic. The root `dashscope_proxy.py` facade re-exports symbols so that `import dashscope_proxy` continues to work for tests, TUI, and external callers.

## Local Contracts

- **Facade pattern**: `dashscope_proxy.py` re-exports all public symbols from this lib. Tests patch the facade (e.g., `dashscope_proxy.TARGET_BASE`), so constants must resolve at runtime via `_cfg()` helpers, not at import time.
- **Module structure**:
  - `config.py` — configuration constants, environment variables, rate limit defaults, mock models; `_load_config()` builds the primary rate-limiter dict; `_load_display_config()` returns TUI Config rows and does not affect limiter construction; includes `MODEL_PROVIDER_MAP` (explicit model-to-provider mapping), `MODEL_FALLBACK_ORDER` (cross-provider failover order), `PROVIDER_SLUGS` (slug → canonical provider name); `STREAM_CHUNK_IDLE_TIMEOUT` / `STREAM_TAIL_BUFFER_SIZE` for streaming
  - `rate_limiter.py` — `SlidingWindowCounter`, `TokenWindowCounter`, `RateLimiter`, `MultiProviderRateLimiter`; uses `round()` for RPS derivation; `_compute_percentile()` for all percentile calculations; **lock order rule: never hold `asyncio.Lock` and `_thread_lock` at the same time** — TPM/RPM/`last_request_time` under `asyncio.Lock`, then release and publish TUI-visible counters (`total_forwarded`, tokens, circuit fields, `model_usage`, `recent_latencies`, body sizes) under `_thread_lock`; `status()` takes `_thread_lock` alone (percentiles outside lock); 3-state circuit breaker (CLOSED→OPEN→HALF_OPEN) with `can_attempt_probe()` claiming a single `circuit_probe_in_flight` after cooldown (success → CLOSED; failed probe → OPEN + cooldown via `record_circuit_failure`; aborted probe → `release_probe()` clears in-flight and reopens); `circuit_is_open()` remains lock-free for status/failover skip; per-model usage tracking with `ModelStats` dataclass and LRU eviction (`model_usage_max = 100`); `recent_latencies` list for percentile computation; `try_admit()` coalesces can_proceed checks + TPM reserve under one asyncio lock and stamps `last_request_time` on successful admit (admission-time RPS shaping; does not charge RPM); `record_completion()` / `record_request()` may refresh `last_request_time` under asyncio lock then publish metrics under `_thread_lock`; legacy `can_proceed` / `reserve_tokens` / `reconcile_tokens` / `record_*` kept for compatibility and failure/failover paths
  - `queue.py` — `wait_for_slot()` deadline-bounded queue wait with client disconnect detection
  - `handlers.py` — `handle_request()` main request handler with retry logic, streaming, session logging; `_build_target_url()` constructs upstream URLs from provider base + path + query string; tracks `tokens_reserved` flag for correct TPM refund accounting; ingress path transforms + pin-strips the parsed body, then dumps JSON once and estimates tokens via `estimate_tokens_for_body`; post-queue admit uses `try_admit` (wait loop still uses `can_proceed`); pre-upstream gate uses `can_attempt_probe()` (not lock-free `circuit_is_open()` alone) so only one HALF_OPEN probe runs after cooldown; probe holder tracked via `holding_probe` so retries after claim do not self-deny; `release_probe()` on abort/`finally` and before failover clears stuck `circuit_probe_in_flight`; stream/non-stream success paths use `record_completion`; failure/failover keep `refund_tokens` + `reserve_tokens` + `record_circuit_failure`; stream loop caches `chunk_idle_timeout` / `tail_max` via `_cfg("STREAM_CHUNK_IDLE_TIMEOUT")` / `_cfg("STREAM_TAIL_BUFFER_SIZE")` once before the chunk loop
  - `provider_router.py` — `ProviderRouter` routes requests to one of seven providers (primary/secondary/tertiary/quaternary/quinary/senary/septenary) based on model name; overlap registry gates primary with `is_available`; `get_limiter_for_provider()` logs warning on fallback to primary
  - `request_transform.py` — `map_developer_to_system()`, `normalize_model_name()`, `split_provider_prefix()`, endpoint detection; `normalize_model_name()` checks exact match or hyphen prefix to avoid false matches (e.g. `mimo-v2-50`); `split_provider_prefix()` always normalizes returned model; `PROVIDER_SLUG_MAP` maps slug + canonical name → canonical name
  - `token_utils.py` — token extraction from responses/streams, request token estimation; `extract_tokens_from_stream()` skips malformed SSE lines; `estimate_tokens_for_body(body: dict)` is the core TPM estimate (messages, list content text parts, `system`/`developer`, tools via `json.dumps`); `estimate_tokens_for_request(body_bytes)` is a thin wrapper that loads JSON then calls `estimate_tokens_for_body` (returns 100 on bad JSON); `_safe_int()` guards against non-integer usage values from upstream
  - `http_helpers.py` — HTTP utilities: header stripping, error responses, backoff computation, disconnect detection; includes `_sleep_interruptible()`, `_sse_response_headers()`, `_finalize_stream_response()`, `_upstream_error_response()`, `SECURITY_HEADERS_TO_STRIP` constant
  - `session_log.py` — `SessionLogWriter` appends JSON-line entries to daily-rotating files in `session_logs/`; default `log_async` uses bounded `queue.Queue` + one background drain thread (`put_nowait`, drop+ERROR on `queue.Full`); batched flush every `SESSION_LOG_FLUSH_EVERY` lines / `SESSION_LOG_FLUSH_INTERVAL` seconds; `close()` stops acceptor under `_accept_lock` (serialized with enqueue), enqueues sentinel (on Full, pulls items aside so drain still happens), joins writer, then post-join writes leftovers under lock before final flush/close; `SESSION_LOG_SYNC_FLUSH=1` restores await+flush-every-line via ThreadPoolExecutor
  - `logging_config.py` — `StructuredLogFormatter` (uses `default=str` for non-serializable values), `TUILogHandler` (thread-safe deque for TUI), `_log()` helper, `configure_logging()` sets up logger; `logger` defined before `_log()`
  - `server.py` — `create_app()`, resource lifecycle, `main()` entry point; uses `asyncio.get_running_loop()`; `session.close()` wrapped in `asyncio.timeout(5)`; Windows signal handler guarded with `hasattr(signal, 'SIGTERM')`; PID file management with `pathlib.Path` (`PID_FILE`, `_write_pid_file()`, `_remove_pid_file()`, `_check_stale_pid_file()`)

- **Rate limiting architecture**:
  - `SlidingWindowCounter` — bounded-memory event counter for RPS/RPM
  - `TokenWindowCounter` — token bucket with reserve/reconcile/refund for TPM
  - `RateLimiter` — combines sliding window + token bucket + 3-state circuit breaker (CLOSED→OPEN→HALF_OPEN; `can_attempt_probe()` admits one probe after cooldown); per-model usage tracking via `ModelStats` dataclass with LRU eviction (`model_usage_max = 100`); `recent_latencies` list for percentile computation; happy-path admit via `try_admit`, completion via `record_completion`
  - `MultiProviderRateLimiter` — wraps multiple `RateLimiter` instances (one per provider) with global queue tracking

- **Provider routing**: model name determines provider. A `<provider>/<model>` pin (`openlux/…`, `mimo/…`, `ark/…`, `metaspark/…`, `deepseek/…`, `glm/…`, `dashscope/…`, or canonical names) forces that provider when configured; unknown pins or pins to unconfigured providers are rejected with HTTP 400 before queue/TPM, and the prefix is stripped before upstream. Bare names: explicit mapping in `MODEL_PROVIDER_MAP` takes priority; otherwise model lists determine routing, checked in order: septenary → senary → quinary → quaternary → tertiary → secondary → primary (default). Seven providers: primary (DashScope), secondary (MIMO), tertiary (OpenLux), quaternary (ARK/BytePlus), quinary (Meta AI/Muse Spark), senary (DeepSeek), septenary (GLM/Z.ai). MIMO v2.5 hyphen aliases (`mimo-v2-5-*`) normalized to dots (`mimo-v2.5-*`). Overlapping models (present in multiple provider lists) support cross-provider failover on 429/5xx/timeout: after per-provider retries are exhausted, the handler advances to the next available provider with a closed circuit, in `MODEL_FALLBACK_ORDER` (default: septenary→senary→quinary→quaternary→tertiary→secondary→primary). TPM is refunded to the old limiter before failover and reserved from the new limiter after. Session entries include `attempted_providers: list[str]` tracking the failover chain. Upstream 4xx (non-429) is terminal and does not trigger failover. `/v1/models` returns deduped model IDs with `providers` and `provider_models` fields indicating which providers serve each model; overlapping models list all available providers. `GET /v1/proxy/status` includes `model_overlaps: {model_id: [provider names]}` for models served by 2+ providers.

- **URL construction**: base URLs must include an explicit version suffix (e.g., `/v1`, `/v3`) if the upstream requires it. The proxy detects version suffixes in base URLs and uses them; if no version is present, no `/vN` prefix is added to the path. This supports APIs like DeepSeek (`https://api.deepseek.com`) which use unversioned paths.

- **Session logging**: writes to `session_logs/YYYY-MM-DD.jsonl` with daily rotation. Default path: non-blocking enqueue onto a bounded queue; one background thread drains and flushes in batches (`SESSION_LOG_FLUSH_EVERY` / `SESSION_LOG_FLUSH_INTERVAL`, defaults 32 / 0.25s). Full entry dicts are preserved (no field dropping). On queue full, log ERROR and drop — never block the event loop. `close()` serializes stop-accept with enqueue via `_accept_lock`, enqueues a sentinel (on Full, pulls entries aside so the writer still stops), joins the writer, then writes any aside/leftover entries under lock before final flush. `SESSION_LOG_SYNC_FLUSH=1` restores per-request `run_in_executor` + flush-every-line for emergency debugging. Each entry is a JSON line with request/response metadata.

- **Test patching convention**: tests patch `dashscope_proxy.CONSTANT` (facade), so modules must resolve constants via `_cfg("CONSTANT")` which reads from the facade at runtime. Direct imports like `from dashscope_proxy_lib.config import CONSTANT` break test patching.

- **Provider naming convention**: When adding a new provider, assign a **provider slug** — a short, lowercase, hyphen-free identifier used in the `<provider>/<model>` pin syntax (e.g., `openlux`, `deepseek`, `glm`). Slugs must be added to `PROVIDER_SLUGS` in `config.py` (slug → canonical provider name like `tertiary`) and `PROVIDER_SLUG_MAP` in `request_transform.py` (slug + canonical name → canonical name for both). Clients use `<slug>/<model>` to pin a provider; the slug is stripped before forwarding upstream. Avoid ambiguous names; prefer provider-specific identifiers over generic terms.

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
