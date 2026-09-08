# Task 5: Update Tests for Quota Removal

**Goal:** Remove quota-related tests and update test configs to use reduced config shape.

**Files:**
- Modify: `tests/test_units.py`
- Modify: `tests/test_integration.py`

## Exact Requirements

### test_units.py - Remove quota tests

Delete these test methods from `TestRateLimiterCanProceed`:
- `test_blocks_at_5h_limit` (lines 62-67)
- `test_blocks_at_weekly_limit` (lines 69-74)
- `test_blocks_at_monthly_limit` (lines 76-81)

Delete entire class `TestRateLimiterQuotaReset` (lines 115-141)

### test_units.py - Update test configs

Remove `requests_per_5h`, `requests_per_week`, `requests_per_month` from:
- `test_rps_spacing` config (lines 97-99)
- `TestComputeBackoff::test_backoff_grows_with_attempt` (lines 496-497)
- `TestComputeBackoff::test_backoff_respects_base_config` (lines 515-516)
- `TestWaitForSlot::test_immediate_success_under_limits` (lines 691-692)
- `TestWaitForSlot::test_none_when_queue_full` (lines 707-708)
- `TestWaitForSlot::test_none_when_client_disconnects` (lines 724-725)
- `TestMultiProviderRateLimiter::_make_config` (lines 1620-1621)

### test_integration.py - Update config

Remove quota keys from `make_test_config` (lines 45-47)

### test_integration.py - Update terminal 429 test

Rename and update `test_quota_exceeded_429_not_retried` to test immediate return:
```python
    async def test_terminal_429_returned_immediately(self, aiohttp_client, proxy_app):
        """Terminal 429 from upstream is returned immediately without retry."""
        upstream_app = web.Application()
        quota_body = json.dumps({
            "error": {
                "code": "throttling",
                "message": "usage allocated quota exceeded. please try again later.",
            }
        }).encode()
        call_count = 0

        async def terminal_429(request):
            nonlocal call_count
            call_count += 1
            return web.Response(status=429, body=quota_body, content_type="application/json")

        upstream_app.router.add_post("/v1/chat/completions", terminal_429)

        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        try:
            original_target = dashscope_proxy.TARGET_BASE
            dashscope_proxy.TARGET_BASE = f"http://127.0.0.1:{upstream_port}"
            try:
                app, rl = proxy_app
                async with aiohttp.ClientSession() as session:
                    app["client_session"] = session
                    client = await aiohttp_client(app)
                    resp = await client.post(
                        "/v1/chat/completions",
                        data=json.dumps({
                            "model": "qwen3-coder-plus",
                            "messages": [{"role": "user", "content": "hi"}],
                        }).encode(),
                    )
                    assert resp.status == 429
                    data = await resp.json()
                    assert "quota exceeded" in data["error"]["message"].lower()
                    assert call_count == 1  # Only one upstream call, no retry
            finally:
                dashscope_proxy.TARGET_BASE = original_target
        finally:
            await upstream_runner.cleanup()
```

## Tests to Run

After making changes:
1. Run: `py -m pytest tests/`
Expected: All tests PASS

## Commit

After implementing:
```bash
git add tests/test_units.py tests/test_integration.py
git commit -m "test: update tests for quota removal"
```