# Task 1: Update RateLimiter Configuration

**Goal:** Remove quota configuration keys from all provider config dictionaries.

**Files:**
- Modify: `dashscope_proxy_lib/config.py`
- Modify: `tests/conftest.py`

## Exact Requirements

Remove the following keys from every provider config dictionary:

### CODING_PLAN_CONFIG
Remove these lines:
```python
    "requests_per_5h": 6000,
    "requests_per_week": 45000,
    "requests_per_month": 90000,
    # Quota-exceeded retry: wait this many seconds then retry once.
    # Alibaba Coding Plan cooldown is ~30 min; default covers it with margin.
    "quota_retry_cooldown": _safe_int("PROXY_QUOTA_RETRY_COOLDOWN", 1800),
    "quota_max_retries": _safe_int("PROXY_QUOTA_MAX_RETRIES", 1),
```

### SECONDARY_CODING_PLAN_CONFIG
Remove these lines:
```python
    "requests_per_5h": _safe_int("SECONDARY_REQUESTS_PER_5H", CODING_PLAN_CONFIG["requests_per_5h"]),
    "requests_per_week": _safe_int("SECONDARY_REQUESTS_PER_WEEK", CODING_PLAN_CONFIG["requests_per_week"]),
    "requests_per_month": _safe_int("SECONDARY_REQUESTS_PER_MONTH", CODING_PLAN_CONFIG["requests_per_month"]),
    "quota_retry_cooldown": _safe_int("SECONDARY_QUOTA_RETRY_COOLDOWN", CODING_PLAN_CONFIG["quota_retry_cooldown"]),
    "quota_max_retries": _safe_int("SECONDARY_QUOTA_MAX_RETRIES", CODING_PLAN_CONFIG["quota_max_retries"]),
```

### TERTIARY_DEFAULTS
Remove these lines:
```python
    "requests_per_5h": 3000,
    "requests_per_week": 20000,
    "requests_per_month": 50000,
    "quota_retry_cooldown": 1800,
    "quota_max_retries": 1,
```

### TERTIARY_CODING_PLAN_CONFIG
Remove these lines:
```python
    "requests_per_5h": _safe_int("TERTIARY_REQUESTS_PER_5H", TERTIARY_DEFAULTS["requests_per_5h"]),
    "requests_per_week": _safe_int("TERTIARY_REQUESTS_PER_WEEK", TERTIARY_DEFAULTS["requests_per_week"]),
    "requests_per_month": _safe_int("TERTIARY_REQUESTS_PER_MONTH", TERTIARY_DEFAULTS["requests_per_month"]),
    "quota_retry_cooldown": _safe_int("TERTIARY_QUOTA_RETRY_COOLDOWN", TERTIARY_DEFAULTS["quota_retry_cooldown"]),
    "quota_max_retries": _safe_int("TERTIARY_QUOTA_MAX_RETRIES", TERTIARY_DEFAULTS["quota_max_retries"]),
```

### QUATERNARY_DEFAULTS and QUATERNARY_CODING_PLAN_CONFIG
Remove the same 5 keys.

### QUINARY_DEFAULTS and QUINARY_CODING_PLAN_CONFIG
Remove the same 5 keys.

### SENARY_DEFAULTS and SENARY_CODING_PLAN_CONFIG
Remove the same 5 keys.

### SEPTENARY_DEFAULTS and SEPTENARY_CODING_PLAN_CONFIG
Remove the same 5 keys.

### tests/conftest.py
Update the `rate_limiter` fixture config:
```python
@pytest.fixture
def rate_limiter(dashscope_module):
    """Create a RateLimiter with tiny limits for fast tests."""
    config = {
        "rpm_limit": 60,
        "tpm_limit": 100_000,
        "safety_factor": 0.8,
        # Remove these three lines:
        # "requests_per_5h": 100,
        # "requests_per_week": 100,
        # "requests_per_month": 100,
        "max_queue_size": 5,
        "max_retries": 3,
        "base_backoff": 0.1,
    }
    return dashscope_module.RateLimiter(config)
```

Update the `make_test_config` fixture:
```python
@pytest.fixture
def make_test_config(dashscope_module):
    """Return a config dict with large limits for fast tests."""
    def _make():
        return {
            "rpm_limit": 6000,
            "tpm_limit": 10_000_000,
            "safety_factor": 0.8,
            # Remove quota keys
            "max_queue_size": 50,
            "max_retries": 2,
            "base_backoff": 0.05,
        }
    return _make
```

## Tests to Run

After making changes:
1. Run: `py -m pytest tests/test_units.py::TestSlidingWindowCounter -v`
2. Run: `py -m pytest tests/test_units.py::TestRateLimiterCanProceed::test_allows_when_under_limits -v`

Expected: These tests should pass (quota-related tests will fail in later tasks).

## Commit

After implementing:
```bash
git add dashscope_proxy_lib/config.py tests/conftest.py
git commit -m "refactor(config): remove quota configuration keys"
```