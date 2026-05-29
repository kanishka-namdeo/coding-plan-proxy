# Coding Plan Proxy

An async HTTP proxy for the DashScope Coding API with built-in rate limiting, request queuing, retry logic, and multi-layer quota management.

## Features

- **Multi-layer rate limiting** — RPS, RPM, TPM, 5-hour, weekly, and monthly quotas with configurable safety factors
- **Smart retry** — Automatic exponential backoff with jitter for 429 and 5xx responses
- **Request queuing** — Bounded queue with configurable max size
- **Developer role mapping** — Automatically converts `developer` role to `system` for compatibility
- **Streaming support** — Full SSE streaming with client disconnect detection
- **Mock model list** — Intercepts `GET /v1/models` to return a curated model catalog
- **Health & readiness endpoints** — `/health` and `/ready` for container orchestration
- **Proxy status** — `GET /v1/proxy/status` for real-time rate limiter metrics

## Setup

1. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

2. **Configure the API key**

   ```bash
   cp .env.example .env
   # Edit .env and add your DashScope API key
   ```

3. **Run the proxy**

   ```bash
   python dashscope_proxy.py
   ```

   The proxy starts on `127.0.0.1:8899` by default.

## Endpoints

| Endpoint | Description |
|---|---|
| `POST /v1/chat/completions` | Forwarded to DashScope with rate limiting |
| `GET /v1/models` | Returns mock model list |
| `GET /v1/proxy/status` | Rate limiter metrics |
| `GET /health` | Health check |
| `GET /ready` | Readiness probe |

## Configuration

Edit `CODING_PLAN_CONFIG` in `dashscope_proxy.py` to adjust limits:

- `rpm_limit` — requests per minute
- `tpm_limit` — tokens per minute
- `safety_factor` — multiplier applied to limits (e.g. 0.8 = 80% of max)
- `requests_per_5h` — rolling 5-hour quota
- `requests_per_week` / `requests_per_month` — longer-term quotas
- `max_queue_size` — maximum pending requests in queue
- `max_retries` — max retry attempts for 429 responses

## Running Tests

```bash
pytest tests/
```
