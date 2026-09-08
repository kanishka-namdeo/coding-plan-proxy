# Task 8: Remove Legacy Quota Fields from Mock Status

**Status:** DONE

## Summary

Removed quota status fields from the `_default_status()` method in `capture_screenshots.py` as requested.

## Commit

- `8207613` — Remove legacy quota fields from mock status in capture_screenshots.py

## Changes

Removed the following 6 fields from `_default_status()` in `capture_screenshots.py`:

1. `requests_5h`
2. `requests_5h_limit`
3. `requests_week`
4. `requests_week_limit`
5. `requests_month`
6. `requests_month_limit`

All other status fields were preserved.

## Test Summary

**Command:** `python -m pytest tests/ -v`

**Result:** 292 passed, 1 warning in 136.28s

All tests pass. The warning is pre-existing and unrelated to this change (coroutine not awaited in a mock).

## Implementation Details

The `_default_status()` method in `LiveRateLimiterProxy` class provides fallback mock status when the live proxy is unavailable. The quota fields (`requests_5h`, `requests_week`, `requests_month` and their limits) were removed from this fallback dictionary. The live status fetched from `/v1/proxy/status` endpoint is unchanged — this only affects the mock fallback scenario.

## Concerns

None.