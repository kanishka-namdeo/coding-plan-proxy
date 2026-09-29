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

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

import dashscope_proxy
import dashscope_proxy_lib.config as _config_mod
import dashscope_proxy_lib.handlers as _handlers_mod
import dashscope_proxy_lib.provider_router as _provider_router_mod

CONNECTOR_LIMIT = 200


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


def _patch_target_base(mock_base: str) -> None:
    dashscope_proxy.TARGET_BASE = mock_base
    _config_mod.TARGET_BASE = mock_base
    _provider_router_mod.TARGET_BASE = mock_base
    _handlers_mod._provider_router = None


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
    _patch_target_base(mock_base)
    dashscope_proxy.DASHSCOPE_API_KEY = "bench-key"
    cfg = high_limits()
    limiter = dashscope_proxy.MultiProviderRateLimiter(cfg)
    app = dashscope_proxy.create_app()
    app["rate_limiter"] = limiter
    app["shutting_down"] = asyncio.Event()
    app["session_log"] = None
    timeout = ClientTimeout(total=30, connect=5)
    connector = TCPConnector(limit=CONNECTOR_LIMIT, ttl_dns_cache=300)
    app["client_session"] = ClientSession(timeout=timeout, connector=connector)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, f"http://127.0.0.1:{port}", app
    except Exception:
        session = app["client_session"]
        if not session.closed:
            await session.close()
        await runner.cleanup()
        raise


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
    mock_runner = None
    proxy_runner = None
    app = None
    results = None
    try:
        mock_runner, mock_base = await start_mock_upstream()
        proxy_runner, proxy_base, app = await start_proxy(mock_base)
        upstream_conn = app["client_session"].connector
        results = {
            "mock_base": mock_base,
            "proxy_base": proxy_base,
            "connector": {
                "limit": upstream_conn.limit,
                "limit_per_host": upstream_conn.limit_per_host,
            },
            "scenarios": {},
        }
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
        if app is not None:
            session = app.get("client_session")
            if session is not None and not session.closed:
                await session.close()
        if proxy_runner is not None:
            await proxy_runner.cleanup()
        if mock_runner is not None:
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
