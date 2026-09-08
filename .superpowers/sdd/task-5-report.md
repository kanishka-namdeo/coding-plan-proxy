# Task 5 Report: Status overlaps, facade exports, docs, full suite

## Status

**DONE**

## Commit Hash

`11025fe`

## Test Summary

- **Command:** `python -m pytest tests/ -v`
- **Result:** 292 passed, 1 warning
- **Duration:** ~136 seconds

All unit and integration tests pass. E2E tests also pass (API key was available).

## Changes Made

### Step 1: `model_overlaps` in status endpoint

✅ **Already implemented** - The `handlers.py` already included `model_overlaps` in the `/v1/proxy/status` response at lines 110-116.

### Step 2: Facade exports

✅ **Already implemented** - The `dashscope_proxy.py` facade already exported `PROVIDER_SLUGS` and `MODEL_FALLBACK_ORDER` in both the import block (lines 80-81) and `__all__` list (lines 175-176).

### Step 3: Documentation updates

#### AGENTS.md

**Updated** the provider-routing bullet to explicitly mention `MODEL_FALLBACK_ORDER`:
- Added: "in `MODEL_FALLBACK_ORDER` (default: septenary→senary→quinary→quaternary→tertiary→secondary→primary)"

#### .env.example

✅ **Already documented** - The `MODEL_FALLBACK_ORDER` environment variable was already documented at lines 163-165.

### Step 4: Test cleanup (quota removal)

The following quota-related tests were removed since quota features were removed in Tasks 1-4:

**Removed from `test_units.py`:**
- `test_blocks_at_5h_limit` - referenced `hour5_limit` attribute (removed)
- `test_blocks_at_weekly_limit` - referenced `week_limit`/`week_count` attributes (removed)
- `test_blocks_at_monthly_limit` - referenced `month_limit`/`month_count` attributes (removed)
- `test_weekly_counter_resets_after_7_days` - referenced `week_limit`/`week_start` (removed)
- `test_monthly_counter_resets_after_30_days` - referenced `month_limit`/`month_start` (removed)
- `test_weekly_counter_does_not_reset_prematurely` - referenced `week_limit` (removed)
- Entire `TestRateLimiterQuotaReset` class removed

**Updated in `test_integration.py`:**
- `test_quota_exceeded_429_not_retried` - Changed to verify **immediate return** (no retry) instead of expecting retries. Terminal 429s with "quota exceeded" message are now passed through immediately without retry attempts.
- Removed `quota_retry_cooldown` and `quota_max_retries` attribute accesses (no longer exist)
- Updated assertion: `limiter.total_429s == 1` (was 2) and `call_count == 1` (was 2)

**Updated test configs:**
- Removed `requests_per_5h`, `requests_per_week`, `requests_per_month` from `test_rps_spacing` config in `test_units.py`

## Concerns

None. All tests pass and the implementation matches the expected behavior for terminal 429s.