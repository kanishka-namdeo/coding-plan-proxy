# Coding Plan Proxy

An HTTP proxy for the DashScope Coding API with multi-provider routing, rate limiting, request queuing, automatic retries, and a rich Textual TUI dashboard.

## Quick Start

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
# add your API key to .env (see .env.example)
py dashscope_proxy.py           # launches proxy + TUI dashboard
py dashscope_proxy.py --headless # launches proxy without TUI
```

Point your OpenAI-compatible client at `http://127.0.0.1:8899`.

## TUI Dashboard

Run the proxy to launch the interactive TUI dashboard. Monitor rate limits, quotas, request metrics, live logs, per-model usage, and configuration in real time.

### Overview Tab

Real-time rate limiter status, RPM/TPM/quotas with progress bars, connection status, and request statistics (success rate, rejected, pending/max queue). Non-primary providers appear as soon as they are configured, including at zero traffic. Live log feed of warnings and errors.

![Overview](screenshots/overview.svg)

### Metrics Tab

Sparkline charts for RPM, tokens-per-minute, queue depth, and upstream latency. Derived metrics (success rate, latency percentiles), failover events, and a latency histogram.

![Metrics](screenshots/metrics.svg)

### Logs Tab

Full log viewer with text search, level filtering, time range selection, pause/resume, auto-scroll toggle, and export to file.

![Logs](screenshots/logs.svg)

### Models Tab

Per-model usage breakdown: request count with percentage, tokens, 429 errors, average latency, and totals row. Sortable by requests, tokens, latency, or 429s.

![Models](screenshots/models.svg)

### Config Tab

Grouped network, timeout, connection, buffering, logging, and per-provider limit blocks. Filter by key; source column shows env vs default.

![Config](screenshots/config.svg)

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `1` | Overview tab |
| `2` | Logs tab |
| `3` | Metrics tab |
| `4` | Models tab |
| `5` | Config tab |
| `r` | Clear logs |
| `q` | Quit |

## Features

### Multi-Provider Routing

**Seven providers supported**
Route requests to DashScope (primary), MIMO (secondary), OpenLux (tertiary), ARK/BytePlus (quaternary), Meta AI/Muse Spark (quinary), DeepSeek (senary), or GLM/Z.ai (septenary).

**Provider pinning**
Force a specific provider by prefixing the model name: `openlux/gpt-5.6-sol`, `deepseek/deepseek-v4-flash`, `glm/glm-5.3`. Available slugs: `dashscope`, `mimo`, `openlux`, `ark`, `metaspark`, `deepseek`, `glm`, `zai`.

**Cross-provider failover**
Overlapping models (present in multiple provider lists) support automatic failover on 429/5xx/timeout. After per-provider retries are exhausted, the handler advances to the next available provider with a closed circuit.

**Model overlap detection**
`GET /v1/models` returns `providers` and `provider_models` fields indicating which providers serve each model. `GET /v1/proxy/status` includes `model_overlaps` for models served by 2+ providers.

### Rate Limiting & Quotas

**Multi-layer rate limiting**
Enforces RPS, RPM, and TPM (via Token Bucket). A configurable safety factor keeps usage below the hard limits.

**TPM token lifecycle**
TPM is enforced via a Token Bucket with reserve/reconcile/refund semantics. Tokens are reserved before sending to upstream, reconciled with real token counts after the response, and refunded on errors or client disconnects.

**Deadline-bounded queue waits**
Queue waits respect a configurable deadline (default 120s). The proxy checks for client disconnects between wait iterations and aborts queued requests if the deadline is exceeded.

### Resilience

**Automatic retries**
Retries 429 and 5xx responses with exponential backoff and jitter.

**Request queuing**
Requests that exceed rate limits are placed in a bounded queue instead of failing immediately.

**Circuit breaker**
Opens circuit on repeated upstream failures, preventing cascade failures and allowing recovery.

### Request Handling

**Developer role mapping**
Converts `developer` role messages to `system` for upstream compatibility.

**SSE streaming**
Streams completions through the proxy and aborts if the client disconnects.

**Hop-by-hop header stripping**
Removes connection-specific headers before forwarding to upstream.

**Client disconnect detection**
Detects when clients disconnect and refunds TPM tokens, cancels pending requests.

**Session logging**
All requests and responses are logged to `session_logs/` for audit and debugging.

### Mock & Diagnostics

**Mock model list**
`GET /v1/models` returns a static list of supported models.

**Interactive TUI dashboard**
Five-tab dashboard with live metrics, sparkline charts, log viewer with filters, per-model analytics, and configuration viewer. Keyboard-driven navigation.

## Endpoints

| Endpoint | Description |
|---|---|
| `POST /v1/chat/completions` | Chat completions, proxied through the rate limiter |
| `GET /v1/models` | Model catalog with `providers` and `provider_models` fields |
| `GET /v1/proxy/status` | Current rate limiter metrics, includes `model_overlaps` |
| `GET /health` | Simple health check |
| `GET /ready` | Readiness probe (checks upstream connection) |

## Configuration

Configure via `.env` file (see `.env.example`). Key settings:

| Setting | Default | Description |
|---|---|---|
| `DASHSCOPE_API_KEY` | (required) | Your DashScope API key |
| `PROXY_RPM_LIMIT` | 9 | Max requests per minute (before safety factor) |
| `PROXY_TPM_LIMIT` | 4,000,000 | Max tokens per minute |
| `PROXY_SAFETY_FACTOR` | 0.8 | Multiply all limits by this (0.8 = leave 20% headroom) |
| `PROXY_MAX_QUEUE_SIZE` | 500 | Max requests waiting in queue |
| `PROXY_MAX_RETRIES` | 40 | Max retries on 429 responses |
| `PROXY_BASE_BACKOFF` | 1.0 | Base seconds for exponential backoff |
| `UPSTREAM_TIMEOUT_TOTAL` | 300 | Total timeout for upstream requests (seconds) |
| `MAX_BODY_SIZE` | 52428800 | Max request body size (50 MB) |

### Provider Configuration

Each provider has its own environment variables for API key, base URL, and rate limits:

| Provider | API Key Env Var | Base URL Env Var |
|----------|-----------------|------------------|
| MIMO (secondary) | `MIMO_CODING_PLAN_API_KEY` | `MIMO_CODING_PLAN_TARGET_BASE` |
| OpenLux (tertiary) | `OPENLUX_API_KEY` | `OPENLUX_TARGET_BASE` |
| ARK (quaternary) | `MODEL_ARK_API_KEY` | `MODEL_ARK_TARGET_BASE` |
| Meta AI (quinary) | `META_AI_API_KEY` | `META_AI_TARGET_BASE` |
| DeepSeek (senary) | `DEEPSEEK_API_KEY` | `DEEPSEEK_TARGET_BASE` |
| GLM (septenary) | `GLM_API_KEY` | `GLM_TARGET_BASE` |

Override rate limits per provider with `SECONDARY_RPM_LIMIT`, `TERTIARY_TPM_LIMIT`, etc.

### Failover Order

Customize the cross-provider failover order with `MODEL_FALLBACK_ORDER`:

```bash
MODEL_FALLBACK_ORDER=septenary,senary,quinary,quaternary,tertiary,secondary,primary
```

## Running Tests

```powershell
py -m pytest tests/
```