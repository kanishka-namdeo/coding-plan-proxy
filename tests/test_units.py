"""Tests for core unit classes and pure functions."""
import json
import time
import asyncio
import os
import sys
import logging
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from email.utils import formatdate

import pytest
import aiohttp


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
# TokenWindowCounter
# ---------------------------------------------------------------------------

class TestTokenWindowCounter:
    def test_reserve_success(self, dashscope_module):
        counter = dashscope_module.TokenWindowCounter(capacity=1000)
        now = time.monotonic()
        assert counter.try_reserve(100, now=now) is True
        assert counter.reserved == 100
        assert counter.available(now=now) == 900.0

    def test_reserve_failure_insufficient_tokens(self, dashscope_module):
        counter = dashscope_module.TokenWindowCounter(capacity=1000)
        now = time.monotonic()
        assert counter.try_reserve(900, now=now) is True
        assert counter.available(now=now) == 100.0
        assert counter.try_reserve(200, now=now) is False

    def test_reserve_zero_tokens_always_succeeds(self, dashscope_module):
        counter = dashscope_module.TokenWindowCounter(capacity=100)
        assert counter.try_reserve(0) is True

    def test_reconcile_actual_greater_than_estimated(self, dashscope_module):
        """If actual usage > estimated, record full actual amount in the window."""
        counter = dashscope_module.TokenWindowCounter(capacity=1000)
        now = time.monotonic()
        counter.try_reserve(100, now=now)
        counter.reconcile(estimated=100, actual=150, now=now)
        assert counter.reserved == 0
        # Full actual amount (150) recorded in the window
        avail = counter.available(now=now)
        assert avail == 850.0

    def test_reconcile_actual_less_than_estimated(self, dashscope_module):
        """If actual usage < estimated, only actual amount is recorded."""
        counter = dashscope_module.TokenWindowCounter(capacity=1000)
        now = time.monotonic()
        counter.try_reserve(100, now=now)
        counter.reconcile(estimated=100, actual=60, now=now)
        assert counter.reserved == 0
        # Only 60 recorded (actual usage), not the estimated 100
        assert counter.available(now=now) == 940.0

    def test_refund_tokens(self, dashscope_module):
        counter = dashscope_module.TokenWindowCounter(capacity=1000)
        now = time.monotonic()
        counter.try_reserve(200, now=now)
        assert counter.reserved == 200
        counter.refund(200)
        assert counter.reserved == 0
        assert counter.available(now=now) == 1000.0

    def test_available_decreases_as_tokens_consumed(self, dashscope_module):
        """TPM available should decrease as tokens are consumed, not refill instantly."""
        counter = dashscope_module.TokenWindowCounter(capacity=10000, window_seconds=60)
        now = time.monotonic()
        # Consume 3000 tokens
        counter.try_reserve(3000, now=now)
        counter.reconcile(estimated=3000, actual=3000, now=now)
        # Available should be 7000, not back to 10000
        assert counter.available(now=now) == 7000.0
        # Consume another 2000
        counter.try_reserve(2000, now=now)
        counter.reconcile(estimated=2000, actual=2000, now=now)
        assert counter.available(now=now) == 5000.0

    def test_tokens_expire_after_window(self, dashscope_module):
        """Tokens should become available again after the window passes."""
        counter = dashscope_module.TokenWindowCounter(capacity=1000, window_seconds=10)
        now = time.monotonic()
        counter.try_reserve(500, now=now)
        counter.reconcile(estimated=500, actual=500, now=now)
        assert counter.available(now=now) == 500.0
        # After 11 seconds, the consumption event should have expired
        assert counter.available(now=now + 11) == 1000.0

    def test_status_dict_shape(self, dashscope_module):
        counter = dashscope_module.TokenWindowCounter(capacity=1000)
        status = counter.status()
        assert "tpm_capacity" in status
        assert "tpm_available" in status
        assert "tpm_reserved" in status
        assert status["tpm_capacity"] == 1000

    def test_available_never_negative(self, dashscope_module):
        counter = dashscope_module.TokenWindowCounter(capacity=100)
        now = time.monotonic()
        counter.try_reserve(100, now=now)
        counter.reconcile(estimated=100, actual=100)
        assert counter.available(now=now) >= 0.0

    def test_refund_cannot_go_negative(self, dashscope_module):
        """Refunding more than reserved should clamp to 0."""
        counter = dashscope_module.TokenWindowCounter(capacity=1000)
        counter.reserved = 50
        counter.refund(200)
        assert counter.reserved == 0


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

class TestTokenExtraction:
    def test_extract_tokens_from_json_response(self, dashscope_module):
        body = json.dumps({
            "usage": {"total_tokens": 42, "prompt_tokens": 10, "completion_tokens": 32}
        }).encode()
        result = dashscope_module.extract_tokens_from_response(body)
        assert isinstance(result, dict)
        assert result["total_tokens"] == 42
        assert result["prompt_tokens"] == 10
        assert result["completion_tokens"] == 32

    def test_extract_tokens_missing_usage(self, dashscope_module):
        body = json.dumps({"choices": []}).encode()
        result = dashscope_module.extract_tokens_from_response(body)
        assert isinstance(result, dict)
        assert result["total_tokens"] == 0

    def test_extract_tokens_invalid_json(self, dashscope_module):
        result = dashscope_module.extract_tokens_from_response(b"not json")
        assert isinstance(result, dict)
        assert result["total_tokens"] == 0

    def test_extract_tokens_from_stream(self, dashscope_module):
        sse = b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\ndata: {"usage": {"total_tokens": 55}}\n\ndata: [DONE]\n\n'
        result = dashscope_module.extract_tokens_from_stream(sse)
        assert isinstance(result, dict)
        assert result["total_tokens"] == 55

    def test_extract_tokens_from_stream_no_usage(self, dashscope_module):
        sse = b'data: {"choices": []}\n\ndata: [DONE]\n\n'
        result = dashscope_module.extract_tokens_from_stream(sse)
        assert isinstance(result, dict)
        assert result["total_tokens"] == 0

    def test_extract_tokens_cached_tokens(self, dashscope_module):
        body = json.dumps({
            "usage": {
                "total_tokens": 100,
                "prompt_tokens": 60,
                "completion_tokens": 40,
                "input_tokens_details": {"cached_tokens": 20}
            }
        }).encode()
        result = dashscope_module.extract_tokens_from_response(body)
        assert result["cached_tokens"] == 20

    def test_extract_tokens_cached_direct(self, dashscope_module):
        body = json.dumps({
            "usage": {
                "total_tokens": 100,
                "cached_tokens": 15
            }
        }).encode()
        result = dashscope_module.extract_tokens_from_response(body)
        assert result["cached_tokens"] == 15

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
# normalize_model_name
# ---------------------------------------------------------------------------

class TestNormalizeModelName:
    def test_mimo_v25_hyphen_alias(self, dashscope_module):
        assert dashscope_module.normalize_model_name("mimo-v2-5") == "mimo-v2.5"

    def test_mimo_v25_pro_hyphen_alias(self, dashscope_module):
        assert dashscope_module.normalize_model_name("mimo-v2-5-pro") == "mimo-v2.5-pro"

    def test_canonical_mimo_names_unchanged(self, dashscope_module):
        assert dashscope_module.normalize_model_name("mimo-v2.5-pro") == "mimo-v2.5-pro"
        assert dashscope_module.normalize_model_name("mimo-v2-pro") == "mimo-v2-pro"

    def test_primary_models_unchanged(self, dashscope_module):
        assert dashscope_module.normalize_model_name("qwen3-coder-plus") == "qwen3-coder-plus"


# ---------------------------------------------------------------------------
# parse_retry_after
# ---------------------------------------------------------------------------

class TestParseRetryAfter:
    def test_delta_seconds(self, dashscope_module):
        assert dashscope_module.parse_retry_after("30") == 30.0

    def test_delta_seconds_float(self, dashscope_module):
        assert dashscope_module.parse_retry_after("2.5") == 2.5

    def test_http_date_future(self, dashscope_module):
        # Use a fixed date 60 seconds in the future
        now = datetime.now(timezone.utc)
        future_dt = now + __import__("datetime").timedelta(seconds=60)
        header = formatdate(timeval=future_dt.timestamp(), usegmt=True)
        # parse_retry_after calls datetime.now(timezone.utc) internally
        # We mock at the module level where the function is used
        with patch("dashscope_proxy.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.timezone = timezone
            mock_dt.datetime = datetime
            result = dashscope_module.parse_retry_after(header)
        assert result is not None
        assert 55 < result < 65

    def test_http_date_past(self, dashscope_module):
        now = datetime.now(timezone.utc)
        past_dt = now - __import__("datetime").timedelta(seconds=10)
        header = formatdate(timeval=past_dt.timestamp(), usegmt=True)
        with patch("dashscope_proxy.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.timezone = timezone
            mock_dt.datetime = datetime
            result = dashscope_module.parse_retry_after(header)
        assert result == 0.5  # clamped to minimum

    def test_invalid_string(self, dashscope_module):
        assert dashscope_module.parse_retry_after("not-a-number") is None

    def test_empty_string(self, dashscope_module):
        assert dashscope_module.parse_retry_after("") is None


# ---------------------------------------------------------------------------
# should_retry_429
# ---------------------------------------------------------------------------

class TestShouldRetry429:
    def test_transient_rate_limit_retries(self, dashscope_module):
        body = b'{"error":{"code":"rate_limit_exceeded","message":"Rate limit reached"}}'
        assert dashscope_module.should_retry_429(body) is True

    def test_quota_exceeded_no_retry(self, dashscope_module):
        body = b'{"error":{"code":"throttling","message":"usage allocated quota exceeded. please try again later."}}'
        assert dashscope_module.should_retry_429(body) is False

    def test_insufficient_quota_code_no_retry(self, dashscope_module):
        body = b'{"error":{"code":"insufficient_quota","message":"You exceeded your current quota"}}'
        assert dashscope_module.should_retry_429(body) is False

    def test_empty_body_retries(self, dashscope_module):
        assert dashscope_module.should_retry_429(b"") is True


# ---------------------------------------------------------------------------
# _strip_hop_by_hop
# ---------------------------------------------------------------------------

class TestStripHopByHop:
    def test_removes_hop_by_hop_headers(self, dashscope_module):
        headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "X-Custom": "value",
        }
        result = dashscope_module._strip_hop_by_hop(headers)
        assert "Connection" not in result
        assert "Transfer-Encoding" not in result
        assert result["Content-Type"] == "application/json"
        assert result["X-Custom"] == "value"

    def test_preserves_normal_headers(self, dashscope_module):
        headers = {
            "X-Request-ID": "abc123",
            "Cache-Control": "no-cache",
            "Content-Type": "text/plain",
        }
        result = dashscope_module._strip_hop_by_hop(headers)
        assert result == headers


# ---------------------------------------------------------------------------
# _compute_backoff
# ---------------------------------------------------------------------------

class TestComputeBackoff:
    def test_backoff_grows_with_attempt(self, dashscope_module):
        config = {
            "rpm_limit": 100, "tpm_limit": 1_000_000, "safety_factor": 0.8,
            "requests_per_5h": 1000, "requests_per_week": 1000,
            "requests_per_month": 1000, "max_queue_size": 10,
            "max_retries": 5, "base_backoff": 1.0,
        }
        rl = dashscope_module.RateLimiter(config)
        # Seed random to make the test deterministic
        with patch.object(dashscope_module.random, "uniform", return_value=1.0):
            b1 = dashscope_module._compute_backoff(rl, 1)
            b3 = dashscope_module._compute_backoff(rl, 3)
            b5 = dashscope_module._compute_backoff(rl, 5)
        # With jitter fixed at 1.0: b1=2, b3=8, b5=32
        assert b5 > b3 > b1
        assert b1 == 2.0
        assert b3 == 8.0
        assert b5 == 32.0

    def test_backoff_respects_base_config(self, dashscope_module):
        config = {
            "rpm_limit": 100, "tpm_limit": 1_000_000, "safety_factor": 0.8,
            "requests_per_5h": 1000, "requests_per_week": 1000,
            "requests_per_month": 1000, "max_queue_size": 10,
            "max_retries": 5, "base_backoff": 0.5,
        }
        rl = dashscope_module.RateLimiter(config)
        with patch.object(dashscope_module.random, "uniform", return_value=1.0):
            b = dashscope_module._compute_backoff(rl, 0)
        # 0.5 * 2^0 * 1.0 = 0.5
        assert b == 0.5


# ---------------------------------------------------------------------------
# _is_chat_endpoint
# ---------------------------------------------------------------------------

class TestIsChatEndpoint:
    def test_matches_chat_completions(self, dashscope_module):
        assert dashscope_module._is_chat_endpoint("/v1/chat/completions") is True
        assert dashscope_module._is_chat_endpoint("/chat/completions") is True
        assert dashscope_module._is_chat_endpoint("/V1/CHAT/COMPLETIONS") is True

    def test_non_chat_endpoints(self, dashscope_module):
        assert dashscope_module._is_chat_endpoint("/health") is False
        assert dashscope_module._is_chat_endpoint("/v1/models") is False
        assert dashscope_module._is_chat_endpoint("/v1/proxy/status") is False


# ---------------------------------------------------------------------------
# _add_forwarded_headers
# ---------------------------------------------------------------------------

class TestAddForwardedHeaders:
    def test_forwards_meaningful_headers_strips_hop_by_hop(self, dashscope_module):
        response = MagicMock()
        response.headers = {}
        upstream = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "X-Request-ID": "abc123",
            "X-RateLimit-Limit": "100",
            "Content-Length": "512",
        }
        dashscope_module._add_forwarded_headers(response, upstream)
        assert response.headers.get("X-Request-ID") == "abc123"
        assert response.headers.get("X-RateLimit-Limit") == "100"
        assert "Connection" not in response.headers
        # Content-Type and Content-Length should NOT be forwarded (explicitly excluded)
        assert "Content-Type" not in response.headers
        assert "Content-Length" not in response.headers

    def test_works_with_client_response(self, dashscope_module):
        response = MagicMock()
        response.headers = {}
        # Simulate what _add_forwarded_headers does when given an object with .headers
        mock_resp_headers = {
            "X-Custom-Header": "val",
            "Keep-Alive": "timeout=5",
        }
        # Test the dict path (the function checks isinstance for ClientResponse)
        dashscope_module._add_forwarded_headers(response, mock_resp_headers)
        assert response.headers.get("X-Custom-Header") == "val"
        assert "Keep-Alive" not in response.headers


# ---------------------------------------------------------------------------
# estimate_tokens_for_request edge cases
# ---------------------------------------------------------------------------

class TestEstimateTokensEdgeCases:
    def test_non_list_messages(self, dashscope_module):
        body = json.dumps({"messages": "not a list"}).encode()
        assert dashscope_module.estimate_tokens_for_request(body) == 100

    def test_content_not_string(self, dashscope_module):
        body = json.dumps({
            "messages": [{"role": "user", "content": ["array", "content"]}]
        }).encode()
        # Non-string content is skipped, total_chars = 0, clamped to 100
        assert dashscope_module.estimate_tokens_for_request(body) == 100

    def test_extremely_long_content(self, dashscope_module):
        long_content = "x" * 100_000
        body = json.dumps({
            "messages": [{"role": "user", "content": long_content}]
        }).encode()
        est = dashscope_module.estimate_tokens_for_request(body)
        assert est == 100_000 // 4  # ~25000

    def test_invalid_json(self, dashscope_module):
        assert dashscope_module.estimate_tokens_for_request(b"not json") == 100

    def test_missing_messages_field(self, dashscope_module):
        body = json.dumps({"model": "qwen3-coder-plus"}).encode()
        assert dashscope_module.estimate_tokens_for_request(body) == 100


# ---------------------------------------------------------------------------
# extract_tokens_from_stream edge cases
# ---------------------------------------------------------------------------

class TestExtractStreamTokensEdgeCases:
    def test_empty_buffer(self, dashscope_module):
        result = dashscope_module.extract_tokens_from_stream(b"")
        assert isinstance(result, dict)
        assert result["total_tokens"] == 0

    def test_only_done_marker(self, dashscope_module):
        result = dashscope_module.extract_tokens_from_stream(b"data: [DONE]\n\n")
        assert isinstance(result, dict)
        assert result["total_tokens"] == 0

    def test_malformed_utf8(self, dashscope_module):
        # Invalid UTF-8 bytes
        data = b'\xff\xfe data: {"usage": {"total_tokens": 10}}'
        # Should not raise, return dict with total_tokens=0
        result = dashscope_module.extract_tokens_from_stream(data)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# StructuredLogFormatter
# ---------------------------------------------------------------------------

class TestStructuredLogFormatter:
    def test_json_output_shape(self, dashscope_module):
        formatter = dashscope_module.StructuredLogFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=1,
            msg="test message", args=(), exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "timestamp" in data
        assert "level" in data
        assert "logger" in data
        assert "message" in data
        assert data["message"] == "test message"

    def test_extra_context_included(self, dashscope_module):
        formatter = dashscope_module.StructuredLogFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=1,
            msg="with context", args=(), exc_info=None,
        )
        record.extra_context = {"request_id": "abc", "status": 200}
        output = formatter.format(record)
        data = json.loads(output)
        assert data["request_id"] == "abc"
        assert data["status"] == 200

    def test_exception_included(self, dashscope_module):
        formatter = dashscope_module.StructuredLogFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=1,
            msg="error occurred", args=(), exc_info=exc_info,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "exception" in data
        assert "ValueError" in data["exception"]
        assert "test error" in data["exception"]


# ---------------------------------------------------------------------------
# wait_for_slot
# ---------------------------------------------------------------------------

class TestWaitForSlot:
    @pytest.mark.asyncio
    async def test_immediate_success_under_limits(self, dashscope_module):
        config = {
            "rpm_limit": 100, "tpm_limit": 1_000_000, "safety_factor": 0.8,
            "requests_per_5h": 1000, "requests_per_week": 1000,
            "requests_per_month": 1000, "max_queue_size": 10,
            "max_retries": 5, "base_backoff": 0.1,
        }
        rl = dashscope_module.RateLimiter(config)
        req = MagicMock()
        req.transport = MagicMock()
        req.transport.is_closing.return_value = False
        result = await dashscope_module.wait_for_slot(rl, req, estimated_tokens=0, deadline_seconds=5.0)
        assert result is not None  # should succeed immediately
        assert result == 0.0  # no wait needed

    @pytest.mark.asyncio
    async def test_none_when_queue_full(self, dashscope_module):
        config = {
            "rpm_limit": 100, "tpm_limit": 1_000_000, "safety_factor": 0.8,
            "requests_per_5h": 1000, "requests_per_week": 1000,
            "requests_per_month": 1000, "max_queue_size": 0,
            "max_retries": 5, "base_backoff": 0.1,
        }
        rl = dashscope_module.RateLimiter(config)
        rl.pending_requests = 1  # exceeds max_queue_size (0)
        req = MagicMock()
        req.transport = MagicMock()
        req.transport.is_closing.return_value = False
        result = await dashscope_module.wait_for_slot(rl, req, estimated_tokens=0, deadline_seconds=5.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_none_when_client_disconnects(self, dashscope_module):
        config = {
            "rpm_limit": 10,  # after safety_factor 0.8 -> 8, not 0
            "tpm_limit": 1_000_000, "safety_factor": 0.8,
            "requests_per_5h": 1000, "requests_per_week": 1000,
            "requests_per_month": 1000, "max_queue_size": 10,
            "max_retries": 5, "base_backoff": 0.01,
        }
        rl = dashscope_module.RateLimiter(config)
        # Exhaust RPM so it must wait (RPM limit = int(10 * 0.8) = 8)
        for _ in range(8):
            rl.rpm_window.add(now=time.monotonic())
        req = MagicMock()
        req.transport = MagicMock()
        # Client disconnects during wait
        req.transport.is_closing.return_value = True
        result = await dashscope_module.wait_for_slot(rl, req, estimated_tokens=0, deadline_seconds=5.0)
        assert result is None


# ---------------------------------------------------------------------------
# SessionLogWriter
# ---------------------------------------------------------------------------

class TestSessionLogWriter:
    def test_creates_directory_and_file(self, dashscope_module, tmp_path):
        log_dir = tmp_path / "logs"
        assert not log_dir.exists()
        writer = dashscope_module.SessionLogWriter(str(log_dir))
        writer.log({"request_id": "abc123"})
        writer.close()

        assert log_dir.exists()
        today = dashscope_module.datetime.now(dashscope_module.timezone.utc).strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.jsonl"
        assert log_file.exists()

        content = log_file.read_text(encoding="utf-8")
        entry = json.loads(content.strip().split("\n")[0])
        assert entry["request_id"] == "abc123"

    def test_appends_multiple_entries(self, dashscope_module, tmp_path):
        log_dir = tmp_path / "logs2"
        writer = dashscope_module.SessionLogWriter(str(log_dir))
        writer.log({"request_id": "req1"})
        writer.log({"request_id": "req2"})
        writer.log({"request_id": "req3"})
        writer.close()

        today = dashscope_module.datetime.now(dashscope_module.timezone.utc).strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.jsonl"
        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3
        assert json.loads(lines[0])["request_id"] == "req1"
        assert json.loads(lines[2])["request_id"] == "req3"

    def test_each_line_is_valid_json(self, dashscope_module, tmp_path):
        log_dir = tmp_path / "logs3"
        writer = dashscope_module.SessionLogWriter(str(log_dir))
        entries = [
            {"request_id": "a", "status_code": 200, "model": "qwen3-coder-plus"},
            {"request_id": "b", "status_code": 429, "error_reason": "max_retries_429"},
            {"request_id": "c", "status_code": 500, "actual_tokens": 1200},
        ]
        for entry in entries:
            writer.log(entry)
        writer.close()

        today = dashscope_module.datetime.now(dashscope_module.timezone.utc).strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.jsonl"
        for line in log_file.read_text(encoding="utf-8").strip().split("\n"):
            parsed = json.loads(line)
            assert "request_id" in parsed

    def test_close_is_noop_when_already_closed(self, dashscope_module, tmp_path):
        log_dir = tmp_path / "logs4"
        writer = dashscope_module.SessionLogWriter(str(log_dir))
        writer.log({"request_id": "x"})
        writer.close()
        writer.close()

    def test_log_entry_schema_fields(self, dashscope_module, tmp_path):
        log_dir = tmp_path / "logs5"
        writer = dashscope_module.SessionLogWriter(str(log_dir))
        entry = {
            "request_id": "test123",
            "method": "POST",
            "path": "/v1/chat/completions",
            "model": "qwen3-coder-plus",
            "is_stream": True,
            "estimated_tokens": 500,
            "status_code": 200,
            "actual_tokens": 620,
            "duration_ms": 1234.5,
            "queue_wait_ms": 0.0,
            "retry_count": 0,
            "error_reason": None,
            "timestamp_utc": dashscope_module.datetime.now(dashscope_module.timezone.utc).isoformat(),
        }
        writer.log(entry)
        writer.close()

        today = dashscope_module.datetime.now(dashscope_module.timezone.utc).strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.jsonl"
        parsed = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert parsed["status_code"] == 200
        assert parsed["actual_tokens"] == 620
        assert parsed["duration_ms"] == 1234.5
        assert parsed["error_reason"] is None
        assert parsed["is_stream"] is True


# ---------------------------------------------------------------------------
# TUILogHandler
# ---------------------------------------------------------------------------

class TestTUILogHandler:
    def test_emit_and_get_logs(self, dashscope_module):
        handler = dashscope_module.TUILogHandler(max_size=100)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        handler.emit(record)
        logs = handler.get_logs()
        assert len(logs) == 1
        assert logs[0]["message"] == "hello"
        assert logs[0]["level"] == "INFO"
        assert "seq" in logs[0]

    def test_sequence_numbers_monotonic(self, dashscope_module):
        handler = dashscope_module.TUILogHandler(max_size=100)
        for i in range(5):
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=1,
                msg=f"msg-{i}", args=(), exc_info=None,
            )
            handler.emit(record)
        logs = handler.get_logs()
        seqs = [e["seq"] for e in logs]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == 5  # all unique

    def test_get_logs_from_seq(self, dashscope_module):
        handler = dashscope_module.TUILogHandler(max_size=100)
        for i in range(5):
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=1,
                msg=f"msg-{i}", args=(), exc_info=None,
            )
            handler.emit(record)
        # Get only entries with seq >= 3
        logs = handler.get_logs(from_seq=3)
        assert len(logs) == 2
        assert all(e["seq"] >= 3 for e in logs)

    def test_clear_resets_seq(self, dashscope_module):
        handler = dashscope_module.TUILogHandler(max_size=100)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        handler.emit(record)
        assert handler._next_seq == 1
        handler.clear()
        assert handler._next_seq == 0
        assert len(handler.get_logs()) == 0

    def test_no_dropped_logs_on_wrap(self, dashscope_module):
        """When buffer wraps, seq-based retrieval should still work."""
        handler = dashscope_module.TUILogHandler(max_size=3)
        for i in range(5):
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=1,
                msg=f"msg-{i}", args=(), exc_info=None,
            )
            handler.emit(record)
        # Buffer holds last 3: msg-2, msg-3, msg-4
        logs = handler.get_logs(from_seq=0)
        assert len(logs) == 3
        msgs = [e["message"] for e in logs]
        assert msgs == ["msg-2", "msg-3", "msg-4"]


# ---------------------------------------------------------------------------
# _client_disconnected
# ---------------------------------------------------------------------------

class TestClientDisconnected:
    def test_disconnected_when_transport_closing(self, dashscope_module):
        req = MagicMock()
        req.transport.is_closing.return_value = True
        assert dashscope_module._client_disconnected(req) is True

    def test_connected_when_transport_open(self, dashscope_module):
        req = MagicMock()
        req.transport.is_closing.return_value = False
        assert dashscope_module._client_disconnected(req) is False

    def test_no_transport_attribute(self, dashscope_module):
        req = MagicMock(spec=[])  # no transport attribute
        assert dashscope_module._client_disconnected(req) is False

    def test_transport_without_is_closing(self, dashscope_module):
        req = MagicMock()
        req.transport = MagicMock(spec=[])  # no is_closing
        assert dashscope_module._client_disconnected(req) is False


# ---------------------------------------------------------------------------
# _make_error_response
# ---------------------------------------------------------------------------

class TestMakeErrorResponse:
    def test_basic_response(self, dashscope_module):
        resp = dashscope_module._make_error_response(400, b'{"error":"bad"}', "req123")
        assert resp.status == 400
        assert resp.headers.get("X-Request-ID") == "req123"
        assert resp.content_type == "application/json"

    def test_response_with_retry_after(self, dashscope_module):
        resp = dashscope_module._make_error_response(503, b'{"error":"busy"}', "req123", retry_after=30)
        assert resp.status == 503
        assert resp.headers.get("Retry-After") == "30"

    def test_response_without_retry_after(self, dashscope_module):
        resp = dashscope_module._make_error_response(400, b'{"error":"bad"}', "req123", retry_after=None)
        assert "Retry-After" not in resp.headers


# ---------------------------------------------------------------------------
# RateLimiter token management methods
# ---------------------------------------------------------------------------

class TestRateLimiterTokenManagement:
    @pytest.mark.asyncio
    async def test_reserve_tokens_success(self, rate_limiter):
        assert await rate_limiter.reserve_tokens(100) is True

    @pytest.mark.asyncio
    async def test_reserve_tokens_zero(self, rate_limiter):
        assert await rate_limiter.reserve_tokens(0) is True

    @pytest.mark.asyncio
    async def test_reconcile_tokens(self, rate_limiter):
        await rate_limiter.reserve_tokens(100)
        await rate_limiter.reconcile_tokens(100, 120)
        # Should release reservation and drain extra 20 from bucket

    @pytest.mark.asyncio
    async def test_refund_tokens(self, rate_limiter):
        await rate_limiter.reserve_tokens(100)
        await rate_limiter.refund_tokens(100)
        # Should release the reservation

    @pytest.mark.asyncio
    async def test_remaining_tpm(self, rate_limiter):
        tpm = await rate_limiter.remaining_tpm()
        assert isinstance(tpm, int)
        assert tpm >= 0

    def test_is_queue_full(self, rate_limiter):
        assert rate_limiter.is_queue_full() is False
        rate_limiter.pending_requests = rate_limiter.max_queue_size + 1
        assert rate_limiter.is_queue_full() is True

    def test_is_queue_full_boundary(self, rate_limiter):
        rate_limiter.pending_requests = rate_limiter.max_queue_size
        assert rate_limiter.is_queue_full() is False
        rate_limiter.pending_requests = rate_limiter.max_queue_size + 1
        assert rate_limiter.is_queue_full() is True


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    async def test_circuit_closed_initially(self, rate_limiter):
        assert rate_limiter.circuit_is_open() is False

    async def test_circuit_opens_after_threshold(self, rate_limiter):
        rate_limiter.circuit_threshold = 3
        await rate_limiter.record_circuit_failure()
        await rate_limiter.record_circuit_failure()
        assert rate_limiter.circuit_is_open() is False  # not yet
        await rate_limiter.record_circuit_failure()
        assert rate_limiter.circuit_is_open() is True  # opened

    async def test_circuit_closes_on_success(self, rate_limiter):
        rate_limiter.circuit_threshold = 1
        await rate_limiter.record_circuit_failure()
        assert rate_limiter.circuit_is_open() is True
        await rate_limiter.record_circuit_success()
        assert rate_limiter.circuit_is_open() is False
        assert rate_limiter.circuit_failure_count == 0

    async def test_status_includes_circuit_fields(self, rate_limiter):
        status = rate_limiter.status()
        assert "circuit_open" in status
        assert "circuit_failure_count" in status

# ---------------------------------------------------------------------------
# SessionLogWriter edge cases
# ---------------------------------------------------------------------------

class TestSessionLogWriterEdgeCases:
    def test_file_rotation_across_midnight(self, dashscope_module, tmp_path):
        log_dir = tmp_path / "rotation_logs"
        writer = dashscope_module.SessionLogWriter(str(log_dir))
        # Start with a mocked date
        old_dt = dashscope_module.datetime(2025, 1, 1, tzinfo=dashscope_module.timezone.utc)
        new_dt = dashscope_module.datetime(2025, 6, 2, tzinfo=dashscope_module.timezone.utc)

        # First log with old date
        with patch("dashscope_proxy.datetime") as mock_dt:
            mock_dt.now.return_value = old_dt
            mock_dt.timezone = dashscope_module.timezone
            mock_dt.datetime = dashscope_module.datetime
            writer.log({"request_id": "req1"})

        # Simulate date change in internal state and log with new date
        writer._current_date = "2025-01-01"
        with patch("dashscope_proxy.datetime") as mock_dt:
            mock_dt.now.return_value = new_dt
            mock_dt.timezone = dashscope_module.timezone
            mock_dt.datetime = dashscope_module.datetime
            writer.log({"request_id": "req2"})
        writer.close()

        old_file = log_dir / "2025-01-01.jsonl"
        new_file = log_dir / "2025-06-02.jsonl"
        assert old_file.exists()
        assert new_file.exists()
        assert json.loads(old_file.read_text().strip())["request_id"] == "req1"
        assert json.loads(new_file.read_text().strip())["request_id"] == "req2"


# ---------------------------------------------------------------------------
# TokenWindowCounter edge cases
# ---------------------------------------------------------------------------

class TestTokenWindowCounterEdgeCases:
    def test_prune_with_negative_elapsed(self, dashscope_module):
        """Clock adjustment: should handle time going backwards gracefully."""
        counter = dashscope_module.TokenWindowCounter(capacity=1000, window_seconds=60)
        future = time.monotonic() + 100
        counter.try_reserve(500, now=future)
        counter.reconcile(estimated=500, actual=500)
        # available() should handle and not go negative
        result = counter.available(now=time.monotonic())
        assert result >= 0

    def test_wait_seconds_for_no_wait_needed(self, dashscope_module):
        """When tokens are available, wait should be 0."""
        counter = dashscope_module.TokenWindowCounter(capacity=1000)
        now = time.monotonic()
        assert counter.wait_seconds_for(100, now=now) == 0.0

    def test_wait_seconds_for_when_window_full(self, dashscope_module):
        """Wait should indicate when oldest tokens expire."""
        counter = dashscope_module.TokenWindowCounter(capacity=1000, window_seconds=60)
        now = time.monotonic()
        counter.try_reserve(800, now=now)
        counter.reconcile(estimated=800, actual=800, now=now)
        # Need 500 more but only 200 available; must wait for the 800 to expire
        wait = counter.wait_seconds_for(500, now=now)
        assert wait > 0


# ---------------------------------------------------------------------------
# SlidingWindowCounter thread safety
# ---------------------------------------------------------------------------

class TestSlidingWindowCounterThreadSafety:
    def test_concurrent_add_and_count(self, dashscope_module):
        """Multiple threads adding and counting simultaneously should not raise."""
        import threading
        sw = dashscope_module.SlidingWindowCounter(60, max_size=1000)
        errors = []

        def add_events():
            try:
                for i in range(50):
                    sw.add(now=100.0 + i)
            except Exception as e:
                errors.append(e)

        def count_events():
            try:
                for _ in range(50):
                    sw.count(now=100.0 + 25)
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(4):
            t = threading.Thread(target=add_events)
            threads.append(t)
            t = threading.Thread(target=count_events)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"


# ---------------------------------------------------------------------------
# _load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_default_config(self, dashscope_module, monkeypatch):
        # Ensure no env vars interfere
        for key in dashscope_module.CODING_PLAN_CONFIG:
            monkeypatch.delenv(f"PROXY_{key.upper()}", raising=False)
        config = dashscope_module._load_config()
        assert config == dashscope_module.CODING_PLAN_CONFIG

    def test_env_var_override(self, dashscope_module, monkeypatch):
        monkeypatch.delenv("PROXY_TPM_LIMIT", raising=False)
        monkeypatch.setenv("PROXY_RPM_LIMIT", "24")
        monkeypatch.setenv("PROXY_MAX_RETRIES", "10")
        config = dashscope_module._load_config()
        assert config["rpm_limit"] == 24
        assert config["max_retries"] == 10
        # Other keys unchanged
        assert config["tpm_limit"] == dashscope_module.CODING_PLAN_CONFIG["tpm_limit"]

    def test_invalid_env_var_uses_default(self, dashscope_module, monkeypatch):
        monkeypatch.setenv("PROXY_RPM_LIMIT", "not-a-number")
        config = dashscope_module._load_config()
        assert config["rpm_limit"] == dashscope_module.CODING_PLAN_CONFIG["rpm_limit"]


# ---------------------------------------------------------------------------
# ProviderRouter
# ---------------------------------------------------------------------------

class TestProviderRouter:
    def test_primary_always_available(self, dashscope_module):
        router = dashscope_module.ProviderRouter()
        assert router.primary.is_available is True

    def test_get_provider_for_model_defaults_to_primary(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_API_KEY", "")
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_BASE_URL", "")
        router = dashscope_module.ProviderRouter()
        provider = router.get_provider_for_model("qwen3-coder-plus")
        assert provider.name == "primary"

    def test_secondary_model_routed_to_secondary(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_API_KEY", "sk-test")
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_BASE_URL", "https://secondary.example.com/v1")
        router = dashscope_module.ProviderRouter()
        assert router.secondary.is_available is True
        provider = router.get_provider_for_model("mimo-v2.5-pro")
        assert provider.name == "secondary"

    def test_secondary_model_hyphen_alias_routed_to_secondary(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_API_KEY", "sk-test")
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_BASE_URL", "https://secondary.example.com/v1")
        router = dashscope_module.ProviderRouter()
        provider = router.get_provider_for_model("mimo-v2-5-pro")
        assert provider.name == "secondary"

    def test_mimo_v25_base_hyphen_alias_routed_to_secondary(self, dashscope_module, monkeypatch):
        """Cursor sends 'mimo-v2-5' (no suffix) for the base model."""
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_API_KEY", "sk-test")
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_BASE_URL", "https://secondary.example.com/v1")
        router = dashscope_module.ProviderRouter()
        provider = router.get_provider_for_model("mimo-v2-5")
        assert provider.name == "secondary"

    @pytest.mark.parametrize("model_id", [
        "mimo-v2.5-pro", "mimo-v2.5", "mimo-v2.5-asr", "mimo-v2.5-tts",
        "mimo-v2.5-tts-voiceclone", "mimo-v2.5-tts-voicedesign",
        "mimo-v2-pro", "mimo-v2-omni", "mimo-v2-tts",
    ])
    def test_all_secondary_models_routed_to_secondary(self, dashscope_module, monkeypatch, model_id):
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_API_KEY", "sk-test")
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_BASE_URL", "https://secondary.example.com/v1")
        router = dashscope_module.ProviderRouter()
        assert router.get_provider_for_model(model_id).name == "secondary"

    @pytest.mark.parametrize("model_id", [
        "qwen3.6-plus", "qwen3.7-plus", "qwen3.5-plus", "qwen3-max",
        "qwen3-coder-plus", "qwen3-coder-next", "kimi-k2-5", "glm-5-0", "MiniMax-M2.5",
    ])
    def test_all_primary_models_routed_to_primary(self, dashscope_module, monkeypatch, model_id):
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_API_KEY", "sk-test")
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_BASE_URL", "https://secondary.example.com/v1")
        router = dashscope_module.ProviderRouter()
        assert router.get_provider_for_model(model_id).name == "primary"

    def test_unknown_model_defaults_to_primary_even_with_secondary(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_API_KEY", "sk-test")
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_BASE_URL", "https://secondary.example.com/v1")
        router = dashscope_module.ProviderRouter()
        provider = router.get_provider_for_model("qwen3-coder-plus")
        assert provider.name == "primary"

    def test_secondary_unconfigured_returns_primary_for_secondary_models(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_API_KEY", "")
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_BASE_URL", "")
        router = dashscope_module.ProviderRouter()
        provider = router.get_provider_for_model("mimo-v2.5-pro")
        assert provider.name == "primary"

    def test_get_all_models_includes_secondary_when_configured(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_API_KEY", "sk-test")
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_BASE_URL", "https://secondary.example.com/v1")
        router = dashscope_module.ProviderRouter()
        models = router.get_all_models()
        model_ids = [m["id"] for m in models["data"]]
        assert "qwen3-coder-plus" in model_ids
        assert "mimo-v2.5-pro" in model_ids

    def test_get_all_models_excludes_secondary_when_not_configured(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_API_KEY", "")
        monkeypatch.setattr("dashscope_proxy_lib.config.SECONDARY_BASE_URL", "")
        router = dashscope_module.ProviderRouter()
        models = router.get_all_models()
        model_ids = [m["id"] for m in models["data"]]
        assert "qwen3-coder-plus" in model_ids
        assert "mimo-v2.5-pro" not in model_ids

    def test_get_provider_status(self, dashscope_module):
        router = dashscope_module.ProviderRouter()
        status = router.get_provider_status()
        assert "primary" in status
        assert "secondary" in status
        assert "tertiary" in status
        assert "available" in status["primary"]
        assert "base_url" in status["primary"]

    def test_tertiary_model_routed_to_tertiary(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_API_KEY", "sk-streamlake")
        monkeypatch.setattr(
            "dashscope_proxy_lib.config.TERTIARY_BASE_URL",
            "https://vanchin.streamlake.ai/api/gateway/coding/v1",
        )
        router = dashscope_module.ProviderRouter()
        assert router.tertiary.is_available is True
        provider = router.get_provider_for_model("kat-coder-pro-v2.5")
        assert provider.name == "tertiary"

    def test_tertiary_unconfigured_returns_primary_for_tertiary_models(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_API_KEY", "")
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_BASE_URL", "")
        router = dashscope_module.ProviderRouter()
        provider = router.get_provider_for_model("kat-coder-pro-v2.5")
        assert provider.name == "primary"

    def test_get_all_models_includes_tertiary_when_configured(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_API_KEY", "sk-streamlake")
        monkeypatch.setattr(
            "dashscope_proxy_lib.config.TERTIARY_BASE_URL",
            "https://vanchin.streamlake.ai/api/gateway/coding/v1",
        )
        router = dashscope_module.ProviderRouter()
        models = router.get_all_models()
        model_ids = [m["id"] for m in models["data"]]
        assert "kat-coder-pro-v2.5" in model_ids

    def test_get_all_models_excludes_tertiary_when_not_configured(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_API_KEY", "")
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_BASE_URL", "")
        router = dashscope_module.ProviderRouter()
        models = router.get_all_models()
        model_ids = [m["id"] for m in models["data"]]
        assert "kat-coder-pro-v2.5" not in model_ids

    def test_model_provider_map_tertiary_override(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_API_KEY", "sk-streamlake")
        monkeypatch.setattr(
            "dashscope_proxy_lib.config.TERTIARY_BASE_URL",
            "https://vanchin.streamlake.ai/api/gateway/coding/v1",
        )
        monkeypatch.setattr(
            "dashscope_proxy_lib.config.MODEL_PROVIDER_MAP",
            {"kat-coder-pro-v2.5": "tertiary"},
        )
        router = dashscope_module.ProviderRouter()
        assert router.get_provider_for_model("kat-coder-pro-v2.5").name == "tertiary"


# ---------------------------------------------------------------------------
# MultiProviderRateLimiter
# ---------------------------------------------------------------------------

class TestMultiProviderRateLimiter:
    def _make_config(self):
        return {
            "rpm_limit": 60, "tpm_limit": 100_000, "safety_factor": 0.8,
            "requests_per_5h": 100, "requests_per_week": 100,
            "requests_per_month": 100, "max_queue_size": 5,
            "max_retries": 3, "base_backoff": 0.1,
        }

    def test_creates_primary_only(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None)
        assert mpl.primary is not None
        assert mpl.secondary is None
        assert mpl.tertiary is None

    def test_creates_both_when_secondary_config_provided(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), self._make_config())
        assert mpl.primary is not None
        assert mpl.secondary is not None
        assert mpl.tertiary is None

    def test_creates_tertiary_when_tertiary_config_provided(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None, self._make_config())
        assert mpl.primary is not None
        assert mpl.secondary is None
        assert mpl.tertiary is not None

    def test_get_limiter_for_provider_returns_secondary(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), self._make_config())
        limiter = mpl.get_limiter_for_provider("secondary")
        assert limiter is mpl.secondary

    def test_get_limiter_for_provider_returns_tertiary(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None, self._make_config())
        limiter = mpl.get_limiter_for_provider("tertiary")
        assert limiter is mpl.tertiary

    def test_get_limiter_for_provider_returns_primary_by_default(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None)
        limiter = mpl.get_limiter_for_provider("primary")
        assert limiter is mpl.primary

    def test_pending_requests_shared(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None)
        mpl.pending_requests = 5
        assert mpl.pending_requests == 5

    def test_max_queue_size_delegates_to_primary(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None)
        assert mpl.max_queue_size == 5

    def test_is_queue_full(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None)
        assert mpl.is_queue_full() is False
        mpl.pending_requests = 10
        assert mpl.is_queue_full() is True

    @pytest.mark.asyncio
    async def test_backward_compat_can_proceed(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None)
        allowed, reason, _ = await mpl.can_proceed()
        assert allowed is True
        assert reason == "ok"

    @pytest.mark.asyncio
    async def test_provider_specific_can_proceed(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), self._make_config())
        allowed, reason, _ = await mpl.can_proceed_for_provider(0, "secondary")
        assert allowed is True

    def test_status_returns_nested_structure(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None)
        status = mpl.status()
        assert "primary" in status
        assert "secondary" in status
        assert "tertiary" in status
        assert "shared_limits" in status
        assert status["shared_limits"] is True
        assert status["secondary"] is None
        assert status["tertiary"] is None

    def test_status_shows_secondary_when_configured(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), self._make_config())
        status = mpl.status()
        assert status["secondary"] is not None
        assert "rpm_limit" in status["secondary"]

    def test_status_shows_tertiary_when_configured(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None, self._make_config())
        status = mpl.status()
        assert status["tertiary"] is not None
        assert "rpm_limit" in status["tertiary"]

    @pytest.mark.asyncio
    async def test_provider_specific_can_proceed_tertiary(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None, self._make_config())
        allowed, reason, _ = await mpl.can_proceed_for_provider(0, "tertiary")
        assert allowed is True

    def test_queue_drops_property(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None)
        mpl.queue_drops = 5
        assert mpl.queue_drops == 5
        assert mpl.primary.queue_drops == 5

    def test_total_rejected_property(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None)
        mpl.total_rejected = 3
        assert mpl.total_rejected == 3
        assert mpl.primary.total_rejected == 3

    def test_rps_limit_property(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None)
        assert isinstance(mpl.rps_limit, int)
        assert mpl.rps_limit > 0

    def test_max_retries_property(self, dashscope_module):
        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), None)
        assert mpl.max_retries == 3

    def test_provider_limiter_survives_module_reload(self, dashscope_module):
        """After rate_limiter module reload, duck-typing must still resolve sub-limiters.

        TUI mode imports dashscope_proxy after the app creates MultiProviderRateLimiter.
        reload() replaces the class object so isinstance checks fail; handlers must use
        get_limiter_for_provider() instead.
        """
        import importlib
        import dashscope_proxy_lib.rate_limiter as rl_mod

        mpl = dashscope_module.MultiProviderRateLimiter(self._make_config(), self._make_config())
        importlib.reload(rl_mod)

        assert not isinstance(mpl, rl_mod.MultiProviderRateLimiter)
        limiter = mpl.get_limiter_for_provider("primary")
        assert hasattr(limiter, "circuit_is_open")
        assert limiter.circuit_is_open() is False
        secondary = mpl.get_limiter_for_provider("secondary")
        assert secondary is mpl.secondary
