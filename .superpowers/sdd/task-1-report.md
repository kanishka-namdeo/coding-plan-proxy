# Task 1 Report: Remove Quota Configuration Keys

**Status:** BLOCKED

## Issue

The task brief instructs to remove quota configuration keys from `config.py` and `conftest.py` only, and states that test `TestRateLimiterCanProceed::test_allows_when_under_limits` should pass.

However, `RateLimiter.__init__` in `rate_limiter.py` requires these keys:

```python
self.week_limit = config["requests_per_week"]      # KeyError if missing
self.month_limit = config["requests_per_month"]    # KeyError if missing
self.hour5_limit = config["requests_per_5h"]       # KeyError if missing
```

## Test Results

### Test 1: TestSlidingWindowCounter
```
tests/test_units.py::TestSlidingWindowCounter::test_add_and_count PASSED
tests/test_units.py::TestSlidingWindowCounter::test_prune_old_events PASSED
tests/test_units.py::TestSlidingWindowCounter::test_prune_all_events PASSED
tests/test_units.py::TestSlidingWindowCounter::test_deque_cap PASSED
tests/test_units.py::TestSlidingWindowCounter::test_empty_count PASSED

5 passed in 0.11s
```

### Test 2: TestRateLimiterCanProceed::test_allows_when_under_limits
```
ERROR at setup of TestRateLimiterCanProceed.test_allows_when_under_limits

self = <dashscope_proxy_lib.rate_limiter.RateLimiter object>
config = {'rpm_limit': 60, 'tpm_limit': 100000, 'safety_factor': 0.8, 'max_queue_size': 5, ...}

    def __init__(self, config: dict):
        ...
        self.week_limit = config["requests_per_week"]
                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^
E       KeyError: 'requests_per_week'

dashscope_proxy_lib\rate_limiter.py:186: KeyError
```

## Changes Made

### dashscope_proxy_lib/config.py
Removed quota keys from all 14 provider config dictionaries:
- `CODING_PLAN_CONFIG`
- `SECONDARY_CODING_PLAN_CONFIG`
- `TERTIARY_DEFAULTS`
- `TERTIARY_CODING_PLAN_CONFIG`
- `QUATERNARY_DEFAULTS`
- `QUATERNARY_CODING_PLAN_CONFIG`
- `QUINARY_DEFAULTS`
- `QUINARY_CODING_PLAN_CONFIG`
- `SENARY_DEFAULTS`
- `SENARY_CODING_PLAN_CONFIG`
- `SEPTENARY_DEFAULTS`
- `SEPTENARY_CODING_PLAN_CONFIG`

Keys removed:
- `requests_per_5h`
- `requests_per_week`
- `requests_per_month`
- `quota_retry_cooldown`
- `quota_max_retries`

### tests/conftest.py
Removed quota keys from:
- `rate_limiter` fixture
- `make_test_config` fixture

## Blocking Reason

The `RateLimiter` class in `rate_limiter.py` still requires quota configuration keys. According to the design spec at `docs/superpowers/specs/2026-09-08-remove-legacy-quotas-design.md`, the full removal includes:

1. `config.py`: Remove quota defaults (DONE)
2. `rate_limiter.py`: Remove quota state, checks, increments, status serialization (NOT DONE)
3. `handlers.py`: Remove quota retry logic (NOT DONE)
4. Additional files

The task brief specifies modifying only `config.py` and `conftest.py`, but the `RateLimiter` class requires additional changes to handle missing quota keys before the specified tests can pass.

## Recommendation

Either:
1. Expand Task 1 scope to include updating `rate_limiter.py` to remove quota state and checks
2. Or create a separate preliminary task to make quota keys optional in `RateLimiter.__init__`

The design spec indicates this is a multi-task refactoring; the current task boundaries may need adjustment.