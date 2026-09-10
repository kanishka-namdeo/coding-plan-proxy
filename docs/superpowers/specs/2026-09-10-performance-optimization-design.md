# Performance Optimization (Measure-First Phased Hardening)

**Date:** 2026-09-10  
**Status:** Approved for implementation planning  
**Approach:** Measure-first phased hardening

## Goal

Reduce proxy-local latency, raise sustainable concurrent throughput, and lower CPU/memory under load — prioritized by **measured impact**. Keep full session-log field fidelity. Include semantic hardening (circuit probe tracking, admission-time RPS) when it improves load behavior, covered by tests.

Upstream provider RTT still dominates end-to-end wall time. Success is measured on **proxy overhead**, concurrency under the rate limiter, and resource cost — not raw chat completion latency to DashScope/etc.

## Constraints (from product choices)

- Optimize **latency + throughput + resources**, ranked by measurement.
- **Full fidelity** session/observability fields: do not drop, truncate, or sample away session-log schema fields. I/O path may change (async queue, batched flush).
- **Full hardening** in scope: wire `HALF_OPEN` / single-probe after cooldown; admission-time RPS shaping; limiter API coalesce that preserves accounting meaning.

## Architecture

Keep the existing aiohttp multi-provider proxy. No new services, no process split for the TUI. Changes stay in `dashscope_proxy_lib/` (and root facade / TUI only where required), plus a new lightweight benchmark harness.

**Control loop**

1. Capture baseline metrics (Wave 0).
2. Apply one optimization wave.
3. Re-measure; keep only if impact is real and `pytest` stays green.
4. Proceed to the next wave by remaining measured impact (default order below; reorder if measurement contradicts).

**Invariant walls (must not weaken)**

- TPM reserve → reconcile / refund (including failover handoff: refund old limiter before reserve on next).
- Queue / pending / deadline / disconnect semantics.
- Fail-closed on limiter lock timeout.
- Full session-log field fidelity (metadata schema unchanged; bodies remain out of session logs as today — metadata only).
- Security header stripping; upstream body bytes match post-transform validation (role map, model normalize, pin strip).
- Non-429 4xx remains terminal (no failover).

**Intentional semantic changes (Wave 5)**

- Wire circuit `HALF_OPEN` / `can_attempt_probe` so only one probe runs after cooldown (today all traffic can resume → thundering herd).
- Update RPS spacing at **admission** (`last_request_time` on admit), not only on completion, to reduce waiter stampedes.

## Measurement (Wave 0)

**Harness:** Local script under `benchmarks/` (or `scripts/`) driving a mock aiohttp upstream that returns fixed JSON / SSE with near-zero think time, so metrics reflect proxy-local cost.

**Primary metrics**

| Metric | Definition |
|--------|------------|
| Proxy overhead p50/p95 | Client total time − mock think-time |
| Stream TTFB p50/p95 | Time to first SSE chunk through the proxy |
| Sustainable RPS | Max success rate before 503 / queue / spacing collapse |
| Resource | Process RSS + event-loop lag samples (schedule delay) |
| Correctness gate | `py -m pytest tests/` green after each keep |

**Scenario matrix**

1. Non-stream chat, small body  
2. Non-stream chat, large body (tools + long messages) — JSON path  
3. Stream chat, many chunks — chunk loop / `_cfg`  
4. Concurrent burst (N parallel) — limiter locks + session I/O  

**Method:** Warmup, then N iterations; report median/p95. Prefer headless for ceiling; optional TUI-on run for contention comparison. Write JSON/CSV under `benchmarks/results/` (gitignored). Prefer stdlib + existing aiohttp; avoid new heavy deps.

**Keep rule:** Keep a wave if it improves a primary metric meaningfully (≥ ~5% on the targeted scenario) without regressing others beyond noise, and tests pass. Otherwise drop/revert.

## Optimization waves

Ordered by expected impact; each wave is one keep/revert unit.

### Wave 0 — Benchmark harness

Add mock-upstream driver + metric reporter. Capture baseline for the matrix. No production behavior change.

### Wave 1 — Ingress JSON

Parse once; estimate tokens from the already-parsed dict (extend `estimate_tokens_for_request` or add a dict path). Dump body **once** after all transforms including pin strip. Eliminate redundant `json.loads` / `json.dumps` on the hot path.

**Target files:** `handlers.py`, `token_utils.py`, related unit tests.

### Wave 2 — Session I/O (full fidelity)

Keep every session-log field. Stop awaiting disk flush on the handler critical path: bounded async queue / fire-and-forget with backpressure. Batch or interval `flush` instead of per-line flush. Optionally skip attaching `TUILogHandler` when headless (no consumer). Optional env escape hatch for sync flush under emergency debugging; default remains full-fidelity async path.

**Target files:** `session_log.py`, `handlers.py`, `logging_config.py`, `server.py`, session-log tests.

**Acceptable tradeoff:** Last few entries may be missing after hard kill; graceful shutdown must drain/flush.

### Wave 3 — Rate-limiter lock coalesce

- Completion API under one `async with _lock`: reconcile + `record_request` + circuit + model stats + body sizes.
- Prefer `try_admit(estimated)` combining check + reserve (RPM still recorded on success only).
- Fix TUI / `_thread_lock` nesting so `status()` snapshots cannot stall the event loop while async code holds `asyncio.Lock`.
- Preserve fail-closed timeouts and TPM accounting.

**Target files:** `rate_limiter.py`, `handlers.py`, `queue.py` (if admit API changes), limiter unit/integration tests.

### Wave 4 — Streaming hot path

Cache `_cfg` values per request/stream. Use one idle deadline/timeout strategy instead of per-chunk `_cfg` + new `asyncio.timeout`. Preserve idle disconnect semantics.

**Target files:** `handlers.py`, stream-related integration tests.

### Wave 5 — Full hardening (semantic)

- Wire `can_attempt_probe` / `HALF_OPEN`; only one probe after cooldown; success → CLOSED; failed probe → OPEN + cooldown.
- Admission-time RPS shaping.
- If still measured as hot: avoid O(n) `wait_seconds_for` under the outer lock; OrderedDict LRU for model stats; cheaper `status()` snapshots; TUI incremental/cached session-log read (addresses documented full-file re-scan).

**Target files:** `rate_limiter.py`, `handlers.py`, `proxy_tui.py` (session log read), new circuit/RPS tests.

### Out of scope

- Separate TUI process / IPC redesign  
- Latency- or cost-based routing / load balancing  
- Replacing aiohttp  
- Changing default session-log schema fields  
- Full load suite in CI (benchmarks remain manual/dev)

## Data flow (happy path after waves)

1. Accept → read body once → `json.loads` once → validate/transform/pin-strip on dict.  
2. Estimate tokens from dict; `json.dumps` once to upstream bytes.  
3. `try_admit` (or equivalent) under one limiter lock; queue wait loop uses same admit semantics.  
4. Forward upstream; stream or buffer response as today.  
5. Single completion update under one lock (reconcile + metrics + circuit).  
6. Enqueue session-log entry (non-blocking w.r.t. response return); writer flushes on interval/batch/shutdown.

## Error handling

- Unchanged for client validation, upstream 4xx/429/5xx retry/failover, disconnect refunds — except Wave 5 circuit probe admission.  
- Session log write failures: log ERROR; must not fail the client response after upstream success.  
- Limiter lock timeout: remain fail-closed.  
- Circuit OPEN: reject/failover per existing handler rules; after cooldown only the probe request proceeds until success/failure resolves HALF_OPEN.

## Files and contracts

Expected touch set:

| Area | Files |
|------|--------|
| Harness | `benchmarks/` (new), `.gitignore` for `benchmarks/results/` |
| Ingress | `dashscope_proxy_lib/handlers.py`, `token_utils.py` |
| Session / logging | `session_log.py`, `logging_config.py`, `server.py` |
| Limiter / queue | `rate_limiter.py`, `queue.py` |
| Streaming | `handlers.py` |
| TUI (Wave 5 optional) | `proxy_tui.py` |
| Facade | `dashscope_proxy.py` only if new public symbols needed |
| Tests | `tests/test_units.py`, `tests/test_integration.py`, `tests/conftest.py` as needed |
| Docs / DOX | Spec here; update `AGENTS.md` / child DOX when contracts change (e.g. admit API, circuit probe behavior, session flush semantics) |

Facade patching convention (`_cfg()`, patch via `dashscope_proxy.*`) remains mandatory.

## Testing and verification

**Per-wave gate:** `py -m pytest tests/` before keep.

**Wave-specific coverage**

| Wave | Tests |
|------|--------|
| 0 | Harness smoke: mock up, one request, metrics artifact written |
| 1 | Dict estimate ≡ bytes estimate for representative bodies; pin-strip single dump still strips prefix |
| 2 | Session entry schema unchanged; entries appear under load; close/shutdown drains; document hard-kill window |
| 3 | TPM reserve/reconcile/refund + failover handoff; concurrent admit/completion; status shape stable for TUI/`/v1/proxy/status` |
| 4 | Stream idle timeout still fires; chunk forwarding unchanged |
| 5 | Single probe in HALF_OPEN; failed probe re-opens; success closes; RPS spaced at admission |

**Regression watchlist:** `TestTokenReconciliation`, queue/TPM reservation, circuit cleanup, `TestSessionLogWriter`, multi-provider status / `recent_latencies`, streaming errors / client disconnect.

**Verification commands**

```powershell
py -m pytest tests/test_units.py tests/test_integration.py
py -m pytest tests/
# Manual: run Wave 0 harness before/after each wave
```

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Batched session flush loses last lines on hard kill | Document; flush on graceful shutdown/`close()` |
| Coalesced limiter APIs break TPM/circuit accounting | Wave-specific unit tests; fail-closed timeouts unchanged |
| Probe / admission RPS changes observed throughput | Explicit Wave 5; measure before/after; tests define new contract |
| TUI lock reorder causes torn reads | Snapshot under one short critical section; keep status dict shape |
| Benchmark noise / false wins | Warmup, fixed matrix, ≥~5% keep rule, multiple runs |
| Scope creep | Out-of-scope list; independent keep/revert waves |

## Success criteria

- Meaningful improvement on targeted primary metrics (≥~5% where applicable) across kept waves.  
- Full session-log field fidelity preserved.  
- `pytest` green.  
- Circuit probe and admission-time RPS covered by tests and reflected in DOX if behavior contracts change.

## Rollout

Land Wave 0 first, then Waves 1→5 unless measurement reorders. Prefer small commits/PRs per wave. No production feature flag required for Waves 1/3/4; Wave 2 may expose an emergency sync-flush env escape hatch.

## Research grounding

Implementation plan `docs/superpowers/plans/2026-09-10-performance-optimization.md` includes a **Research grounding** table citing:

- aiohttp 3.14 ClientSession / TCPConnector docs (session reuse; `limit_per_host` defaults)
- Python asyncio-dev + Logging Cookbook (`QueueHandler`/`QueueListener` for non-blocking logs)
- asyncio lock vs threading.Lock guidance (do not block the loop)
- HALF_OPEN single-probe circuit-breaker practice (thundering-herd mitigation)
- orjson as optional only after redundant-JSON removal is measured

Do not add `orjson` or loosen connector limits unless Wave 0/1 benches show those bottlenecks.
