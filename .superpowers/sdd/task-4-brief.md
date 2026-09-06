### Task 4: Cross-provider failover on 429/5xx/timeout

**Files:**
- Modify: `dashscope_proxy_lib/handlers.py` (`handle_request` retry loop → provider rotation on retryable upstream failures)
- Test: `tests/test_integration.py` (append failover tests)

**Interfaces:**
- Consumes: `router.get_providers_for_model()` from Task 2; existing `should_retry_429`, `_compute_backoff`, per-provider `limiter`.
- Produces: session entry gains `attempted_providers: list[str]`; structured failover log per hop.

- [ ] **Step 1: Write the failing test**

Three tests (verbatim from plan, placed in `tests/test_integration.py` after `TestPinnedRoutingHandler`):

1. `test_overlap_failover_on_502`: Overlapping model on tertiary+quaternary, tertiary returns 502 → handler fails over to quaternary → 200.
2. `test_pinned_request_does_not_fail_over`: Pinned `openlux/model` returning 502 → 502 to client, no cross-provider retry.
3. `test_upstream_400_does_not_fail_over`: Upstream 400 on first provider → 400 to client, no cross-provider retry (400 is terminal).

See plan lines 346-433 for exact test code.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_integration.py::test_overlap_failover_on_502 tests/test_integration.py::test_pinned_request_does_not_fail_over tests/test_integration.py::test_upstream_400_does_not_fail_over -v`
Expected: FAIL (first test gets 502, no failover exists; collection relies on `asyncio_mode = "auto"` so plain `async def` works)

- [ ] **Step 3: Write minimal implementation**

This is a significant refactoring of `handle_request`. Goal: add provider rotation on retryable failures while preserving all existing behavior for single-provider and pinned cases.

**Key data structures to add (after provider selection at line ~237):**

```python
router = get_provider_router()
from dashscope_proxy_lib.request_transform import split_provider_prefix
pinned_name, bare_name = split_provider_prefix(model_name or "")

# Candidate provider list
if pinned_name is not None:
    candidates = [getattr(router, pinned_name)]
else:
    overlap = router.get_providers_for_model(model_name or "")
    if overlap:
        candidates = overlap
    else:
        candidates = [router.get_provider_for_model(model_name or "")]

provider = candidates[0]
provider_name = provider.name
attempted_providers: list[str] = [provider_name]
session_entry["attempted_providers"] = attempted_providers
candidate_idx = 0
```

**Rotation rules:**

On a terminal-per-provider failure (conditions below), advance `candidate_idx` to the next provider in `candidates` that is available (`is_available` or circuit not open). If no candidates remain, return 502.

Conditions that trigger rotation (within the existing retry loops):
1. **5xx failures:** After `retry_5xx > MAX_5XX_RETRIES` for the current provider.
2. **Retryable 429:** After `retry > limiter.max_retries` for the current provider AND `should_retry_429(error_body)` is true.
3. **Connection errors / timeouts:** After the existing `retry > limiter.max_retries` for client errors.

Conditions that DO NOT trigger rotation (terminal for the request):
1. **Upstream 4xx (non-429):** Return immediately with the 4xx response.
2. **Quota-429:** When `should_retry_429(error_body)` is false (quota exceeded, non-retryable) — keep the existing same-provider cooldown path, no rotation.
3. **Pinned requests:** `len(candidates) == 1`, so no rotation possible.

**On rotation:**
- Log: `_log(logging.WARNING, "provider failover", request_id=request_id, model=model_name, from_provider=current_provider.name, to_provider=next_provider.name, reason="..." )`
- Append `next_provider.name` to `attempted_providers`.
- Update `provider`, `provider_name`, `limiter` (via `get_limiter_for_provider(provider_name)`).
- Rebuild `target_url` and `headers` for the new provider.
- Reset per-provider retry counters (`retry_5xx = 0`, `retry = 0`) for the new provider.
- Continue the outer retry loop.

**Streaming:**
- Rotation decisions happen on upstream status BEFORE `resp.prepare()` in the streaming branch.
- Once `stream_prepared = True` (after `await resp.prepare(request)`), no rotation on mid-stream failures — use the existing streaming-error path.

**Queue/TPM:**
- Queue wait (`await wait_for_slot`) happens once before the loop (unchanged).
- TPM reservation (`await limiter.reserve_tokens`) happens once per request, not per-provider-hop. Use the `limiter` for the first provider; on rotation, refund any remaining reserved tokens to the old limiter and reserve from the new limiter. (If the implementation keeps `tokens_reserved` as a flag, be careful to refund to the correct limiter before switching.)
- Token reconcile/refund happens once at the end (unchanged).

**Helper function pattern:**

Inside `handle_request`, a local helper `_advance_candidate()` returns `True` if a next provider was selected, `False` if exhausted:

```python
def _advance_candidate() -> bool:
    nonlocal candidate_idx, provider, provider_name, limiter, retry, retry_5xx
    # Skip providers with open circuits
    while candidate_idx + 1 < len(candidates):
        next_provider = candidates[candidate_idx + 1]
        next_limiter = get_limiter_for_provider(next_provider.name)
        if not next_limiter.circuit_is_open():
            candidate_idx += 1
            provider = next_provider
            provider_name = provider.name
            limiter = next_limiter
            attempted_providers.append(provider_name)
            retry = 0
            retry_5xx = 0
            _log(logging.WARNING, "provider failover",
                 request_id=request_id, model=model_name,
                 from_provider=attempted_providers[-2], to_provider=provider_name,
                 reason="upstream failure")
            return True
        candidate_idx += 1  # Skip this candidate, try the next
    return False
```

Call `_advance_candidate()` when a provider's retries are exhausted (5xx or retryable-429 or connection errors). If it returns `False`, return 502.

**Implementation order:**

1. Add candidate list setup after Task 3's pin validation (integrate with existing `pinned_name` logic).
2. Add `attempted_providers` to `session_entry`.
3. In the streaming branch (lines 400-630), after checking upstream status (429/5xx/4xx), on terminal-per-provider status, call `_advance_candidate()` and `continue` the outer loop. For mid-stream failures (after `stream_prepared`), keep existing error path.
4. In the non-streaming branch (lines 640-790), similarly check after status and call `_advance_candidate()`.
5. Ensure TPM reservation handling across hops (refund old limiter, reserve from new).

**CAUTION:**
- `get_limiter_for_provider` is a method on `MultiProviderRateLimiter`, accessed via `rate_limiter.get_limiter_for_provider(provider_name)`. The `limiter` variable used for TPM operations must be updated on rotation.
- `target_url` and `headers` must be rebuilt for the new provider (copy the URL construction logic from lines 310-340 and header setup from lines 340-345).
- Keep the diff minimal and preserve all existing early-exit paths (client disconnect, queue full, circuit open, shutdown).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_integration.py -v`
Expected: PASS (full integration file; existing tests should not regress; 3 new tests pass)

- [ ] **Step 5: Commit**

```bash
git add dashscope_proxy_lib/handlers.py tests/test_integration.py
git commit -m "feat: cross-provider failover for overlapping models"
```