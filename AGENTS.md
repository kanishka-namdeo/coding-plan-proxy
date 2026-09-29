# DOX framework

- DOX is highly performant AGENTS.md hierarchy installed here
- Agent must follow DOX instructions across any edits

## Core Contract

- AGENTS.md files are binding work contracts for their subtrees
- Work products, source materials, instructions, records, assets, and durable docs must stay understandable from the nearest applicable AGENTS.md plus every parent AGENTS.md above it

## Read Before Editing

1. Read the root AGENTS.md
2. Identify every file or folder you expect to touch
3. Walk from the repository root to each target path
4. Read every AGENTS.md found along each route
5. If a parent AGENTS.md lists a child AGENTS.md whose scope contains the path, read that child and continue from there
6. Use the nearest AGENTS.md as the local contract and parent docs for repo-wide rules
7. If docs conflict, the closer doc controls local work details, but no child doc may weaken DOX

Do not rely on memory. Re-read the applicable DOX chain in the current session before editing.

## Update After Editing

Every meaningful change requires a DOX pass before the task is done.

Update the closest owning AGENTS.md when a change affects:

- purpose, scope, ownership, or responsibilities
- durable structure, contracts, workflows, or operating rules
- required inputs, outputs, permissions, constraints, side effects, or artifacts
- user preferences about behavior, communication, process, organization, or quality
- AGENTS.md creation, deletion, move, rename, or index contents

Update parent docs when parent-level structure, ownership, workflow, or child index changes. Update child docs when parent changes alter local rules. Remove stale or contradictory text immediately. Small edits that do not change behavior or contracts may leave docs unchanged, but the DOX pass still must happen.

## Hierarchy

- Root AGENTS.md is the DOX rail: project-wide instructions, global preferences, durable workflow rules, and the top-level Child DOX Index
- Child AGENTS.md files own domain-specific instructions and their own Child DOX Index
- Each parent explains what its direct children cover and what stays owned by the parent
- The closer a doc is to the work, the more specific and practical it must be

## Child Doc Shape

- Create a child AGENTS.md when a folder becomes a durable boundary with its own purpose, rules, responsibilities, workflow, materials, or quality standards
- Work Guidance must reflect the current standards of the project or user instructions; if there are no specific standards or instructions yet, leave it empty
- Verification must reflect an existing check; if no verification framework exists yet, leave it empty and update it when one exists

Default section order:
- Purpose
- Ownership
- Local Contracts
- Work Guidance
- Verification
- Child DOX Index

## Style

- Keep docs concise, current, and operational
- Document stable contracts, not diary entries
- Put broad rules in parent docs and concrete details in child docs
- Prefer direct bullets with explicit names
- Do not duplicate rules across many files unless each scope needs a local version
- Delete stale notes instead of explaining history
- Trim obvious statements, repeated rules, misplaced detail, and warnings for risks that no longer exist

## Closeout

1. Re-check changed paths against the DOX chain
2. Update nearest owning docs and any affected parents or children
3. Refresh every affected Child DOX Index
4. Remove stale or contradictory text
5. Run existing verification when relevant
6. Report any docs intentionally left unchanged and why

## User Preferences

When the user requests a durable behavior change, record it here or in the relevant child AGENTS.md

### Subagent Execution Model (durable instruction, 2026-09-12)

- Execute every user request via subagents: a single subagent for one coherent task, or multiple parallel subagents for independent work streams
- Dispatch parallel subagents only for truly independent tasks (disjoint files/concerns); run dependent work sequentially, never concurrently
- Never let parallel subagents edit the same files or shared state; parallelize read-only exploration freely, serialize mutations
- Give each subagent a self-contained prompt: full context, exact target paths, expected output, and a reminder to follow the applicable DOX chain
- Treat subagent output as unverified until integrated: review results, run the relevant verification, and complete the DOX pass before reporting back to the user

## Child DOX Index

- `dashscope_proxy_lib/` — proxy core: rate limiter (sliding window, token bucket, multi-provider, 3-state circuit breaker CLOSED→OPEN→HALF_OPEN with `can_attempt_probe` single in-flight probe after cooldown + `release_probe` on abort, per-model usage tracking with LRU eviction, `try_admit` checks RPM + reserves TPM lock-free then stamps `last_request_time` under a microsecond-held admit lock with RPS re-check + `record_completion`, TPM wait via `wait_seconds_for` with no admit lock held (≥1s floor), `TokenWindowCounter` running `_used_total` (no O(n) sum under lock), pending counter on `threading.Lock`, never hold admit `asyncio.Lock` across counter/`_thread_lock` calls), request queue, HTTP helpers (header stripping, `_sleep_interruptible()`, `_sse_response_headers()`, `_finalize_stream_response()`, `_upstream_error_response()`, `_read_upstream_capped()` (caps upstream body reads at `UPSTREAM_MAX_BODY_SIZE`, default 50 MB — truncated error bodies proxied as-is with a warning; truncated non-stream success body discarded with 502 + TPM refund), `SECURITY_HEADERS_TO_STRIP`), provider routing (DashScope/MIMO/OpenLux/ARK/MetaAI/DeepSeek/GLM/Agnes AI text+image+video, availability-gated overlap registry, `MODEL_PROVIDER_MAP`, `MODEL_FALLBACK_ORDER`, `PROVIDER_SLUGS`), request transformation (robust MIMO normalization, `PROVIDER_SLUG_MAP`, `requires_messages` / `GENERATION_PATH_MARKERS` ingress predicate that exempts generation paths from the `messages` requirement while keeping `model` mandatory), token utilities (partial stream handling, `estimate_tokens_for_body` / `estimate_tokens_for_request` with tool/system counting, single ingress dump), session logging (bounded queue + background drain, batched flush, `SESSION_LOG_SYNC_FLUSH` escape hatch, thread-safe close/drain, sync `log()` serialized against `close()`), logging infrastructure (non-serializable value handling, `configure_logging(enable_tui_handler=…)` — headless skips TUI handler; TUI handler attaches to the `dashscope_proxy_lib` package logger so every lib module logger feeds the TUI via propagation), server lifecycle (Windows-compatible, `--headless`, modern async patterns, startup failure cleanup for sessions/connectors/log writers/runners, bounded TUI shutdown verification, PID file management)
- `tests/` — pytest test suite: unit tests (rate limiter incl. `TestTryAdmitAndRecordCompletion` / admit-time RPS / TPM wait outside admit lock / `test_status_during_completion` / `TestCircuitBreaker` HALF_OPEN probe + release, token utils incl. `TestEstimateTokensDict`, request transform, HTTP helpers, session log async enqueue/batch flush, `TestConfigureLogging` headless skip + package-logger attachment, `TestSessionLogIncrementalRead` incl. truncation/inode-rotation resets, `TestLatencyTrackerMergeSnapshot`, pinned routing, overlap registry, provider prefix splitting, `TestRequiresMessages` generation-path ingress predicate, Agnes AI per-ordinal routing + shared-key availability + display-config rows), integration tests (handler with mock upstream, circuit single-probe after cooldown, probe clear on 429/disconnect, provider routing tests incl. `TestOctonaryProviderRouting` / `TestNonaryProviderRouting` / `TestDecenaryProviderRouting`, `TestGenerationPathForwarding`), audit locks (`test_audit_lifecycle.py` incl. `TestModelFallbackOrderNormalization` / `TestServerPortValidation` / `TestServerSessionLogShutdown` / `TestPidFileMarker` / `TestSessionLogSafeParsing` / `TestSessionLogCloseJoinTimeout` / `TestSafeFloat` / `TestLogBufferSizeFloor`, `test_audit_ratelimiter.py`, `test_audit_request_path.py` incl. `TestGenerationPathIngressExemption` / `TestReadUpstreamCappedCaps` / `TestStripClientSecurityHeaders` / `TestSecurityHeadersHandlerSites` / `TestParseRetryAfterCapped` / `TestRetryAfterCappedCallSite` / `TestNormalizeModelNameAudit` / `TestSplitProviderPrefixMultiSlash` / `TestEstimateTokensMediaAndNonStringParts` / `TestExtractTokensStreamPrefixes` / `TestRecordCompletionCircuitNeutrality` / `TestWaitForSlotTuple` / `TestWaitForSlotJitterBranches` / `TestHandlerConsumesWaitForSlotTuple` / `TestCircuitOpenFirstCandidate` / `TestTruncatedUpstreamBodies` / `TestInvalidModelPinIngress` / `TestPostQueueAdmitDenial` / `TestStreamAbortExceptionContract` / `TestProviderRouterAudit`, `test_audit_tui.py`), e2e tests (real API calls, requires API key)
- `benchmarks/` — local proxy-overhead harness (`run_bench.py`); results under `benchmarks/results/` are gitignored
- `screenshots/` — TUI dashboard screenshots (SVG format) and SVG→PNG conversion script
- `docs/` — documentation directory containing superpowers plans and specs (ephemeral planning documents)
- Root-owned files: `dashscope_proxy.py` (facade re-exporting lib), `proxy_tui.py` + `proxy_tui.tcss` (Textual TUI dashboard; poller uses MultiProvider top-level `pending_requests` / `recent_latencies`; Overview left panel uses a compact provider grid: one `DataTable#provider-grid` row per provider (Provider | Circuit | RPM | TPM | Fwd | 429, ~10-char bar + percent cells, idle providers render as idle rows) fed by `tui_status.provider_grid_rows`, plus a cursor-following detail pane (`#provider-detail-title` + `#provider-detail`, updates on `DataTable.RowHighlighted`, defaults to primary) and a compact aggregated `#request-stats`; per-provider sections and `#rl-metrics` removed; Overview titles use flow-based `.section-title` (`.panel-title` keeps `dock: top` and is only for the log panel / Metrics tab); Config uses `_load_display_config()`; latency tracker accumulates across polls via `LatencyTracker.merge_snapshot` (count-based overlap merge of consecutive status snapshots); TPM quota calculation fixed; alert badge uses `set_class()`; log export/filter features; Log widgets `max_lines`-capped to bound scrollback memory (`#live-log` 500, `#live-log-full` `LOG_BUFFER_SIZE`); `PROVIDER_REGISTRY` constant; incremental session log read via `_read_session_log_entries` — tracks file offset/inode, carries incomplete trailing lines in `_session_log_partial`, keeps last 200 parsed entries in a deque; failover panel reads `timestamp_utc` from session entries), `tui_status.py` (pure status helpers for the TUI poller: `series_from_status`, `provider_grid_rows` + `mini_bar` / `circuit_glyph` / `PROVIDER_KEYS` / `PROVIDER_LABELS` for the Overview grid, `overview_request_stats`, `failover_alert_should_show`; legacy `provider_section_visible` retained), `run_tui.ps1` (PowerShell launcher), `test_live_server.py` (manual live server test), `capture_screenshots.py` (screenshot automation), `.env.example`, `requirements.txt`, `pyproject.toml`
- Root utility scripts (diagnostic/analysis tools, not part of core proxy): `test_agnes.py` (Agnes AI text/image/video tests), `test_all_providers.py` (real-world provider tests), `test_openlux_models.py` / `query_openlux_models.py` / `check_openlux_models.py` / `_test_openlux.py` (OpenLux model queries), `_check_yesterday.py` / `_check_retries.py` / `_analyze_errors.py` (session log analysis)
