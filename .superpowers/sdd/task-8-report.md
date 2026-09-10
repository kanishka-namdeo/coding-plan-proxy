# Task 8 Report — Wave 5 Wire circuit HALF_OPEN probe

**Status:** DONE

**Branch:** `perf/measure-first-hardening`

## Commits

- `57f8355` — `fix: enforce single circuit probe after cooldown`

## Deliverables

| Item | Path | Notes |
| --- | --- | --- |
| Probe API | `dashscope_proxy_lib/rate_limiter.py` | `circuit_state` / `circuit_probe_in_flight`; `can_attempt_probe()` claims single HALF_OPEN probe after cooldown; success/failure clear probe flag |
| Handler gate | `dashscope_proxy_lib/handlers.py` | Pre-upstream admit uses `can_attempt_probe()` instead of lock-free `circuit_is_open()` |
| Unit tests | `tests/test_units.py` | `test_can_attempt_probe_claims_half_open`, `test_probe_success_closes` |
| Integration | `tests/test_integration.py` | `test_single_probe_after_cooldown` — 5 concurrent, 1 upstream hit |
| DOX | root / lib / tests `AGENTS.md` | Document probe gate + tests |

## Tests

- RED then GREEN: unit probe tests + integration single-probe
- `py -m pytest tests/test_units.py::TestCircuitBreaker tests/test_integration.py::TestCircuitBreakerCleanup -v` — **8 passed**
- `py -m pytest tests/test_integration.py -q` — **72 passed**

## Concerns

- Brief said helpers were “existing”; they were documented but not implemented — added `can_attempt_probe` + state fields in this task (allowed “tweaks”).
- Probe-denied path still returns 503 without attempting provider failover (same as prior `circuit_is_open` gate).
- Failover skip still uses lock-free `circuit_is_open()`; after cooldown a HALF_OPEN provider may be selected, then denied at the probe gate (safe, may burn a failover slot).

## Follow-up — Critical probe stuck fix

**Commit:** `ed7549c` — `fix: clear circuit probe claim on abort and retry paths`

### Problem
After `can_attempt_probe()` claimed HALF_OPEN, several handler paths left `circuit_probe_in_flight` stuck:
1. Retryable 429 → `continue` → next loop iteration denied forever (HALF_OPEN + in_flight)
2. Terminal 429 / stream 4xx / disconnect / CancelledError after claim without clearing probe

### Fix
- Track `holding_probe` after a successful claim; probe holder retries without reclaiming
- Add `RateLimiter.release_probe()` — clears in-flight and reopens HALF_OPEN → OPEN + cooldown (no-op if already cleared)
- Call `release_probe()` in handler `finally` and before provider failover
- Mid-request `record_circuit_failure` still clears probe; holder re-admits or gets 503 on next loop

### Tests
- Unit: `test_failed_probe_via_record_circuit_failure_reopens`, `test_release_probe_clears_half_open`
- Integration: `test_terminal_429_after_probe_claim_does_not_stick`, `test_disconnect_after_probe_claim_does_not_stick`
- `py -m pytest tests/test_units.py::TestCircuitBreaker tests/test_integration.py::TestCircuitBreakerCleanup -v` — **12 passed**
