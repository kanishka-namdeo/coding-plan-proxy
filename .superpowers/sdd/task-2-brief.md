# Task 2: Remove Quota State from RateLimiter

**Goal:** Remove quota state, checks, increments, and status fields from RateLimiter.

**Files:**
- Modify: `dashscope_proxy_lib/rate_limiter.py`

## Exact Requirements

### RateLimiter.__init__()
Remove these lines:
```python
        self.hour5_window = SlidingWindowCounter(5 * 3600)
        
        self.week_count = 0
        self.month_count = 0
        self.week_start = time.time()
        self.month_start = time.time()

        self.week_limit = config["requests_per_week"]
        self.month_limit = config["requests_per_month"]
        self.hour5_limit = config["requests_per_5h"]
        
        self.quota_retry_cooldown = config.get("quota_retry_cooldown", 1800)
        self.quota_max_retries = config.get("quota_max_retries", 1)
```

### RateLimiter.can_proceed()
Remove these lines:
```python
            if now_wall - self.week_start >= 7 * 24 * 3600:
                self.week_count = 0
                self.week_start = now_wall
            if now_wall - self.month_start >= 30 * 24 * 3600:
                self.month_count = 0
                self.month_start = now_wall

            h5_count = self.hour5_window.count(now_mono)
            if h5_count >= self.hour5_limit:
                oldest = self.hour5_window.events[0] if self.hour5_window.events else now_mono
                wait = max(0, oldest + 5 * 3600 - now_mono)
                _log(logging.DEBUG, "can_proceed denied: 5-hour quota exhausted", wait_seconds=wait)
                return False, "5-hour quota exhausted", wait

            if self.week_count >= self.week_limit:
                _log(logging.DEBUG, "can_proceed denied: weekly quota exhausted", wait_seconds=60)
                return False, "Weekly quota exhausted", 60

            if self.month_count >= self.month_limit:
                _log(logging.DEBUG, "can_proceed denied: monthly quota exhausted", wait_seconds=60)
                return False, "Monthly quota exhausted", 60
```

### RateLimiter.record_request()
Remove these lines:
```python
            self.hour5_window.add(now)
            self.week_count += 1
            self.month_count += 1
```

### RateLimiter.status()
Remove these lines from the return dict:
```python
            "requests_5h": self.hour5_window.count(now),
            "requests_5h_limit": self.hour5_limit,
            "requests_week": self.week_count,
            "requests_week_limit": self.week_limit,
            "requests_month": self.month_count,
            "requests_month_limit": self.month_limit,
```

### MultiProviderRateLimiter.__init__()
Update the `limits_differ` check from:
```python
        limits_differ = any(
            primary_config.get(k) != septenary_config.get(k)
            for k in ["rpm_limit", "tpm_limit", "requests_per_5h",
                     "requests_per_week", "requests_per_month"]
        )
```

To:
```python
        limits_differ = any(
            primary_config.get(k) != septenary_config.get(k)
            for k in ["rpm_limit", "tpm_limit"]
        )
```

## Tests to Run

After making changes:
1. Run: `py -m pytest tests/test_units.py::TestRateLimiterCanProceed -v`
2. Run: `py -m pytest tests/test_units.py::TestSlidingWindowCounter -v`

Expected: Tests that don't use quota state should pass. Quota-specific tests will be removed in Task 5.

## Commit

After implementing:
```bash
git add dashscope_proxy_lib/rate_limiter.py
git commit -m "refactor(rate_limiter): remove quota state and checks"
```