# Task 3 Report: Remove Quota Retry Logic from Handlers

**Status:** DONE_WITH_CONCERNS

## Commit Hashes

- `fc11027fc641eea13d08c73655d6a84a7bf24481` — refactor(handlers): remove quota retry logic, immediate terminal 429

## Test Summary

### Tests Run

| Test | Result |
|------|--------|
| `test_retries_on_429_then_succeeds` | PASSED |
| `test_streaming_429_retries_then_succeeds` | PASSED |
| `test_streaming_429_forwards_retry_after_header` | PASSED |
| `test_non_streaming_429_after_max_retries` | PASSED |
| `test_multiple_429s_then_success` | PASSED |
| `test_non_200_non_429_non_5xx_forwarded` | PASSED |
| `test_quota_exceeded_429_not_retried` | FAILED (expected) |

**Summary:** 6 passed, 1 failed. The failing test is the quota-specific test that was testing the old quota retry behavior. Per the brief, "Quota-specific tests will be updated in Task 5."

## Concerns

1. **Quota-specific test failure:** `test_quota_exceeded_429_not_retried` fails because it was testing the old quota retry behavior (expecting 2 calls due to cooldown retry). The new behavior immediately returns the terminal 429 without retry, so only 1 upstream call is made. This test will be updated in Task 5 per the brief.

## Implementation Details

### Changes Made

1. **Removed quota retry variables** (around line 410):
   - Removed `quota_retries = 0`
   - Removed `quota_max = getattr(limiter, "quota_max_retries", 0)`
   - Removed `quota_cooldown = getattr(limiter, "quota_retry_cooldown", 1800)`

2. **Streaming 429 handling** (around line 560):
   - Replaced the quota retry logic (20+ lines of cooldown sleep and retry) with immediate return:
     - Changed `error_reason` from `"upstream_quota_exceeded"` to `"upstream_429_terminal"`
     - Changed log message from `"upstream quota exceeded, not retrying"` to `"upstream terminal 429, not retrying"`
     - Removed the cooldown sleep and retry loop entirely
     - Returns HTTP 429 immediately with the upstream body and headers

3. **Non-streaming 429 handling** (around line 800):
   - Same changes as streaming: immediate return for terminal 429, no cooldown retry

### Unchanged Behavior

- `should_retry_429()` function remains unchanged — it still classifies 429s as retryable or terminal
- Generic 429 retry logic (for retryable 429s) remains unchanged — still uses backoff and max retries
- The proxy still distinguishes between terminal 429s (immediate return) and retryable 429s (normal retry path)

### Verification

- No linter errors in `handlers.py`
- No remaining references to `quota_retries`, `quota_max`, or `quota_cooldown` in the codebase
- All generic 429 retry tests pass

## Files Modified

- `dashscope_proxy_lib/handlers.py` — 1 file changed, 5 insertions(+), 32 deletions(-)