# Task 4: Remove Quota Fields from Server Logging

**Goal:** Remove quota fields from startup log in server.py.

**Files:**
- Modify: `dashscope_proxy_lib/server.py`

## Exact Requirements

### Remove quota fields from startup log (around line 150)
Change from:
```python
    _log(logging.INFO, "proxy started",
         host=PROXY_HOST, port=PROXY_PORT, target=TARGET_BASE,
         rps=rate_limiter.primary.rps_limit, rpm=rate_limiter.primary.rpm_limit,
         tpm=rate_limiter.primary.tpm_limit,
         quota_5h=rate_limiter.primary.hour5_limit,
         quota_week=rate_limiter.primary.week_limit,
         quota_month=rate_limiter.primary.month_limit,
         safety_factor=config["safety_factor"])
```

To:
```python
    _log(logging.INFO, "proxy started",
         host=PROXY_HOST, port=PROXY_PORT, target=TARGET_BASE,
         rps=rate_limiter.primary.rps_limit, rpm=rate_limiter.primary.rpm_limit,
         tpm=rate_limiter.primary.tpm_limit,
         safety_factor=config["safety_factor"])
```

## Tests to Run

After making changes:
1. Run: `py -m pytest tests/test_units.py::TestSlidingWindowCounter -v`

Expected: Tests pass (no rate limiter changes in this task).

## Commit

After implementing:
```bash
git add dashscope_proxy_lib/server.py
git commit -m "refactor(server): remove quota fields from startup log"
```