# Task 3: Remove Quota Retry Logic from Handlers

**Goal:** Remove quota retry variables and special quota-429 branches from request handler.

**Files:**
- Modify: `dashscope_proxy_lib/handlers.py`

## Exact Requirements

### Remove quota retry variables
From the variable initialization section (around line 410), remove:
```python
    quota_retries = 0
    quota_max = getattr(limiter, "quota_max_retries", 0)
    quota_cooldown = getattr(limiter, "quota_retry_cooldown", 1800)
```

### Streaming 429 handling (around line 560)
Replace the quota retry logic:
```python
                        if not should_retry_429(error_body):
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

With immediate return:
```python
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

### Non-streaming 429 handling (around line 810)
Replace the same quota retry logic with immediate return.

## Tests to Run

After making changes:
1. Run: `py -m pytest tests/test_integration.py::TestMaxRetriesExhausted -v`
2. Run: `py -m pytest tests/test_integration.py::TestNonStreaming429 -v`

Expected: Generic 429 retry tests pass. Quota-specific tests will be updated in Task 5.

## Commit

After implementing:
```bash
git add dashscope_proxy_lib/handlers.py
git commit -m "refactor(handlers): remove quota retry logic, immediate terminal 429"
```