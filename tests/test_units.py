"""Tests for core unit classes and pure functions."""
import json
import time
import asyncio
import os
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from email.utils import formatdate

import pytest


# ---------------------------------------------------------------------------
# SlidingWindowCounter
# ---------------------------------------------------------------------------

class TestSlidingWindowCounter:
    def test_add_and_count(self, dashscope_module):
        sw = dashscope_module.SlidingWindowCounter(60)
        sw.add(now=100.0)
        sw.add(now=101.0)
        sw.add(now=102.0)
        assert sw.count(now=102.0) == 3

    def test_prune_old_events(self, dashscope_module):
        sw = dashscope_module.SlidingWindowCounter(10)
        sw.add(now=100.0)
        sw.add(now=105.0)
        # At time 115, event at 100 is outside the 10s window
        assert sw.count(now=115.0) == 1

    def test_prune_all_events(self, dashscope_module):
        sw = dashscope_module.SlidingWindowCounter(10)
        sw.add(now=100.0)
        assert sw.count(now=200.0) == 0

    def test_deque_cap(self, dashscope_module):
        sw = dashscope_module.SlidingWindowCounter(3600, max_size=5)
        for i in range(20):
            sw.add(now=100.0 + i)
        assert len(sw.events) <= 5

    def test_empty_count(self, dashscope_module):
        sw = dashscope_module.SlidingWindowCounter(60)
        assert sw.count(now=100.0) == 0


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

class TestRateLimiterCanProceed:
    @pytest.mark.asyncio
    async def test_allows_when_under_limits(self, rate_limiter):
        allowed, reason, _ = await rate_limiter.can_proceed()
        assert allowed is True
        assert reason == "ok"

    @pytest.mark.asyncio
    async def test_blocks_at_5h_limit(self, rate_limiter):
        rate_limiter.hour5_limit = 0
        allowed, reason, _ = await rate_limiter.can_proceed()
        assert allowed is False
        assert "5-hour" in reason.lower() or "quota" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_at_weekly_limit(self, rate_limiter):
        rate_limiter.week_count = rate_limiter.week_limit
        allowed, reason, _ = await rate_limiter.can_proceed()
        assert allowed is False
        assert "weekly" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_at_monthly_limit(self, rate_limiter):
        rate_limiter.month_count = rate_limiter.month_limit
        allowed, reason, _ = await rate_limiter.can_proceed()
        assert allowed is False
        assert "monthly" in reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_at_rpm_limit(self, rate_limiter):
        rate_limiter.rpm_limit = 1
        await rate_limiter.record_request(0, now=time.monotonic())
        allowed, reason, _ = await rate_limiter.can_proceed()
        assert allowed is False
        assert "rpm" in reason.lower()

    @pytest.mark.asyncio
    async def test_rps_spacing(self, dashscope_module):
        config = {
            "rpm_limit": 6000,
            "tpm_limit": 10_000_000,
            "safety_factor": 0.8,
            "requests_per_5h": 100_000,
            "requests_per_week": 100_000,
            "requests_per_month": 100_000,
            "max_queue_size": 200,
            "max_retries": 3,
            "base_backoff": 0.1,
        }
        rl = dashscope_module.RateLimiter(config)
        # Simulate a request just happened
        rl.last_request_time = time.monotonic()
        # Second request immediately should be blocked by RPS spacing
        allowed, reason, wait = await rl.can_proceed()
        assert allowed is False
        assert "rps" in reason.lower()
        assert wait > 0


# ---------------------------------------------------------------------------
# RateLimiter quota reset
# ---------------------------------------------------------------------------

class TestRateLimiterQuotaReset:
    @pytest.mark.asyncio
    async def test_weekly_counter_resets_after_7_days(self, rate_limiter):
        rate_limiter.week_count = rate_limiter.week_limit
        # Force week_start to 8 days ago
        rate_limiter.week_start = time.time() - (8 * 24 * 3600)
        allowed, _, _ = await rate_limiter.can_proceed()
        assert allowed is True
        assert rate_limiter.week_count == 0

    @pytest.mark.asyncio
    async def test_monthly_counter_resets_after_30_days(self, rate_limiter):
        rate_limiter.month_count = rate_limiter.month_limit
        rate_limiter.month_start = time.time() - (31 * 24 * 3600)
        allowed, _, _ = await rate_limiter.can_proceed()
        assert allowed is True
        assert rate_limiter.month_count == 0

    @pytest.mark.asyncio
    async def test_weekly_counter_does_not_reset_prematurely(self, rate_limiter):
        rate_limiter.week_count = rate_limiter.week_limit
        rate_limiter.week_start = time.time() - (3 * 24 * 3600)  # 3 days ago
        allowed, _, _ = await rate_limiter.can_proceed()
        assert allowed is False


# ---------------------------------------------------------------------------
# RateLimiter concurrency
# ---------------------------------------------------------------------------

class TestRateLimiterConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_record_request(self, rate_limiter):
        """Multiple concurrent record_request calls should all succeed and increment correctly."""
        tasks = [rate_limiter.record_request(10) for _ in range(10)]
        await asyncio.gather(*tasks)
        assert rate_limiter.total_forwarded == 10
        assert rate_limiter.total_tokens_consumed == 100

    @pytest.mark.asyncio
    async def test_concurrent_can_proceed(self, rate_limiter):
        """Multiple concurrent can_proceed calls should not raise."""
        tasks = [rate_limiter.can_proceed() for _ in range(20)]
        results = await asyncio.gather(*tasks)
        for allowed, reason, _ in results:
            assert isinstance(allowed, bool)


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

class TestTokenExtraction:
    def test_extract_tokens_from_json_response(self, dashscope_module):
        body = json.dumps({
            "usage": {"total_tokens": 42, "prompt_tokens": 10, "completion_tokens": 32}
        }).encode()
        assert dashscope_module.extract_tokens_from_response(body) == 42

    def test_extract_tokens_missing_usage(self, dashscope_module):
        body = json.dumps({"choices": []}).encode()
        assert dashscope_module.extract_tokens_from_response(body) == 0

    def test_extract_tokens_invalid_json(self, dashscope_module):
        assert dashscope_module.extract_tokens_from_response(b"not json") == 0

    def test_extract_tokens_from_stream(self, dashscope_module):
        sse = b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\ndata: {"usage": {"total_tokens": 55}}\n\ndata: [DONE]\n\n'
        assert dashscope_module.extract_tokens_from_stream(sse) == 55

    def test_extract_tokens_from_stream_no_usage(self, dashscope_module):
        sse = b'data: {"choices": []}\n\ndata: [DONE]\n\n'
        assert dashscope_module.extract_tokens_from_stream(sse) == 0

    def test_estimate_tokens(self, dashscope_module):
        body = json.dumps({
            "messages": [{"role": "user", "content": "Hello world, this is a test"}]
        }).encode()
        est = dashscope_module.estimate_tokens_for_request(body)
        assert est > 0  # ~10 chars / 4 = ~2, but clamped to 100

    def test_estimate_tokens_empty_messages(self, dashscope_module):
        body = json.dumps({"messages": []}).encode()
        assert dashscope_module.estimate_tokens_for_request(body) == 100


# ---------------------------------------------------------------------------
# map_developer_to_system
# ---------------------------------------------------------------------------

class TestMapDeveloperToSystem:
    def test_converts_developer_role(self, dashscope_module):
        body = {"messages": [{"role": "developer", "content": "be helpful"}]}
        result = dashscope_module.map_developer_to_system(body)
        assert result["messages"][0]["role"] == "system"

    def test_leaves_other_roles(self, dashscope_module):
        body = {"messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]}
        result = dashscope_module.map_developer_to_system(body)
        roles = [m["role"] for m in result["messages"]]
        assert "developer" not in roles

    def test_non_dict_message(self, dashscope_module, caplog):
        body = {"messages": ["not a dict", {"role": "user", "content": "hi"}]}
        result = dashscope_module.map_developer_to_system(body)
        assert result["messages"][1]["role"] == "user"
        assert "Non-dict" in caplog.text

    def test_messages_not_a_list(self, dashscope_module, caplog):
        body = {"messages": {"0": {"role": "developer", "content": "hi"}}}
        result = dashscope_module.map_developer_to_system(body)
        assert result["messages"]["0"]["role"] == "developer"  # unchanged
        assert "not a list" in caplog.text.lower()

    def test_no_messages_field(self, dashscope_module):
        body = {"model": "qwen3-coder-plus"}
        result = dashscope_module.map_developer_to_system(body)
        assert "messages" not in result


# ---------------------------------------------------------------------------
# parse_retry_after
# ---------------------------------------------------------------------------

class TestParseRetryAfter:
    def test_delta_seconds(self, dashscope_module):
        assert dashscope_module.parse_retry_after("30") == 30.0

    def test_delta_seconds_float(self, dashscope_module):
        assert dashscope_module.parse_retry_after("2.5") == 2.5

    def test_http_date_future(self, dashscope_module):
        future = datetime.now(timezone.utc).timestamp() + 60
        header = formatdate(timeval=future, usegmt=True)
        result = dashscope_module.parse_retry_after(header)
        assert result is not None
        assert 30 < result < 90  # roughly 60s

    def test_http_date_past(self, dashscope_module):
        past = datetime.now(timezone.utc).timestamp() - 10
        header = formatdate(timeval=past, usegmt=True)
        result = dashscope_module.parse_retry_after(header)
        assert result == 0.5  # clamped to minimum

    def test_invalid_string(self, dashscope_module):
        assert dashscope_module.parse_retry_after("not-a-number") is None
