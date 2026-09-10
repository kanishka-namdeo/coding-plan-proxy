# Proxy overhead benchmarks

Local harness that starts a mock upstream and the proxy on ephemeral ports, then measures end-to-end latency through the proxy (not upstream API time in isolation).

## Run

From the repository root:

```text
py benchmarks/run_bench.py
py benchmarks/run_bench.py --out benchmarks/results/baseline.json
```

Prefer a quiet, headless machine (close the TUI and other heavy apps) so numbers are comparable run-to-run.

## Scenarios

| Key | Description |
| --- | --- |
| `nonstream_small` | Small JSON chat completion, 50 samples after 5 warmup |
| `nonstream_large` | ~8k user message + 20 tools, 30 samples after 3 warmup |
| `stream_many_chunks` | Streaming response with many SSE chunks, 30 samples |
| `concurrent_burst` | 50 parallel small requests; reports success count, wall time, and RPS |

Each scenario reports `p50_ms`, `p95_ms`, and `mean_ms` (burst also includes `wall_ms` and `rps`). Output JSON includes connector `limit` / `limit_per_host` metadata for the proxy client session.

## Interpreting results

These numbers are **proxy overhead baselines** against a local mock upstream. Use them before/after performance work on the same machine and command.

When comparing optimizations, treat a regression as worth investigating if p50 or p95 moves by roughly **≥5%** without an explained tradeoff.

## Results on disk

Files under `benchmarks/results/` are **gitignored**. Commit the harness only; keep baseline JSON local for comparison.
