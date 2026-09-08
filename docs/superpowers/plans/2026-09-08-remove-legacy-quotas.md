# Remove Legacy Quotas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove 5-hour, weekly, and monthly quota tracking and retry behavior from the proxy.

**Architecture:** Delete quota state, checks, and retry logic from config, rate limiter, handler, and tests. Terminal upstream 429 responses return immediately without cooldown retry.

**Tech Stack:** Python 3.14, aiohttp, pytest, Textual TUI

## Global Constraints

- Remove quota configuration keys: `requests_per_5h`, `requests_per_week`, `requests_per_month`, `quota_retry_cooldown`, `quota_max_retries`
- Remove quota state: `hour5_window`, `hour5_limit`, `week_count`, `week_start`, `week_limit`, `month_count`, `month_start`, `month_limit`
- Remove quota retry variables: `quota_retries`, `quota_max`, `quota_cooldown`
- Remove quota status fields: `requests_5h`, `requests_5h_limit`, `requests_week`, `requests_week_limit`, `requests_month`, `requests_month_limit`
- Keep RPM/TPM quota warnings in TUI (they check active limits)
- Keep `should_retry_429()` and `_NON_RETRYABLE_429_MARKERS` unchanged
- Terminal 429: immediate return, no cooldown retry

---

## Task 1: Update RateLimiter Configuration

**Files:**
- Modify: `dashscope_proxy_lib/config.py`
- Test: `tests/conftest.py`, `tests/test_units.py`, `tests/test_integration.py`

**Interfaces:**
- Consumes: None
- Produces: Config dicts with keys: `rpm_limit`, `tpm_limit`, `safety_factor`, `max_queue_size`, `max_retries`, `base_backoff`

- [ ] **Step 1: Remove quota keys from CODING_PLAN_CONFIG**

```python
# Remove these lines from CODING_PLAN_CONFIG:
    "requests_per_5h": 6000,
    "requests_per_week": 45000,
    "requests_per_month": 90000,
    # Quota-exceeded retry: wait this many seconds then retry once.
    # Alibaba Coding Plan cooldown is ~30 min; default covers it with margin.
    "quota_retry_cooldown": _safe_int("PROXY_QUOTA_RETRY_COOLDOWN", 1800),
    "quota_max_retries": _safe_int("PROXY_QUOTA_MAX_RETRIES", 1),
```

- [ ] **Step 2: Remove quota keys from SECONDARY_CODING_PLAN_CONFIG**

```python
# Remove these lines from SECONDARY_CODING_PLAN_CONFIG:
    "requests_per_5h": _safe_int("SECONDARY_REQUESTS_PER_5H", CODING_PLAN_CONFIG["requests_per_5h"]),
    "requests_per_week": _safe_int("SECONDARY_REQUESTS_PER_WEEK", CODING_PLAN_CONFIG["requests_per_week"]),
    "requests_per_month": _safe_int("SECONDARY_REQUESTS_PER_MONTH", CODING_PLAN_CONFIG["requests_per_month"]),
    "quota_retry_cooldown": _safe_int("SECONDARY_QUOTA_RETRY_COOLDOWN", CODING_PLAN_CONFIG["quota_retry_cooldown"]),
    "quota_max_retries": _safe_int("SECONDARY_QUOTA_MAX_RETRIES", CODING_PLAN_CONFIG["quota_max_retries"]),
```

- [ ] **Step 3: Remove quota keys from TERTIARY_DEFAULTS and TERTIARY_CODING_PLAN_CONFIG**

```python
# Remove from TERTIARY_DEFAULTS:
    "requests_per_5h": 3000,
    "requests_per_week": 20000,
    "requests_per_month": 50000,
    "quota_retry_cooldown": 1800,
    "quota_max_retries": 1,

# Remove from TERTIARY_CODING_PLAN_CONFIG:
    "requests_per_5h": _safe_int("TERTIARY_REQUESTS_PER_5H", TERTIARY_DEFAULTS["requests_per_5h"]),
    "requests_per_week": _safe_int("TERTIARY_REQUESTS_PER_WEEK", TERTIARY_DEFAULTS["requests_per_week"]),
    "requests_per_month": _safe_int("TERTIARY_REQUESTS_PER_MONTH", TERTIARY_DEFAULTS["requests_per_month"]),
    "quota_retry_cooldown": _safe_int("TERTIARY_QUOTA_RETRY_COOLDOWN", TERTIARY_DEFAULTS["quota_retry_cooldown"]),
    "quota_max_retries": _safe_int("TERTIARY_QUOTA_MAX_RETRIES", TERTIARY_DEFAULTS["quota_max_retries"]),
```

- [ ] **Step 4: Remove quota keys from QUATERNARY, QUINARY, SENARY, SEPTENARY configs**

Repeat the same pattern for each remaining provider config:
- QUATERNARY_DEFAULTS and QUATERNARY_CODING_PLAN_CONFIG
- QUINARY_DEFAULTS and QUINARY_CODING_PLAN_CONFIG
- SENARY_DEFAULTS and SENARY_CODING_PLAN_CONFIG
- SEPTENARY_DEFAULTS and SEPTENARY_CODING_PLAN_CONFIG

- [ ] **Step 5: Update test config in conftest.py**

```python
# In tests/conftest.py, update rate_limiter fixture config:
@pytest.fixture
def rate_limiter(dashscope_module):
    """Create a RateLimiter with tiny limits for fast tests."""
    config = {
        "rpm_limit": 60,
        "tpm_limit": 100_000,
        "safety_factor": 0.8,
        # Remove: "requests_per_5h": 100,
        # Remove: "requests_per_week": 100,
        # Remove: "requests_per_month": 100,
        "max_queue_size": 5,
        "max_retries": 3,
        "base_backoff": 0.1,
    }
    return dashscope_module.RateLimiter(config)
```

- [ ] **Step 6: Update make_test_config in conftest.py**

```python
# Update make_test_config fixture:
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

- [ ] **Step 7: Run tests to verify config changes work**

Run: `py -m pytest tests/test_units.py::TestRateLimiterCanProceed -v`
Expected: PASS (quota-related tests will fail, others pass)

- [ ] **Step 8: Commit**

```bash
git add dashscope_proxy_lib/config.py tests/conftest.py
git commit -m "refactor(config): remove quota configuration keys"
```

---

## Task 2: Remove Quota State from RateLimiter

**Files:**
- Modify: `dashscope_proxy_lib/rate_limiter.py`
- Test: `tests/test_units.py`

**Interfaces:**
- Consumes: Config dicts from Task 1
- Produces: RateLimiter without quota state, `status()` without quota fields

- [ ] **Step 1: Remove quota state initialization in RateLimiter.__init__()**

```python
# Remove these lines from RateLimiter.__init__():
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

- [ ] **Step 2: Remove quota checks from RateLimiter.can_proceed()**

```python
# Remove these lines from RateLimiter.can_proceed():
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

- [ ] **Step 3: Remove quota increments from RateLimiter.record_request()**

```python
# Remove these lines from RateLimiter.record_request():
            self.hour5_window.add(now)
            self.week_count += 1
            self.month_count += 1
```

- [ ] **Step 4: Remove quota fields from RateLimiter.status()**

```python
# Remove these lines from the return dict in RateLimiter.status():
            "requests_5h": self.hour5_window.count(now),
            "requests_5h_limit": self.hour5_limit,
            "requests_week": self.week_count,
            "requests_week_limit": self.week_limit,
            "requests_month": self.month_count,
            "requests_month_limit": self.month_limit,
```

- [ ] **Step 5: Update MultiProviderRateLimiter limits_differ check**

```python
# In MultiProviderRateLimiter.__init__(), update the limits_differ check:
        # Before:
        limits_differ = any(
            primary_config.get(k) != septenary_config.get(k)
            for k in ["rpm_limit", "tpm_limit", "requests_per_5h",
                     "requests_per_week", "requests_per_month"]
        )
        
        # After:
        limits_differ = any(
            primary_config.get(k) != septenary_config.get(k)
            for k in ["rpm_limit", "tpm_limit"]
        )
```

- [ ] **Step 6: Run tests to verify quota state removal**

Run: `py -m pytest tests/test_units.py::TestSlidingWindowCounter -v`
Expected: PASS (unaffected tests)

Run: `py -m pytest tests/test_units.py::TestRateLimiterCanProceed -v`
Expected: Some tests fail (quota-related), others pass

- [ ] **Step 7: Commit**

```bash
git add dashscope_proxy_lib/rate_limiter.py
git commit -m "refactor(rate_limiter): remove quota state and checks"
```

---

## Task 3: Remove Quota Retry Logic from Handlers

**Files:**
- Modify: `dashscope_proxy_lib/handlers.py`
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: RateLimiter without quota state from Task 2
- Produces: Handler that returns terminal 429 immediately

- [ ] **Step 1: Remove quota retry variables from handle_request()**

```python
# Remove these lines from the variable initialization section:
    quota_retries = 0
    quota_max = getattr(limiter, "quota_max_retries", 0)
    quota_cooldown = getattr(limiter, "quota_retry_cooldown", 1800)
```

- [ ] **Step 2: Remove quota retry branch from streaming 429 handling**

```python
# In the streaming 429 handling section, remove the quota retry logic:
                        if not should_retry_429(error_body):
                            # REMOVE this entire block:
                            if quota_retries < quota_max:
                                quota_retries += 1
                                _log(logging.WARNING, "upstream quota exceeded, retrying after cooldown",
                                     request_id=request_id, model=model_name,
                                     quota_retry=quota_retries, quota_max=quota_max,
                                     cooldown_sec=quota_cooldown)
                                del error_body
                                if not await _sleep_interruptible(request, quota_cooldown):
                                    error_reason = "client_disconnected"
                                    status_code = 499
                                    await limiter.refund_tokens(estimated_tokens)
                                    return _make_error_response(499, b'{"error":"client disconnected"}', request_id)
                                continue
                            error_reason = "upstream_quota_exceeded"
                            status_code = 429
                            _log(logging.WARNING, "upstream quota exceeded, not retrying",
                                 request_id=request_id, model=model_name)
                            await limiter.refund_tokens(estimated_tokens)
                            resp = web.Response(status=429, body=error_body, content_type="application/json")
                            resp.headers["X-Request-ID"] = request_id
                            _add_forwarded_headers(resp, upstream_headers)
                            return resp
```

- [ ] **Step 3: Replace with immediate return for terminal 429**

```python
# Replace the removed quota retry logic with:
                        if not should_retry_429(error_body):
                            error_reason = "upstream_429_terminal"
                            status_code = 429
                            _log(logging.WARNING, "upstream terminal 429, not retrying",
                                 request_id=request_id, model=model_name)
                            await limiter.refund_tokens(estimated_tokens)
                            resp = web.Response(status=429, body=error_body, content_type="application/json")
                            resp.headers["X-Request-ID"] = request_id
                            _add_forwarded_headers(resp, upstream_headers)
                            return resp
```

- [ ] **Step 4: Remove quota retry branch from non-streaming 429 handling**

```python
# In the non-streaming 429 handling section, remove the same pattern:
                        if not should_retry_429(resp_body):
                            # REMOVE this entire block (same as streaming)
```

- [ ] **Step 5: Replace with immediate return for terminal 429 (non-streaming)**

```python
# Replace with:
                        if not should_retry_429(resp_body):
                            error_reason = "upstream_429_terminal"
                            _log(logging.WARNING, "upstream terminal 429, not retrying",
                                 request_id=request_id, model=model_name)
                            await limiter.refund_tokens(estimated_tokens)
                            out = web.Response(status=429, body=resp_body, content_type="application/json")
                            out.headers["X-Request-ID"] = request_id
                            _add_forwarded_headers(out, resp_headers)
                            return out
```

- [ ] **Step 6: Run integration tests**

Run: `py -m pytest tests/test_integration.py::TestNonStreaming429 -v`
Expected: PASS (generic 429 retry still works)

- [ ] **Step 7: Commit**

```bash
git add dashscope_proxy_lib/handlers.py
git commit -m "refactor(handlers): remove quota retry logic, immediate terminal 429"
```

---

## Task 4: Remove Quota Fields from Server Logging

**Files:**
- Modify: `dashscope_proxy_lib/server.py`

**Interfaces:**
- Consumes: RateLimiter without quota state from Task 2
- Produces: Startup log without quota fields

- [ ] **Step 1: Remove quota fields from startup log**

```python
# In server.py, remove quota fields from the startup log:
    _log(logging.INFO, "proxy started",
         host=PROXY_HOST, port=PROXY_PORT, target=TARGET_BASE,
         rps=rate_limiter.primary.rps_limit, rpm=rate_limiter.primary.rpm_limit,
         tpm=rate_limiter.primary.tpm_limit,
         # Remove: quota_5h=rate_limiter.primary.hour5_limit,
         # Remove: quota_week=rate_limiter.primary.week_limit,
         # Remove: quota_month=rate_limiter.primary.month_limit,
         safety_factor=config["safety_factor"])
```

- [ ] **Step 2: Commit**

```bash
git add dashscope_proxy_lib/server.py
git commit -m "refactor(server): remove quota fields from startup log"
```

---

## Task 5: Update Tests for Quota Removal

**Files:**
- Modify: `tests/test_units.py`, `tests/test_integration.py`

**Interfaces:**
- Consumes: All changes from Tasks 1-4
- Produces: All tests passing with reduced config shape

- [ ] **Step 1: Remove quota-related tests from test_units.py**

Delete the following test classes and methods:
- `TestRateLimiterCanProceed::test_blocks_at_5h_limit`
- `TestRateLimiterCanProceed::test_blocks_at_weekly_limit`
- `TestRateLimiterCanProceed::test_blocks_at_monthly_limit`
- `TestRateLimiterQuotaReset` (entire class)

- [ ] **Step 2: Update test configs throughout test_units.py**

Remove quota keys from all test config dicts:
- `TestRateLimiterCanProceed::test_rps_spacing` config
- `TestComputeBackoff` configs
- `TestWaitForSlot` configs
- `TestMultiProviderRateLimiter::_make_config()`

- [ ] **Step 3: Update test_quota_exceeded_429_not_retried in test_integration.py**

```python
# Update the test to verify immediate return (no retry):
    async def test_terminal_429_returned_immediately(self, aiohttp_client, proxy_app):
        """Terminal 429 from upstream is returned immediately without retry."""
        upstream_app = web.Application()
        quota_body = json.dumps({
            "error": {
                "code": "throttling",
                "message": "usage allocated quota exceeded. please try again later.",
            }
        }).encode()
        call_count = 0

        async def quota_429(request):
            nonlocal call_count
            call_count += 1
            return web.Response(status=429, body=quota_body, content_type="application/json")

        upstream_app.router.add_post("/v1/chat/completions", quota_429)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, rl = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 429
                    data = await resp.json()
                    assert "quota exceeded" in data["error"]["message"].lower()
                    assert limiter.total_429s == 1  # Only one upstream call
                    assert call_count == 1  # No retry
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()
```

- [ ] **Step 4: Run full test suite**

Run: `py -m pytest tests/`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_units.py tests/test_integration.py
git commit -m "test: update tests for quota removal"
```

---

## Task 6: Update Documentation

**Files:**
- Modify: `.env.example`, `README.md`, `README_SCRIPT.md`, `dashscope_proxy_lib/AGENTS.md`

**Interfaces:**
- Consumes: All changes from Tasks 1-5
- Produces: Documentation without quota references

- [ ] **Step 1: Remove quota env vars from .env.example**

Remove all lines containing:
- `REQUESTS_PER_5H`
- `REQUESTS_PER_WEEK`
- `REQUESTS_PER_MONTH`
- `QUOTA_RETRY_COOLDOWN`
- `QUOTA_MAX_RETRIES`

- [ ] **Step 2: Remove quota table from README.md**

Remove the quota configuration table entries:
```markdown
| `requests_per_5h` | 6000 | Rolling 5-hour request cap |
| `requests_per_week` | 45000 | Weekly request cap |
| `requests_per_month` | 90000 | Monthly request cap |
```

- [ ] **Step 3: Update README.md feature description**

Update the rate limiting section:
```markdown
**Multi-layer rate limiting**
Enforces RPS, RPM, and TPM (via Token Bucket). A configurable safety factor keeps usage below the hard limits.
```

Remove the quota-specific bullet from the list.

- [ ] **Step 4: Update README_SCRIPT.md**

Remove quota references from TUI feature description.

- [ ] **Step 5: Update dashscope_proxy_lib/AGENTS.md**

Update the `RateLimiter` description:
```markdown
- `RateLimiter` — combines sliding window + token bucket + circuit breaker
```

Remove "quota windows (5h/week/month)" from the description.

- [ ] **Step 6: Commit**

```bash
git add .env.example README.md README_SCRIPT.md dashscope_proxy_lib/AGENTS.md
git commit -m "docs: remove quota configuration and behavior descriptions"
```

---

## Task 7: Final Verification

**Files:**
- All project files

**Interfaces:**
- Consumes: All changes from Tasks 1-6
- Produces: Clean codebase with no quota references

- [ ] **Step 1: Search for remaining quota references**

Run: `py -m pytest tests/`
Expected: All tests PASS

Run linter on edited files:
```bash
# Check for any remaining quota references
grep -r "quota" dashscope_proxy_lib/ --include="*.py" || echo "No quota references found"
grep -r "requests_per_5h\|requests_per_week\|requests_per_month" . --include="*.py" --include="*.md" --include=".env*" || echo "No quota config references found"
```

- [ ] **Step 2: Run full test suite one more time**

Run: `py -m pytest tests/`
Expected: All tests PASS

- [ ] **Step 3: Create summary commit**

```bash
git add -A
git commit -m "refactor: remove legacy quota tracking and retry behavior

- Remove 5-hour, weekly, and monthly quota configuration
- Remove quota state from RateLimiter
- Remove quota retry logic from handlers
- Terminal 429 responses returned immediately
- Update tests and documentation

BREAKING CHANGE: quota environment variables and status fields removed"
```

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-08-remove-legacy-quotas.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**