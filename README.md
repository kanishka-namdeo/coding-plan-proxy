# Coding Plan Proxy

A smart proxy that lets you use Alibaba DashScope's Coding Plan at full throttle, without hitting rate limits or getting your requests dropped.

## Why this exists

DashScope's Coding Plan gives you generous quotas (thousands of requests per week), but the raw API has aggressive per-minute and per-second throttling. This proxy sits between your IDE and DashScope, smoothing out the request flow so you get steady, uninterrupted access — it handles retries, queues, and pacing automatically.

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env
# add your API key to .env
python dashscope_proxy.py
```

Point your OpenAI-compatible client at `http://127.0.0.1:8899` and you're done.

## What it does

**Rate limiting that won't kill your flow**
Multiple quota layers (RPS, RPM, tokens, 5-hour, weekly, monthly) with a built-in safety margin so you stay well within DashScope's limits.

**Automatic retries**
When the upstream returns 429 or 5xx, the proxy backs off with exponential jitter and retries transparently — your client never sees the hiccup.

**Request queuing**
Instead of failing fast when limits are hit, requests wait in a bounded queue until a slot opens up.

**Developer role mapping**
Converts the `developer` message role to `system` so your requests work even if the upstream doesn't support the newer format.

**SSE streaming**
Full support for streaming completions with client disconnect detection so resources aren't wasted on abandoned requests.

**Mock model list**
`GET /v1/models` returns a curated catalog of supported models without hitting the upstream.

## Endpoints

| Endpoint | Description |
|---|---|
| `POST /v1/chat/completions` | Chat completions, proxied through the rate limiter |
| `GET /v1/models` | Local mock model catalog |
| `GET /v1/proxy/status` | Current rate limiter metrics (RPM usage, queued requests, totals) |
| `GET /health` | Simple health check |
| `GET /ready` | Readiness probe (checks upstream connection) |

## Configuration

Tweak `CODING_PLAN_CONFIG` in `dashscope_proxy.py` to match your plan:

| Setting | Default | Description |
|---|---|---|
| `rpm_limit` | 2400 | Max requests per minute (before safety factor) |
| `tpm_limit` | 4,000,000 | Max tokens per minute |
| `safety_factor` | 0.8 | Multiply all limits by this (0.8 = leave 20% headroom) |
| `requests_per_5h` | 6000 | Rolling 5-hour request cap |
| `requests_per_week` | 45000 | Weekly request cap |
| `requests_per_month` | 90000 | Monthly request cap |
| `max_queue_size` | 200 | Max requests waiting in queue |
| `max_retries` | 10 | Max retries on 429 responses |

The defaults match DashScope's Coding Plan tiers — you usually only need to adjust `safety_factor`.

## Running Tests

```bash
pytest tests/
```
