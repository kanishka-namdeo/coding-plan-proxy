# Remove Legacy Quota Tracking and Retry Behavior

**Date:** 2026-09-08
**Status:** Approved for implementation planning

## Goal

Remove the unused 5-hour, weekly, and monthly request quota subsystem from the proxy. The removal is intentional and complete: configuration, state, enforcement, status fields, quota-specific retry behavior, tests, and documentation must no longer describe or implement those quotas.

## Scope and behavior

The rate limiter will continue to enforce and report:

- RPS spacing and RPM sliding-window limits
- TPM token reservation, reconciliation, and refund
- bounded request queue limits
- generic upstream 429 retries with existing backoff
- 5xx retries with existing backoff
- circuit-breaker behavior
- request totals, token totals, body-size totals, queue wait metrics, model usage, and latency metrics

The following will be removed:

- 5-hour request window and limit
- weekly and monthly counters, start times, and limits
- `requests_per_5h`, `requests_per_week`, and `requests_per_month` configuration keys
- corresponding provider environment overrides
- `quota_retry_cooldown` and `quota_max_retries` configuration keys and limiter attributes
- special upstream quota-429 cooldown/retry behavior
- quota status fields and quota-specific tests/documentation

An upstream 429 classified as non-retryable by `should_retry_429()` will be returned immediately. A generic retryable 429 will continue through the existing normal retry path. This does not remove `should_retry_429()`; it still distinguishes retryable and terminal upstream 429 responses.

## Architecture and data flow

1. Provider configuration dictionaries contain only active rate-limiting controls: RPM, TPM, safety factor, queue size, retry count, and backoff.
2. `RateLimiter.can_proceed()` checks active RPM/RPS/TPM constraints and queue-related admission behavior; it performs no quota-window checks or resets.
3. `RateLimiter.record_request()` records active request and token metrics without incrementing long-term quota counters.
4. `RateLimiter.status()` omits the removed quota fields. `MultiProviderRateLimiter.status()` continues aggregating the remaining status fields and preserves unrelated status compatibility.
5. The request handler treats terminal upstream 429 responses as immediate terminal responses after refunding reserved TPM. It does not sleep or make a quota retry attempt.
6. The TUI Config tab automatically stops showing removed settings because it renders provider config dictionaries. Existing active TUI metrics remain unchanged.

## Error handling

- Terminal/non-retryable upstream 429: **currently** triggers a quota-specific cooldown retry (up to `quota_max_retries` times); **after removal** return HTTP 429 immediately with the upstream body and headers, refund any reserved TPM, and record the 429/model metric.
- Retryable upstream 429: retain the current retry counter, backoff, disconnect handling, failover behavior, and terminal response after retry exhaustion.
- No new error responses or fallback behavior are introduced.
- Legacy quota environment variables are ignored because they are no longer part of configuration; they are not preserved as compatibility aliases.
- The existing `should_retry_429()` function and `_NON_RETRYABLE_429_MARKERS` in `http_helpers.py` remain unchanged - they continue to classify upstream 429 responses as retryable or terminal.

## Files and contracts

Expected implementation files:

- `dashscope_proxy_lib/config.py`: remove quota defaults, overrides, and display rows indirectly supplied by provider configs.
- `dashscope_proxy_lib/rate_limiter.py`: remove quota state, checks, increments, status serialization, and the `limits_differ` logic in `MultiProviderRateLimiter.__init__()` that compares quota keys.
- `dashscope_proxy_lib/handlers.py`: remove quota cooldown variables (`quota_retries`, `quota_max`, `quota_cooldown`) and both special quota-429 branches (streaming and non-streaming). Terminal 429 responses should be returned immediately without cooldown retry.
- `dashscope_proxy_lib/server.py`: remove quota fields from startup logging.
- `proxy_tui.py`: keep existing RPM/TPM quota warning methods (`_check_quota_thresholds`, `_quota_warning`, `_update_alert_badge`) unchanged - these check active RPM/TPM limits, not the removed 5h/weekly/monthly quotas.
- `tests/conftest.py`, `tests/test_units.py`, and `tests/test_integration.py`: use the reduced config shape and verify immediate terminal handling plus unchanged generic retries.
- `.env.example`, `README.md`, `README_SCRIPT.md`, and applicable DOX/project reports: remove obsolete quota configuration and behavior descriptions.

Unrelated status fields, provider routing, failover, model analytics, body-size accounting, and session logging remain in scope only insofar as they must continue to work after the quota fields are removed.

## Testing and verification

Focused tests will cover:

- constructing every provider limiter with the reduced config shape;
- unchanged RPM, RPS, TPM, queue, circuit-breaker, and normal retry behavior;
- absence of quota state and quota status fields;
- exactly one upstream call for a terminal quota-style 429;
- continued retries for a generic retryable 429.

Verification will run:

```powershell
py -m pytest tests/test_units.py tests/test_integration.py
py -m pytest tests/
```

After edits, linter diagnostics will be checked for recently changed files, and a repository-wide search will confirm that quota configuration, enforcement, and cooldown references are gone except in historical documents where retaining history is intentional. Current README/DOX documentation will not retain stale product-contract claims.

## Compatibility decision

This is a breaking configuration/status change by design. Existing quota environment variables and status keys are removed rather than retained as ignored aliases. RPM, RPS, TPM, queue, retry, circuit-breaker, provider-routing, and remaining status contracts are preserved.
