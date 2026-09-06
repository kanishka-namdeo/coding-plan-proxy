# tests

## Purpose

Pytest test suite for the DashScope proxy. Three layers: unit tests (isolated classes/functions), integration tests (handler with mock upstream via aiohttp test server), and e2e tests (real API calls requiring an API key).

## Ownership

This directory owns all test code. Test fixtures and configuration live in `conftest.py`.

## Local Contracts

- **Test layers**:
  - `test_units.py` — unit tests for `SlidingWindowCounter`, `TokenWindowCounter`, `RateLimiter`, token extraction, request transformation, HTTP helpers, session logging, TUI log handler, provider router, multi-provider rate limiter, TUI status helpers (`TestTuiStatusHelpers`)
  - `test_integration.py` — integration tests for `handle_request()` with mock upstream servers: API key validation, health/ready endpoints, request validation, proxy forwarding, 429/5xx retries, mock models, proxy status, response headers, queue enforcement, circuit breaker, graceful shutdown, streaming errors, client disconnect, multi-retry scenarios, provider routing
  - `test_e2e_real.py` — end-to-end tests against real DashScope API (requires `DASHSCOPE_API_KEY` in `.env`)
  - `conftest.py` — shared fixtures: `dashscope_module`, `rate_limiter`, `mock_app`, `mock_request`, `make_test_config`, `token_bucket`

- **Fixture conventions**:
  - `dashscope_module` — returns the already-imported `dashscope_proxy` module (facade)
  - `rate_limiter` — creates a `RateLimiter` with tiny limits for fast tests
  - `mock_app` — mock aiohttp app with `rate_limiter` and `client_session`
  - `mock_request` — mock aiohttp request with default path `/v1/chat/completions`
  - `proxy_app` (integration) — creates proxy app with test rate limiter and mock client session
  - `_reset_provider_router` (integration, autouse) — resets the lazy provider router singleton before each test

- **Mock patterns**:
  - Integration tests use `aiohttp_client` fixture to create a test server with the proxy app
  - Mock upstream servers are created inline in tests that need to simulate upstream responses
  - Tests patch `dashscope_proxy.TARGET_BASE` to point to the mock upstream
  - Use `_patch_target_base()` context manager (integration) to patch across all modules

- **Test configuration**:
  - `pyproject.toml` sets `asyncio_mode = "auto"` and `testpaths = ["tests"]`
  - Tests use `pytest.mark.asyncio` for async test functions
  - Rate limiter configs in tests use large limits (6000 RPM, 10M TPM) for fast execution
  - Backoff times are tiny (0.05s base) to keep tests fast

- **E2E requirements**:
  - `test_e2e_real.py` requires `DASHSCOPE_API_KEY` in `.env`
  - E2E tests make real API calls to DashScope and verify end-to-end behavior
  - Skip e2e tests if API key is not available

## Work Guidance

- Add unit tests for new pure functions and classes in `test_units.py`
- Add integration tests for new handler behavior in `test_integration.py`
- Use `aiohttp_client` fixture for integration tests that need a test server
- Mock upstream servers inline in tests; do not create shared mock server fixtures
- Patch constants via the facade (`dashscope_proxy.CONSTANT`), not direct imports
- Reset the provider router singleton before tests that exercise provider routing
- Keep test configs fast: large limits, tiny backoffs, small queue sizes
- Mark async tests with `@pytest.mark.asyncio` (or rely on `asyncio_mode = "auto"`)

## Verification

- `py -m pytest tests/` — full test suite
- `py -m pytest tests/test_units.py` — unit tests only
- `py -m pytest tests/test_integration.py` — integration tests only
- `py -m pytest tests/test_e2e_real.py` — e2e tests (requires API key)

## Child DOX Index

No child directories.
