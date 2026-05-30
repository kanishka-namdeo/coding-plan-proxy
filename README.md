# Coding Plan Proxy

An HTTP proxy for the DashScope Coding API with rate limiting, request queuing, and automatic retries.

## Quick Start

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
# add your API key to .env (see .env.example)
py dashscope_proxy.py
```

Point your OpenAI-compatible client at `http://127.0.0.1:8899`.

## Features

**Multi-layer rate limiting**
Enforces RPS, RPM, TPM (via Token Bucket), and quotas over 5-hour, weekly, and monthly windows. A configurable safety factor keeps usage below the hard limits.

**Automatic retries**
Retries 429 and 5xx responses with exponential backoff and jitter.

**Request queuing**
Requests that exceed rate limits are placed in a bounded queue instead of failing immediately.

**Developer role mapping**
Converts `developer` role messages to `system` for upstream compatibility.

**SSE streaming**
Streams completions through the proxy and aborts if the client disconnects.

**Mock model list**
`GET /v1/models` returns a static list of supported models.

**TPM token lifecycle**
TPM is enforced via a Token Bucket with reserve/reconcile/refund semantics. Tokens are reserved before sending to upstream, reconciled with real token counts after the response, and refunded on errors or client disconnects.

**Deadline-bounded queue waits**
Queue waits respect a configurable deadline (default 120s). The proxy checks for client disconnects between wait iterations and aborts queued requests if the deadline is exceeded.

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
| `rpm_limit` | 12 | Max requests per minute (before safety factor) |
| `tpm_limit` | 2,000,000 | Max tokens per minute |
| `safety_factor` | 0.6 | Multiply all limits by this (0.6 = leave 40% headroom) |
| `requests_per_5h` | 6000 | Rolling 5-hour request cap |
| `requests_per_week` | 45000 | Weekly request cap |
| `requests_per_month` | 90000 | Monthly request cap |
| `max_queue_size` | 500 | Max requests waiting in queue |
| `max_retries` | 40 | Max retries on 429 responses |
| `base_backoff` | 1.0 | Base seconds for exponential backoff |

The defaults match DashScope's Coding Plan tiers — you usually only need to adjust `safety_factor`.

## Running Tests

```powershell
py -m pytest tests/
```
