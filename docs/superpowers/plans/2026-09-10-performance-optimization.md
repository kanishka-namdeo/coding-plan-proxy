# Performance Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut proxy-local overhead and raise concurrency via measure-first waves: benchmark harness, JSON ingress, full-fidelity session I/O, limiter lock coalesce, streaming hot path, then circuit probe + admission-time RPS hardening.

**Architecture:** Keep the aiohttp multi-provider proxy. Add a local mock-upstream benchmark harness. Optimize hot paths in `dashscope_proxy_lib/` without dropping session-log fields. Coalesce rate-limiter lock round-trips and wire the existing but unused HALF_OPEN probe path. Re-measure after each wave; keep only if metrics improve (~≥5% on the targeted scenario) and `pytest` stays green.

**Tech Stack:** Python 3.14, aiohttp 3.x, pytest / pytest-asyncio, stdlib only for benchmarks (no new heavy deps).

**Spec:** `docs/superpowers/specs/2026-09-10-performance-optimization-design.md`

## Global Constraints

- Full session-log field fidelity — never drop/truncate schema fields; I/O path may change
- TPM reserve → reconcile/refund (incl. failover handoff) must stay correct
- Fail-closed on limiter lock timeout
- Facade patching: resolve constants via `_cfg()` / patch `dashscope_proxy.*`
- Prefer `py -m pytest` on Windows
- Keep rule: meaningful metric win (~≥5% on targeted scenario) + green tests, else revert the wave
- Out of scope: separate TUI process, latency-based routing, replacing aiohttp, CI load suite

## File map

| File | Responsibility |
|------|----------------|
| `benchmarks/run_bench.py` | Mock upstream + client matrix + JSON metrics writer |
| `benchmarks/README.md` | How to run / interpret results |
| `benchmarks/results/` | Gitignored output |
| `dashscope_proxy_lib/token_utils.py` | Dict + bytes token estimate |
| `dashscope_proxy_lib/handlers.py` | Single dump after transforms; admit/completion APIs; stream timeout cache; circuit probe gate |
| `dashscope_proxy_lib/session_log.py` | Non-blocking enqueue, batched flush, shutdown drain |
| `dashscope_proxy_lib/logging_config.py` | Optional TUI handler attach |
| `dashscope_proxy_lib/server.py` | Pass `enable_tui_handler` into logging config |
| `dashscope_proxy_lib/rate_limiter.py` | `try_admit`, `record_completion`, lock/TUI fix, admission RPS, probe wiring |
| `dashscope_proxy_lib/queue.py` | Wait loop compatible with admit semantics |
| `proxy_tui.py` | Incremental session-log tail (Wave 5) |
| `dashscope_proxy.py` | Re-export any new public symbols |
| `tests/test_units.py` / `test_integration.py` | Wave-specific tests |
| `.gitignore` | `benchmarks/results/` |
| `AGENTS.md` (root + lib) | Update when contracts change (admit/completion, circuit probe, session flush) |

---

### Task 1: Wave 0 — Benchmark harness

**Files:**
- Create: `benchmarks/run_bench.py`
- Create: `benchmarks/README.md`
- Modify: `.gitignore`
- Test: harness self-smoke (run script once)

**Interfaces:**
- Consumes: None (standalone; starts its own aiohttp proxy app + mock upstream)
- Produces: `benchmarks/results/<timestamp>.json` with per-scenario p50/p95 overhead, stream TTFB, burst success rate, optional RSS

- [ ] **Step 1: Add gitignore entry**

Append to `.gitignore`:

```
# Benchmark outputs
benchmarks/results/
```

- [ ] **Step 2: Create `benchmarks/run_bench.py`**

Write this complete script (mirror `tests/test_integration.py` proxy wiring):

```python
"""Local proxy-overhead benchmarks against a mock upstream.

Usage:
  py benchmarks/run_bench.py
  py benchmarks/run_bench.py --out benchmarks/results/baseline.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import ClientSession, TCPConnector, web

import dashscope_proxy


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[idx]


def high_limits() -> dict:
    return {
        "rpm_limit": 1_000_000,
        "tpm_limit": 100_000_000,
        "safety_factor": 1.0,
        "max_queue_size": 10_000,
        "max_retries": 1,
        "base_backoff": 0.01,
    }


async def start_mock_upstream() -> tuple[web.AppRunner, str]:
    async def chat(request: web.Request):
        body = await request.json()
        if body.get("stream"):
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"}
            )
            await resp.prepare(request)
            for i in range(20):
                await resp.write(
                    f'data: {{"choices":[{{"delta":{{"content":"{i}"}}}}]}}\n\n'.encode()
                )
            await resp.write(
                b'data: {"usage":{"prompt_tokens":10,"completion_tokens":20,'
                b'"total_tokens":30}}\n\n'
            )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp
        return web.json_response(
            {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def start_proxy(mock_base: str) -> tuple[web.AppRunner, str, object]:
    os.environ["SESSION_LOG_ENABLED"] = "0"
    dashscope_proxy.TARGET_BASE = mock_base
    dashscope_proxy.DASHSCOPE_API_KEY = "bench-key"
    cfg = high_limits()
    limiter = dashscope_proxy.MultiProviderRateLimiter(cfg)
    app = dashscope_proxy.create_app()
    app["rate_limiter"] = limiter
    app["shutting_down"] = asyncio.Event()
    app["session_log"] = None
    timeout = dashscope_proxy.aiohttp.ClientTimeout(total=30, connect=5)
    connector = TCPConnector(limit=200, ttl_dns_cache=300)
    app["client_session"] = ClientSession(timeout=timeout, connector=connector)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}", app


def small_body() -> dict:
    return {
        "model": "qwen3-coder-plus",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }


def large_body() -> dict:
    msg = "x" * 8000
    return {
        "model": "qwen3-coder-plus",
        "messages": [{"role": "user", "content": msg}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "tool_%d" % i,
                    "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
                },
            }
            for i in range(20)
        ],
        "stream": False,
    }


def stream_body() -> dict:
    b = small_body()
    b["stream"] = True
    return b


async def timed_post(session: ClientSession, url: str, body: dict) -> float:
    t0 = time.perf_counter()
    async with session.post(
        f"{url}/v1/chat/completions",
        json=body,
        headers={"Authorization": "Bearer bench"},
    ) as resp:
        if body.get("stream"):
            async for _ in resp.content.iter_any():
                pass
        else:
            await resp.read()
        assert resp.status == 200, await resp.text()
    return (time.perf_counter() - t0) * 1000.0


async def run_scenario(
    session: ClientSession, proxy: str, name: str, body: dict, n: int, warmup: int
) -> dict:
    for _ in range(warmup):
        await timed_post(session, proxy, body)
    samples: list[float] = []
    for _ in range(n):
        samples.append(await timed_post(session, proxy, body))
    return {
        "n": n,
        "p50_ms": round(pct(samples, 50), 3),
        "p95_ms": round(pct(samples, 95), 3),
        "mean_ms": round(sum(samples) / len(samples), 3),
    }


async def run_burst(session: ClientSession, proxy: str, n: int = 50) -> dict:
    body = small_body()
    t0 = time.perf_counter()
    results = await asyncio.gather(
        *[timed_post(session, proxy, body) for _ in range(n)],
        return_exceptions=True,
    )
    elapsed = (time.perf_counter() - t0) * 1000.0
    ok = [r for r in results if isinstance(r, float)]
    return {
        "n": n,
        "success": len(ok),
        "p50_ms": round(pct(ok, 50), 3) if ok else None,
        "p95_ms": round(pct(ok, 95), 3) if ok else None,
        "wall_ms": round(elapsed, 3),
        "rps": round(len(ok) / (elapsed / 1000.0), 2) if elapsed else 0,
    }


async def main(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    mock_runner, mock_base = await start_mock_upstream()
    proxy_runner, proxy_base, app = await start_proxy(mock_base)
    results = {"mock_base": mock_base, "proxy_base": proxy_base, "scenarios": {}}
    try:
        async with ClientSession() as session:
            results["scenarios"]["nonstream_small"] = await run_scenario(
                session, proxy_base, "nonstream_small", small_body(), 50, 5
            )
            results["scenarios"]["nonstream_large"] = await run_scenario(
                session, proxy_base, "nonstream_large", large_body(), 30, 3
            )
            results["scenarios"]["stream_many_chunks"] = await run_scenario(
                session, proxy_base, "stream_many_chunks", stream_body(), 30, 3
            )
            results["scenarios"]["concurrent_burst"] = await run_burst(
                session, proxy_base, 50
            )
    finally:
        await app["client_session"].close()
        await proxy_runner.cleanup()
        await mock_runner.cleanup()
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results["scenarios"], indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("benchmarks/results") / f"run-{int(time.time())}.json",
    )
    args = parser.parse_args()
    asyncio.run(main(args.out))
```

If `dashscope_proxy.aiohttp` is not on the facade, import `aiohttp` directly for `ClientTimeout`. Fix any `TARGET_BASE` patching to match how integration tests retarget upstream (patch facade attributes used by `_cfg("TARGET_BASE")`).

- [ ] **Step 3: Write `benchmarks/README.md`**

Document: run command, scenario list, keep rule (~≥5%), headless preferred, results are gitignored.

- [ ] **Step 4: Smoke the harness**

Run: `py benchmarks/run_bench.py --out benchmarks/results/smoke.json`  
Expected: exit 0, JSON file with four scenario keys and numeric p50/p95.

- [ ] **Step 5: Capture baseline**

Run once more as `benchmarks/results/baseline.json` (local only). Note p50/p95 for later comparison (do not commit results).

- [ ] **Step 6: Commit**

```bash
git add .gitignore benchmarks/run_bench.py benchmarks/README.md
git commit -m "bench: add mock-upstream proxy overhead harness"
```

---

### Task 2: Wave 1 — Ingress JSON (estimate from dict, dump once)

**Files:**
- Modify: `dashscope_proxy_lib/token_utils.py`
- Modify: `dashscope_proxy_lib/handlers.py` (transform → pin strip → single dump → estimate)
- Modify: `dashscope_proxy.py` (re-export if new public name)
- Test: `tests/test_units.py`

**Interfaces:**
- Consumes: existing `estimate_tokens_for_request(body_bytes: bytes) -> int`
- Produces:
  - `estimate_tokens_for_body(body: dict) -> int` (core logic)
  - `estimate_tokens_for_request(body_bytes: bytes) -> int` becomes thin wrapper: loads JSON then calls `estimate_tokens_for_body`, still returns 100 on bad JSON

- [ ] **Step 1: Write failing tests**

Add to `tests/test_units.py`:

```python
class TestEstimateTokensDict:
    def test_dict_matches_bytes(self, dashscope_module):
        payload = {
            "messages": [{"role": "user", "content": "Hello world, this is a test"}],
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
            "system": "be brief",
        }
        body_bytes = json.dumps(payload).encode()
        from_bytes = dashscope_module.estimate_tokens_for_request(body_bytes)
        from_dict = dashscope_module.estimate_tokens_for_body(payload)
        assert from_dict == from_bytes

    def test_dict_empty_messages_floor(self, dashscope_module):
        assert dashscope_module.estimate_tokens_for_body({"messages": []}) == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_units.py::TestEstimateTokensDict -v`  
Expected: FAIL (`estimate_tokens_for_body` missing)

- [ ] **Step 3: Implement `estimate_tokens_for_body` and refactor bytes wrapper**

In `dashscope_proxy_lib/token_utils.py`:

```python
def estimate_tokens_for_body(body: dict) -> int:
    """Rough estimate from an already-parsed request body for TPM planning."""
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return 100

    total_chars = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            total_chars += sum(
                len(p.get("text", ""))
                for p in content
                if isinstance(p, dict)
            )

    for field in ("system", "developer"):
        value = body.get(field)
        if isinstance(value, str):
            total_chars += len(value)

    tools = body.get("tools", [])
    if isinstance(tools, list):
        try:
            total_chars += len(json.dumps(tools))
        except (TypeError, ValueError):
            total_chars += len(tools) * 200

    return max(100, total_chars // 4)


def estimate_tokens_for_request(body_bytes: bytes) -> int:
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return 100
    if not isinstance(body, dict):
        return 100
    return estimate_tokens_for_body(body)
```

Re-export `estimate_tokens_for_body` from `dashscope_proxy.py` (`__all__` + import).

- [ ] **Step 4: Reorder handler: transform + pin strip, then one dump, then estimate from dict**

In `handlers.py`, after validation:

1. `map_developer_to_system` + `normalize_model_name` on `body`.
2. Run pin/prefix logic that mutates `body["model"]` **before** any `json.dumps`.
3. **Remove** the early `json.dumps` at the old ~324 line and the second dump at pin-strip (~390).
4. After pin handling settles `body`, do a single:

```python
try:
    body_bytes = json.dumps(body).encode()
except (TypeError, ValueError) as e:
    # same 400 unserializable response as today
    ...
estimated_tokens = estimate_tokens_for_body(body) if body is not None else 0
```

If `body` is None (non-POST / empty), keep `estimated_tokens = 0` / existing behavior.

Import `estimate_tokens_for_body` alongside existing token utils import in handlers.

- [ ] **Step 5: Run unit + a focused integration subset**

Run:

```powershell
py -m pytest tests/test_units.py::TestEstimateTokensDict tests/test_units.py::TestTokenExtraction -v
py -m pytest tests/test_integration.py -k "pin or chat or proxy" -v --tb=short
```

Expected: PASS

- [ ] **Step 6: Re-bench vs baseline (manual)**

Run harness; confirm `nonstream_large` p50/p95 improves or is within noise. If large regression, investigate before commit.

- [ ] **Step 7: Commit**

```bash
git add dashscope_proxy_lib/token_utils.py dashscope_proxy_lib/handlers.py dashscope_proxy.py tests/test_units.py
git commit -m "perf: estimate tokens from dict and dump request body once"
```

---

### Task 3: Wave 2 — Session log non-blocking + batched flush

**Files:**
- Modify: `dashscope_proxy_lib/session_log.py`
- Modify: `dashscope_proxy_lib/handlers.py` (`finally` + `_maybe_flush_session_log`)
- Test: `tests/test_units.py` (`TestSessionLogWriter`, edge cases)

**Interfaces:**
- Consumes: existing `SessionLogWriter.log` / `log_async` / `close`
- Produces:
  - `log_async(entry)` returns after enqueue (does not wait for disk flush)
  - Internal bounded queue; single writer thread/executor drains and flushes every `SESSION_LOG_FLUSH_INTERVAL` seconds or every `SESSION_LOG_FLUSH_EVERY` lines (defaults: 0.25s / 32 lines)
  - `close()` drains queue, final flush, then shuts down executor
  - Optional env `SESSION_LOG_SYNC_FLUSH=1` restores old await+flush-every-line behavior for emergency debugging

- [ ] **Step 1: Write failing tests**

```python
class TestSessionLogAsyncEnqueue:
    @pytest.mark.asyncio
    async def test_log_async_does_not_require_immediate_flush(self, dashscope_module, tmp_path):
        writer = dashscope_module.SessionLogWriter(str(tmp_path / "logs"))
        # With batched flush, enqueue should complete quickly even if we don't close yet
        t0 = time.monotonic()
        await writer.log_async({"request_id": "r1", "status_code": 200})
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05
        writer.close()  # drain
        # After close, entry must be on disk
        files = list((tmp_path / "logs").glob("*.jsonl"))
        assert files
        text = files[0].read_text(encoding="utf-8")
        assert "r1" in text

    @pytest.mark.asyncio
    async def test_close_drains_pending(self, dashscope_module, tmp_path):
        writer = dashscope_module.SessionLogWriter(str(tmp_path / "logs"))
        for i in range(20):
            await writer.log_async({"request_id": f"r{i}", "status_code": 200})
        writer.close()
        text = next((tmp_path / "logs").glob("*.jsonl")).read_text(encoding="utf-8")
        assert text.count("request_id") == 20
```

- [ ] **Step 2: Run tests — expect FAIL** (current `log_async` always flushes; timing may pass accidentally — assert on flush batching via a test double or by checking that `_write_sync` path uses `flush` only on interval; alternatively spy by subclassing)

If timing is flaky, prefer:

```python
def test_write_sync_skips_flush_when_batching(self, dashscope_module, tmp_path, monkeypatch):
    writer = dashscope_module.SessionLogWriter(str(tmp_path / "logs"))
    monkeypatch.setenv("SESSION_LOG_SYNC_FLUSH", "0")
    # Force batching mode attributes if read at init — construct after env set
    flushes = []
    writer._ensure_file = lambda: None  # type: ignore
    class FakeFile:
        def write(self, _): pass
        def flush(self): flushes.append(1)
        def close(self): pass
    writer._file = FakeFile()
    writer._current_date = "2099-01-01"
    writer._flush_every = 10
    writer._lines_since_flush = 0
    with writer._lock:
        for _ in range(3):
            writer._write_sync({"request_id": "x"})
    assert len(flushes) == 0
    with writer._lock:
        writer._flush_unlocked()
    assert len(flushes) == 1
    writer.close()
```

Adapt to the implementation’s attribute names; keep the contract: not every write calls `flush()`.

- [ ] **Step 3: Implement batched writer**

Rewrite `session_log.py` core behavior:

```python
SESSION_LOG_FLUSH_EVERY = int(os.environ.get("SESSION_LOG_FLUSH_EVERY", "32"))
SESSION_LOG_FLUSH_INTERVAL = float(os.environ.get("SESSION_LOG_FLUSH_INTERVAL", "0.25"))
SESSION_LOG_SYNC_FLUSH = os.environ.get("SESSION_LOG_SYNC_FLUSH", "0") == "1"
SESSION_LOG_QUEUE_MAX = int(os.environ.get("SESSION_LOG_QUEUE_MAX", "10000"))
```

- Keep full `entry` dicts unchanged (no field dropping).
- `log_async`: if `SESSION_LOG_SYNC_FLUSH`, keep old `run_in_executor` + flush-every-write path; else put entry on `queue.Queue(maxsize=SESSION_LOG_QUEUE_MAX)` (drop+ERROR log if full — never block the event loop more than a short `put_nowait`).
- Background thread or the existing single-worker executor loop: pull entries, `_write_sync` without per-line flush, flush on count/interval.
- `close()`: stop acceptor, drain queue, `_flush_unlocked()`, shutdown.

- [ ] **Step 4: Handler `finally` may still `await log_async`** — after change, await only waits for enqueue, not disk. Keep try/except ERROR logging. Same for `_maybe_flush_session_log`.

- [ ] **Step 5: Run session log tests + full units**

Run: `py -m pytest tests/test_units.py -k SessionLog -v`  
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add dashscope_proxy_lib/session_log.py dashscope_proxy_lib/handlers.py tests/test_units.py
git commit -m "perf: non-blocking session log enqueue with batched flush"
```

---

### Task 4: Wave 2b — Skip TUI log handler when headless

**Files:**
- Modify: `dashscope_proxy_lib/logging_config.py`
- Modify: `dashscope_proxy_lib/server.py`
- Test: `tests/test_units.py` (small test for `configure_logging`)

**Interfaces:**
- Consumes: `configure_logging()`
- Produces: `configure_logging(*, enable_tui_handler: bool = True) -> None`

- [ ] **Step 1: Failing test**

```python
def test_configure_logging_can_skip_tui_handler(self, dashscope_module):
    dashscope_module.configure_logging(enable_tui_handler=False)
    from dashscope_proxy_lib.logging_config import logger, TUILogHandler
    assert not any(isinstance(h, TUILogHandler) for h in logger.handlers)
    # restore default for other tests
    dashscope_module.configure_logging(enable_tui_handler=True)
```

- [ ] **Step 2: Implement**

```python
def configure_logging(*, enable_tui_handler: bool = True):
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    logger.handlers = [h for h in logger.handlers if not isinstance(h, (logging.NullHandler, TUILogHandler))]
    if enable_tui_handler:
        logger.addHandler(tui_handler)
```

In `server.main(headless=...)`, call `configure_logging(enable_tui_handler=not headless)` at startup (where logging is currently configured — if `create_proxy_resources` calls `configure_logging()`, thread `headless` through or call again from `main` before serving).

- [ ] **Step 3: pytest + commit**

```bash
git add dashscope_proxy_lib/logging_config.py dashscope_proxy_lib/server.py tests/test_units.py
git commit -m "perf: skip TUI log handler in headless mode"
```

---

### Task 5: Wave 3 — `try_admit` + `record_completion`

**Files:**
- Modify: `dashscope_proxy_lib/rate_limiter.py`
- Modify: `dashscope_proxy_lib/handlers.py`
- Modify: `dashscope_proxy_lib/queue.py` (only if admit-after-wait needs a hook; prefer keep `can_proceed` for wait loop)
- Test: `tests/test_units.py`, `tests/test_integration.py`

**Interfaces:**
- Consumes: `can_proceed`, `reserve_tokens`, `reconcile_tokens`, `record_request`, `record_circuit_success` / `failure`, `record_model_stats`, `record_body_sizes`
- Produces:

```python
async def try_admit(self, estimated_tokens: int = 0) -> tuple[bool, str, float]:
    """Under one lock: same checks as can_proceed; on success reserve TPM.
    Does NOT charge RPM (still recorded on completion).
    Returns (allowed, reason, wait_seconds). Fail-closed on lock timeout.
    """

async def record_completion(
    self,
    *,
    estimated_tokens: int,
    actual_tokens: int,
    model: str | None,
    latency_ms: float,
    request_bytes: int,
    response_bytes: int,
    is_429: bool = False,
    circuit_success: bool = True,
) -> None:
    """Under one lock: reconcile TPM, record_request metrics, circuit,
    model stats, body sizes. Fail-closed logging on lock timeout.
    """
```

Keep existing methods for tests/compatibility; handler happy-path switches to the new APIs.

- [ ] **Step 1: Write failing unit tests**

```python
@pytest.mark.asyncio
async def test_try_admit_reserves_tpm(self, rate_limiter):
    ok, reason, wait = await rate_limiter.try_admit(50)
    assert ok and reason == "ok"
    assert rate_limiter.tpm_bucket.reserved >= 50

@pytest.mark.asyncio
async def test_record_completion_single_path(self, rate_limiter):
    await rate_limiter.try_admit(50)
    await rate_limiter.record_completion(
        estimated_tokens=50,
        actual_tokens=40,
        model="m",
        latency_ms=12.0,
        request_bytes=10,
        response_bytes=20,
        circuit_success=True,
    )
    assert rate_limiter.tpm_bucket.reserved == 0
    assert rate_limiter.total_forwarded == 1
```

- [ ] **Step 2: Implement `try_admit` / `record_completion` in `RateLimiter`**

`try_admit`: copy `can_proceed` checks inside one `async with self._lock`; on success call `tpm_bucket.try_reserve` (if estimated > 0). On RPS deny, **do not** update `last_request_time` yet (Wave 5 changes that).

`record_completion`: one lock acquisition that performs reconcile + rpm add + counters + circuit success/failure branch + model stats (+ eviction if needed; may still sort outside lock like today) + body size counters. Prefer moving eviction sort outside like current `record_model_stats`.

- [ ] **Step 3: Update handler**

After `wait_for_slot` returns a wait time (allowed), replace separate `reserve_tokens` with:

```python
ok, reason, _wait = await limiter.try_admit(estimated_tokens)
if not ok:
    # same refund/503 paths as today's post-queue reserve failure
    ...
tokens_reserved = estimated_tokens > 0
```

Note: `wait_for_slot` still uses read-only `can_proceed` to sleep; `try_admit` is the commit. This preserves the intentional race guard with one fewer lock on the success bookkeeping side once completion is coalesced.

Replace success-path sequences:

```python
await limiter.reconcile_tokens(...)
await limiter.record_request(...)
await limiter.record_circuit_success()
await limiter.record_model_stats(...)
await limiter.record_body_sizes(...)
```

with one `record_completion(...)` (stream + non-stream success paths). Failure paths keep `refund_tokens` + `record_circuit_failure` as appropriate (optionally add `record_failure_completion` later — YAGNI unless easy).

- [ ] **Step 4: Run limiter + integration**

```powershell
py -m pytest tests/test_units.py -k "RateLimiter or TokenWindow or try_admit or record_completion or TokenReconcil" -v
py -m pytest tests/test_integration.py -v --tb=line
```

Expected: PASS

- [ ] **Step 5: Bench burst scenario; commit**

```bash
git add dashscope_proxy_lib/rate_limiter.py dashscope_proxy_lib/handlers.py tests/test_units.py tests/test_integration.py
git commit -m "perf: coalesce rate limiter admit and completion under fewer locks"
```

---

### Task 6: Wave 3b — Fix asyncio / thread lock nesting for TUI

**Files:**
- Modify: `dashscope_proxy_lib/rate_limiter.py` (`record_*` / `status`)
- Test: `tests/test_units.py` (status shape + concurrent record while status)

**Interfaces:**
- Produces: lock order rule — **never hold `asyncio.Lock` and `_thread_lock` at the same time**.

**Mandated pattern:**

```python
# TPM / RPM work under asyncio.Lock only
async with self._lock:
    self.tpm_bucket.reconcile(...)
    self.rpm_window.add(now)
    local_tokens = ...
# Release asyncio.Lock, then publish TUI-visible counters
with self._thread_lock:
    self.total_forwarded += 1
    self.total_tokens_consumed += local_tokens
    self.last_request_time = now
    # model_usage / recent_latencies / circuit fields likewise
```

Apply the same split in `record_request`, `record_completion`, `record_circuit_success`, `record_circuit_failure`, `record_body_sizes`, and model-stats updates. `status()` keeps taking `_thread_lock` alone for snapshots (percentiles still computed outside the lock).

- [ ] **Step 1: Unit test that `status()` works during concurrent `record_completion`**

```python
@pytest.mark.asyncio
async def test_status_during_completion(self, rate_limiter):
    async def hammer():
        for _ in range(50):
            await rate_limiter.try_admit(1)
            await rate_limiter.record_completion(
                estimated_tokens=1, actual_tokens=1, model="m",
                latency_ms=1.0, request_bytes=1, response_bytes=1,
            )
    task = asyncio.create_task(hammer())
    for _ in range(50):
        s = rate_limiter.status()
        assert "total_forwarded" in s
    await task
```

- [ ] **Step 2: Refactor lock nesting as above**

- [ ] **Step 3: Full unit + integration rate-limit related tests; commit**

```bash
git commit -m "perf: avoid holding asyncio lock while taking TUI thread lock"
```

---

### Task 7: Wave 4 — Streaming idle timeout without per-chunk `_cfg`

**Files:**
- Modify: `dashscope_proxy_lib/handlers.py` (stream loop ~795–814)
- Test: `tests/test_integration.py` (stream idle / happy stream)

**Interfaces:**
- Produces: per-stream local `chunk_idle_timeout = _cfg("STREAM_CHUNK_IDLE_TIMEOUT")` captured once before the `async for chunk` loop; reuse that float in `asyncio.timeout(...)`.

- [ ] **Step 1: Add/adjust integration test if none asserts idle timeout** — if an existing stream test covers forwarding, add a unit-level note isn’t enough; ensure at least one stream success test still passes.

- [ ] **Step 2: Implement**

```python
chunk_idle_timeout = _cfg("STREAM_CHUNK_IDLE_TIMEOUT")
tail_max = _cfg("STREAM_TAIL_BUFFER")  # or existing local name for 8KB tail
async for chunk in upstream.content:
    total_stream_bytes += len(chunk)
    tail_buffer.extend(chunk)
    if len(tail_buffer) > tail_max:
        del tail_buffer[: len(tail_buffer) - tail_max]
    try:
        async with asyncio.timeout(chunk_idle_timeout):
            await resp.write(chunk)
    except asyncio.TimeoutError:
        _log(
            logging.WARNING,
            "Stream chunk idle timeout",
            request_id=request_id,
            timeout=chunk_idle_timeout,
        )
        try:
            sse_error = (
                b'data: {"error": {"message": "upstream chunk timeout",'
                b' "type": "proxy_error"}}\n\n'
            )
            await resp.write(sse_error)
        except Exception:
            pass
        break
```

Also cache `_cfg("MAX_5XX_RETRIES")` once per request at the start of the forward/retry loop (same task).

- [ ] **Step 3: pytest stream tests + commit**

```bash
git commit -m "perf: cache stream idle timeout outside chunk loop"
```

---

### Task 8: Wave 5 — Wire circuit HALF_OPEN probe

**Files:**
- Modify: `dashscope_proxy_lib/handlers.py` (replace `circuit_is_open()` gate with probe-aware admission)
- Modify: `dashscope_proxy_lib/rate_limiter.py` only if helpers need tweaks
- Test: `tests/test_units.py`, `tests/test_integration.py`

**Interfaces:**
- Consumes: existing `can_attempt_probe() -> bool`, `circuit_is_open() -> bool`, `record_circuit_success` / `record_circuit_failure`
- Produces: Handler behavior — before upstream call:

```python
if not limiter.can_attempt_probe():
    # treat like circuit open: 503 + refund + existing failover opportunity
    ...
```

Do **not** call lock-free `circuit_is_open()` alone for the admit gate (it allows herd after cooldown). Keep `circuit_is_open()` for status displays if needed.

- [ ] **Step 1: Failing tests**

```python
def test_can_attempt_probe_claims_half_open(self, rate_limiter):
    rate_limiter.circuit_state = "OPEN"
    rate_limiter.circuit_open_until = time.monotonic() - 1
    assert rate_limiter.can_attempt_probe() is True
    assert rate_limiter.circuit_state == "HALF_OPEN"
    assert rate_limiter.circuit_probe_in_flight is True
    assert rate_limiter.can_attempt_probe() is False

@pytest.mark.asyncio
async def test_probe_success_closes(self, rate_limiter):
    rate_limiter.circuit_state = "HALF_OPEN"
    rate_limiter.circuit_probe_in_flight = True
    await rate_limiter.record_circuit_success()
    assert rate_limiter.circuit_state == "CLOSED"
    assert rate_limiter.circuit_probe_in_flight is False
```

Integration: open circuit, wait cooldown, fire 5 concurrent requests — only one reaches mock upstream (use counter on mock).

- [ ] **Step 2: Implement handler gate with `can_attempt_probe()`**

On probe failure paths that call `record_circuit_failure`, ensure probe flag clears (already in limiter). On early 503 when probe denied, do not leave `circuit_probe_in_flight` stuck (denied means we did not claim).

- [ ] **Step 3: pytest + commit**

```bash
git commit -m "fix: enforce single circuit probe after cooldown"
```

---

### Task 9: Wave 5 — Admission-time RPS shaping

**Files:**
- Modify: `dashscope_proxy_lib/rate_limiter.py` (`try_admit` / `can_proceed`)
- Test: `tests/test_units.py`

**Interfaces:**
- Produces: On successful admit, set `self.last_request_time = time.monotonic()` under the admit lock (in `try_admit`). `record_request` / `record_completion` may still update it; admit-time update is what spaces waiters.

- [ ] **Step 1: Failing test**

```python
@pytest.mark.asyncio
async def test_try_admit_updates_last_request_time(self, rate_limiter):
    rate_limiter.rps_limit = 1.0  # 1 rps => 1s gap
    t0 = time.monotonic()
    ok, _, _ = await rate_limiter.try_admit(0)
    assert ok
    ok2, reason, wait = await rate_limiter.try_admit(0)
    assert ok2 is False
    assert reason == "RPS spacing"
    assert wait > 0.5
```

- [ ] **Step 2: Implement admit-time `last_request_time` update in `try_admit`**

- [ ] **Step 3: Adjust any tests that assumed completion-only spacing**

- [ ] **Step 4: Commit**

```bash
git commit -m "perf: apply RPS spacing at admission time"
```

---

### Task 10: Wave 5 extras — TPM wait without holding outer lock + TUI session log tail

**Files:**
- Modify: `dashscope_proxy_lib/rate_limiter.py` (`can_proceed` / `try_admit` TPM wait estimate)
- Modify: `proxy_tui.py` (`_read_session_log_entries`)
- Test: units for wait estimate conservatism; optional TUI unit if present

**Interfaces:**
- Produces:
  - When TPM insufficient, compute `wait_seconds_for` **after** releasing `RateLimiter._lock` (or use a snapshot of bucket state copied under lock, compute outside). Wait must stay **≥** current estimate (never under-wait).
  - TUI: track file offset / inode; read only new bytes since last poll; still return last 200 parsed entries via `deque(maxlen=200)`.

- [ ] **Step 1: Implement TPM wait release-before-walk**

```python
# inside can_proceed / try_admit when avail < estimated:
need = estimated_tokens
# release lock, then:
wait = self.tpm_bucket.wait_seconds_for(need, time.monotonic())
```

Ensure `wait_seconds_for` is safe under its own `TokenWindowCounter._lock` without needing `RateLimiter._lock`.

- [ ] **Step 2: TUI incremental read**

```python
def __init__(...):
    self._session_log_offset = 0
    self._session_log_inode = None
    self._session_log_tail: deque[dict] = deque(maxlen=200)

def _read_session_log_entries(self) -> list[dict]:
    path = ...
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return list(self._session_log_tail)
    inode = getattr(st, "st_ino", None) or st.st_mtime_ns
    if inode != self._session_log_inode or st.st_size < self._session_log_offset:
        self._session_log_offset = 0
        self._session_log_tail.clear()
        self._session_log_inode = inode
    with open(path, "r", encoding="utf-8") as f:
        f.seek(self._session_log_offset)
        data = f.read()
        self._session_log_offset = f.tell()
    for line in data.splitlines():
        ...
        self._session_log_tail.append(entry)
    return list(self._session_log_tail)
```

- [ ] **Step 3: pytest + commit**

```bash
git commit -m "perf: shorten TPM wait lock hold; incremental TUI session log read"
```

---

### Task 11: DOX + final verification

**Files:**
- Modify: `AGENTS.md`, `dashscope_proxy_lib/AGENTS.md` (circuit probe contract, session flush semantics, `try_admit` / `record_completion`, `estimate_tokens_for_body`)
- Modify: `tests/AGENTS.md` if new test classes listed
- Run full suite + final bench

- [ ] **Step 1: Update DOX docs** to match shipped contracts (no diary; durable behavior only).

- [ ] **Step 2: Full verification**

```powershell
py -m pytest tests/
py benchmarks/run_bench.py --out benchmarks/results/final.json
```

Compare to `baseline.json` for waves kept; document summary in commit message or leave results untracked.

- [ ] **Step 3: Commit docs**

```bash
git add AGENTS.md dashscope_proxy_lib/AGENTS.md tests/AGENTS.md
git commit -m "docs: record performance optimization contracts in DOX"
```

---

## Self-review (plan vs spec)

| Spec item | Task |
|-----------|------|
| Wave 0 harness + metrics + gitignore | Task 1 |
| Wave 1 JSON dict estimate + single dump | Task 2 |
| Wave 2 session async/batch flush, full fidelity | Task 3 |
| Wave 2 headless skip TUI handler | Task 4 |
| Wave 3 try_admit + record_completion | Task 5 |
| Wave 3 TUI/asyncio lock nesting | Task 6 |
| Wave 4 stream `_cfg` cache | Task 7 |
| Wave 5 circuit probe | Task 8 |
| Wave 5 admission RPS | Task 9 |
| Wave 5 TPM wait + TUI incremental log | Task 10 |
| Invariants / out of scope | Global Constraints |
| DOX + final verify | Task 11 |

No TBD placeholders. Interfaces for `try_admit` / `record_completion` / `estimate_tokens_for_body` / `configure_logging(enable_tui_handler=...)` are consistent across tasks.
