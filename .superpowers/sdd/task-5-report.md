# Task 5 Report — Wave 3 try_admit + record_completion

**Status:** DONE  
**Branch:** `perf/measure-first-hardening`  
**Commit:** (pending) — perf: coalesce rate limiter admit and completion under fewer locks

## Deliverables

| Item | Path | Notes |
| --- | --- | --- |
| `try_admit` | `dashscope_proxy_lib/rate_limiter.py` | Same checks as `can_proceed` + TPM reserve under one `asyncio.Lock`; does not charge RPM; does not update `last_request_time` (Task 9) |
| `record_completion` | `dashscope_proxy_lib/rate_limiter.py` | One lock: reconcile + RPM/metrics + circuit branch + model stats + body sizes |
| Handler happy-path | `dashscope_proxy_lib/handlers.py` | Post-queue `try_admit`; stream/non-stream success → `record_completion`; failover still `refund`/`reserve`; failures keep `refund`/`record_circuit_failure` |
| Unit tests | `tests/test_units.py` | `TestTryAdmitAndRecordCompletion` |
| DOX | `AGENTS.md`, `dashscope_proxy_lib/AGENTS.md`, `tests/AGENTS.md` | Document new APIs + handler wiring |

## Tests

- Focused units (`RateLimiter` / `TokenWindow` / `try_admit` / `record_completion` / `TokenReconcil`): **56 passed**
- `tests/test_integration.py`: **70 passed** (1 pre-existing AsyncMock warning)
- Bench `concurrent_burst`: **50/50 success**, ~231 RPS wall (~216 ms)

## Concerns

- Docstring mentions fail-closed lock timeout; no timeout wrapper yet (same as other limiter methods).
- `record_completion` still nests `_thread_lock` inside `asyncio.Lock` (Task 6 will split).
- Failover still uses separate `reserve_tokens` after refund (intentional; not admit-gated).
- Post-queue deny body still says "TPM quota exceeded while queued" even for RPM/RPS admit denies.
