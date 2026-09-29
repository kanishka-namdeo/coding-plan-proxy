# tests

## Purpose

Pytest test suite for the DashScope proxy. Three layers: unit tests (isolated classes/functions), integration tests (handler with mock upstream via aiohttp test server), and e2e tests (real API calls requiring an API key).

## Ownership

This directory owns all test code. Test fixtures and configuration live in `conftest.py`.

## Local Contracts

- **Test layers**:
  - `test_units.py` — unit tests for `SlidingWindowCounter`, `TokenWindowCounter` (incl. `_used_total` running-total consistency), `RateLimiter` (incl. `TestTryAdmitAndRecordCompletion` for coalesced admit/completion + `test_try_admit_updates_last_request_time` admission-time RPS + `test_concurrent_try_admit_no_serialization` concurrent-admit + `test_admit_paths_do_not_hold_admit_lock_into_bucket` admit-lock-free bucket probes + `test_pending_counter_thread_safe_without_event_loop_block` threading-lock pending counter + `test_tpm_wait_estimate_conservative_outside_admit_lock` TPM wait ≥ bucket estimate with no admit lock held, `test_status_during_completion` lock-order / concurrent status with raised `rps_limit`, `TestCircuitBreaker` HALF_OPEN `can_attempt_probe` / probe-success close / `record_circuit_failure` reopen / `release_probe`), token extraction (incl. partial stream handling, tool/system token counting, `_safe_int` guards), `TestEstimateTokensDict` (`estimate_tokens_for_body` parity with bytes wrapper), request transformation (incl. MIMO normalization edge cases, prefix splitting with normalization), HTTP helpers (incl. `TestReadUpstreamCapped` capped upstream body read via fakes — under/over/exact-cap/empty bodies), session logging (thread-safe close, `TestSessionLogAsyncEnqueue` non-blocking enqueue + drain + batched flush spy with stopped writer, close sentinel-on-Full preserve), TUI log handler (formatTime via `timezone.utc`), `TestConfigureLogging` (`enable_tui_handler=False` skips attaching `TUILogHandler`; `True` attaches to the `dashscope_proxy_lib` package logger so sibling module loggers propagate to the TUI feed; idempotent re-configuration), provider router (incl. septenary routing, prefix/overlap cases, availability-gated overlap registry), multi-provider rate limiter (incl. `get_limiter_for_provider` warning on fallback), TUI status helpers (`TestTuiStatusHelpers`), incremental TUI session log (`TestSessionLogIncrementalRead` — offset/inode + partial-line carry + truncation and file-recreation resets), latency tracker snapshot merge (`TestLatencyTrackerMergeSnapshot` — count-based overlap merge accumulates across polls), display-config rows (`TestLoadDisplayConfig`), structured log formatter (non-serializable values via `default=str`), pinned routing (`TestPinnedRouting`), overlap registry (`TestOverlapRegistry`), provider prefix splitting (`TestSplitProviderPrefix`), Overview provider grid rows (`TestProviderGridRows` — idle/active/open-circuit/half-open rows via `tui_status.provider_grid_rows`, row count matches providers in status dict), Agnes AI routing (`TestRequiresMessages` for the generation-path ingress predicate, per-ordinal routing + shared-`AGNES_API_KEY` availability, `get_all_models` / `get_provider_status` / `MODEL_PROVIDER_MAP` override, no cross-provider overlap, `TestLoadDisplayConfig` Agnes row keys plus a regression check that the six legacy key prefixes are unchanged, `TestMultiProviderRateLimiter` coverage for the three Agnes sub-limiters)
  - `test_integration.py` — integration tests for `handle_request()` with mock upstream servers: API key validation, health/ready endpoints, request validation, proxy forwarding, 429/5xx retries, mock models, proxy status, response headers, queue enforcement, circuit breaker (incl. single HALF_OPEN probe after cooldown under concurrency — raises `rps_limit` so admit-time RPS does not serialize the herd; terminal 429 / disconnect after probe claim clear `circuit_probe_in_flight`), graceful shutdown, streaming errors (incl. chunk idle-timeout SSE `proxy_error`), client disconnect, multi-retry scenarios, provider routing (incl. `TestSecondaryProviderRouting`, `TestTertiaryProviderRouting`, `TestQuaternaryProviderRouting`, `TestPinnedRoutingHandler`, `TestOctonaryProviderRouting`, `TestNonaryProviderRouting`, `TestDecenaryProviderRouting`), generation-path forwarding (`TestGenerationPathForwarding` — `POST /v1/images/generations` without `messages` reaches the mock upstream, while a chat POST without `messages` still 400s)
  - `test_e2e_real.py` — end-to-end tests against real DashScope API (requires `DASHSCOPE_API_KEY` in `.env`)
  - `test_audit_lifecycle.py` — audit locks for safe config parsing, session-log close semantics, logging topology, server startup / PID-file hardening, and slug-source alignment (`TestModelFallbackOrderNormalization` canonical set tracks every provider, `TestServerPortValidation` port validation, `TestServerSessionLogShutdown` bounded shutdown, `TestPidFileMarker` PID file round-trip, `TestSessionLogSafeParsing` env var parsing, `TestSessionLogCloseJoinTimeout` timeout paths, `TestSafeFloat` non-finite rejection, `TestLogBufferSizeFloor` buffer size validation)
  - `test_audit_ratelimiter.py` — audit locks for the rate limiter (`TestTryAdmitPhase4LockDiscipline` admit-lock discipline, `TestCircuitFlapping`, `TestProbeClaimWatchdog`, `TestRecordCompletionCircuitNone`, `TestZeroLimitWarning`, `TestReconcileOutOfOrder`)
  - `test_audit_request_path.py` — numbered audit locks for the request path, including `TestGenerationPathIngressExemption` (generation paths bypass only the `messages` check and still require `model`; chat-style and unknown paths stay strict), `TestReadUpstreamCappedCaps` (cap semantics), `TestStripClientSecurityHeaders` / `TestSecurityHeadersHandlerSites` (header stripping), `TestParseRetryAfterCapped` / `TestRetryAfterCappedCallSite` (429 backoff), `TestNormalizeModelNameAudit` (model normalization), `TestSplitProviderPrefixMultiSlash` (multi-slash pins), `TestEstimateTokensMediaAndNonStringParts` (token estimation), `TestExtractTokensStreamPrefixes` (SSE prefixes), `TestRecordCompletionCircuitNeutrality` (circuit neutrality), `TestWaitForSlotTuple` / `TestWaitForSlotJitterBranches` (RPM/TPM/RPS jitter), `TestHandlerConsumesWaitForSlotTuple` (handler integration), `TestCircuitOpenFirstCandidate` (failover vs pinned), `TestTruncatedUpstreamBodies` (2xx/429/5xx handling), `TestInvalidModelPinIngress` (multi-slash validation), `TestPostQueueAdmitDenial` (admit denial reasons), `TestStreamAbortExceptionContract` (CancelledError vs connection errors), `TestProviderRouterAudit` (live slug inversion, model sets)
  - `test_audit_tui.py` — audit locks for the TUI (`TestRequiredStatusKeys`, `TestDrainLogBuffer`, `TestPerProviderLatencyMerge`, `TestFailoverTimeGate`, `TestSessionLogReaderReplaceErrors`, `TestMiniBarClamp`, `TestSuccessRateDisplay`)
  - `conftest.py` — shared fixtures: `dashscope_module`, `rate_limiter`, `mock_app`, `mock_request`, `make_test_config`, `token_bucket`

- **Fixture conventions**:
  - `dashscope_module` — returns the already-imported `dashscope_proxy` module (facade)
  - `rate_limiter` — creates a `RateLimiter` with tiny limits for fast tests
  - `mock_app` — mock aiohttp app with `rate_limiter` and `client_session`
  - `mock_request` — mock aiohttp request with default path `/v1/chat/completions`
  - `proxy_app` (integration) — creates proxy app with test rate limiter and mock client session
  - `_reset_provider_router` (integration, autouse) — resets the lazy provider router singleton before each test

- **Mock patterns**:
  - Integration tests use `aiohttp_client` fixture to create a test server with the proxy app
  - Mock upstream servers are created inline in tests that need to simulate upstream responses
  - Tests patch `dashscope_proxy.TARGET_BASE` to point to the mock upstream
  - Use `_patch_target_base()` context manager (integration) to patch across all modules

- **Test configuration**:
  - `pyproject.toml` sets `asyncio_mode = "auto"` and `testpaths = ["tests"]`
  - Tests use `pytest.mark.asyncio` for async test functions
  - Rate limiter configs in tests use large limits (6000 RPM, 10M TPM) for fast execution
  - Backoff times are tiny (0.05s base) to keep tests fast

- **E2E requirements**:
  - `test_e2e_real.py` requires `DASHSCOPE_API_KEY` in `.env`
  - E2E tests make real API calls to DashScope and verify end-to-end behavior
  - Skip e2e tests if API key is not available

## Work Guidance

- Add unit tests for new pure functions and classes in `test_units.py`
- Add integration tests for new handler behavior in `test_integration.py`
- Use `aiohttp_client` fixture for integration tests that need a test server
- Mock upstream servers inline in tests; do not create shared mock server fixtures
- Patch constants via the facade (`dashscope_proxy.CONSTANT`), not direct imports
- Reset the provider router singleton before tests that exercise provider routing
- Keep test configs fast: large limits, tiny backoffs, small queue sizes
- Mark async tests with `@pytest.mark.asyncio` (or rely on `asyncio_mode = "auto"`)

## Verification

- `py -m pytest tests/` — full test suite
- `py -m pytest tests/test_units.py` — unit tests only
- `py -m pytest tests/test_integration.py` — integration tests only
- `py -m pytest tests/test_e2e_real.py` — e2e tests (requires API key)

## Child DOX Index

No child directories.
