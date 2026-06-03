# Coding Plan Proxy — Agent Instructions

## Project Context

Single-file Python async HTTP reverse proxy (`dashscope_proxy.py`) built with `aiohttp`. It sits between developer tools (Cursor IDE, etc.) and Alibaba's DashScope AI API to prevent rate-limit lockouts.

**What it does:**
- Proxies OpenAI-compatible API calls to `https://coding-intl.dashscope.aliyuncs.com`
- Multi-layer rate limiting (RPS, RPM, TPM, 5-hour/weekly/monthly quotas)
- Request queuing with bounded queue size
- Automatic retries with exponential backoff on 429 and 5xx
- SSE streaming support with client-disconnect detection
- `developer` → `system` role mapping
- Mock model list at `GET /v1/models`

**Tech Stack:**
- Python 3.13.13 with `aiohttp` (server + HTTP client)
- `pytest` with `pytest-asyncio` for testing
- Single-file architecture: `dashscope_proxy.py` is the entire app
- Tests live in `tests/` directory

**Key Files:**
| File | Purpose |
|---|---|
| `dashscope_proxy.py` | The entire proxy application (~739 lines) |
| `tests/test_units.py` | Unit tests for individual components |
| `tests/test_integration.py` | Integration tests with app fixtures |
| `tests/test_e2e_real.py` | End-to-end tests against real upstream |
| `requirements.txt` | Dependencies |

## Python Environment (Windows)

This system runs **Windows** with **Python 3.13.13** installed via the `py` launcher.

- **ALWAYS** use the `.venv` virtual environment at the project root for ALL Python operations
- **NEVER** use `python`, `python3`, or bare `py` directly — always use `.venv\Scripts\python.exe`
- **NEVER** use `pip` directly — use `.venv\Scripts\python.exe -m pip` instead
- Virtual environment path: `.venv\Scripts\python.exe` (relative to project root)

## Security Rules

- **NEVER** hardcode API keys, tokens, or secrets in source code
- **ALWAYS** load secrets from environment variables (`os.environ.get()`)
- **NEVER** commit `.env` files or credential files to git

## Architecture Constraints

- Keep the single-file architecture (don't split `dashscope_proxy.py` unless explicitly asked)
- All configuration is in `CODING_PLAN_CONFIG` dict and module-level constants
- The aiohttp `ClientSession` is stored on `app["client_session"]` and must be properly closed on shutdown
- Rate limiter state lives on `app["rate_limiter"]`
- Shutdown signal is `app["shutting_down"]` (asyncio.Event)

## Critical Safety Rules

### Trace ALL Code Paths for Resource Cleanup

When writing code with retry loops, conditionals, or error handlers, trace every possible execution path and verify resources are properly cleaned up on ALL paths, not just the happy path.

**Resources to track:**
- HTTP connections (`upstream.close()`)
- aiohttp `ClientResponse` objects (`response.release()` or `response.close()`)
- Counters (`pending_requests` must be incremented/decremented exactly once per request)
- File handles, locks, any acquired resource

**Before committing any code change involving retries, verify:**
1. Every `continue` in a retry loop cleans up resources first
2. Every `return` in error paths cleans up resources first
3. Every exception handler cleans up resources first
4. The `finally` block does not double-clean (causing errors on already-released resources)

### Never Double-Decrement or Double-Cleanup

If a resource is cleaned up in both an `except` block AND a `finally` block, it will be cleaned up twice. This causes errors. Always consolidate cleanup into a single `finally` block with a guard.

### Verify Logical Reachability of Branches

After writing nested conditionals, verify that all branches are reachable. Check for contradictory conditions like `if is_stream:` followed by `if not is_stream:` inside it.

### Counters Must Be Balanced

Every increment must have exactly one corresponding decrement. If a counter is incremented before entering a retry loop, it must be decremented exactly once when the request completes (success, error, or exception). Use a `finally` block for the decrement, not both `except` and `finally`.

### Read Your Own Output Before Continuing

Before making tool calls that depend on code you just wrote, re-read the code you just generated. Check for:
- Duplicate method calls on the same object
- Calls on consumed resources (e.g., `.read()` after `.release()`)
- Typos in variable names, missing imports

### aiohttp Content-Type Must Not Include Charset

When creating `web.Response` objects, strip the charset from `Content-Type`. aiohttp's `content_type` parameter rejects values containing `; charset=`.

```python
content_type = upstream_content_type.split(";")[0].strip()
resp = web.Response(content_type=content_type)
```

### Use Correct Shell Syntax

This project runs on **Windows with PowerShell**. Do NOT use bash heredocs, `$()` substitution, or bash pipes. Use PowerShell-native syntax.

### Verification Checklist

Before declaring any code change complete:
- All retry loop paths clean up resources before `continue`
- No double-cleanup (except blocks + finally blocks)
- All conditional branches are reachable
- Counter increments/decrements are balanced (net zero per request)
- aiohttp `content_type` values have charset stripped
- Shell commands use PowerShell syntax, not bash

## Verification and Workflow Rules

### Test End-to-End Before Claiming Success

After implementing any feature involving external services, always test the actual forwarding path end-to-end. Testing mock endpoints alone (`/health`, `/v1/models`, `/v1/proxy/status`) is insufficient.

### Clarify Before Acting on Ambiguous Requests

When a user request seems off-topic or ambiguous, ask for clarification first. Do NOT dispatch subagents to search for functionality you already know doesn't exist. Do NOT install tools without confirming the user wants them.

### Identify Critical Bugs and Fix Them Immediately

When evaluating code and finding CRITICAL or HIGH severity bugs, fix them immediately in the same session. Do NOT defer critical fixes to "future work."

### Run Tests and Linters Before Committing

Before running `git commit`:
1. Run `pytest tests/ -v` — all tests must pass
2. Run any available linter — no new errors
3. If tests fail, fix them first
4. Only commit when the codebase is in a passing state

### Do Not Add TODO Comments

If something needs to be done, do it. TODO comments accumulate and are rarely completed.

### Write Technical Documentation, Not Marketing Copy

When writing README or documentation, state facts about what the proxy does and how to use it. Keep it concise and technical.

### Implement Identified Improvements, Not Just Recommend Them

When asked to evaluate reliability or code quality, identify issues AND implement the critical ones. Don't stop at producing a recommendation list.
