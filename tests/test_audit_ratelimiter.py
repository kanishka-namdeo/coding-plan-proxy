"""Regression tests for rate limiter audit fixes (admit-lock discipline,
circuit flapping, probe-claim watchdog, record_completion semantics,
zero-limit warning, out-of-order reconcile timestamps).

Follows the test_units.py conventions: fixtures from conftest.py
(`dashscope_module`, `rate_limiter`), RateLimiter constructed via the
dashscope_proxy facade with the standard config dict shape.
"""
import time
import logging

import pytest


def _config(**overrides):
    """Standard test config dict (large limits, fast execution)."""
    cfg = {
        "rpm_limit": 6000,
        "tpm_limit": 10_000_000,
        "safety_factor": 0.8,
        "max_queue_size": 50,
        "max_retries": 2,
        "base_backoff": 0.05,
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# Fix 1: try_admit phase-4 denial must not run bucket/log work under the lock
# ---------------------------------------------------------------------------

class TestTryAdmitPhase4LockDiscipline:
    @pytest.mark.asyncio
    async def test_phase4_refund_runs_outside_admit_lock(self, dashscope_module):
        """Phase-4 RPS re-check denial: the TPM refund must run after the lock release.

        A genuine phase-4 loser can only exist when another request stamps
        ``last_request_time`` while this request is between its phase-2 and
        phase-4 checks. In single-loop asyncio that interleaving is forced by
        simulating the competitor's stamp inside the ``try_reserve`` spy
        (i.e. during this request's "TPM work").
        """
        rl = dashscope_module.RateLimiter(_config())
        rl.rps_limit = 1.0  # 1 rps -> 1s spacing
        rl.last_request_time = time.monotonic() - 2.0  # phase-2 pre-check passes

        held = []
        real_reserve = rl.tpm_bucket.try_reserve
        real_refund = rl.tpm_bucket.refund

        def probe_reserve(tokens, now=None):
            outcome = real_reserve(tokens, now)
            # Simulate a competitor that was admitted during this request's
            # TPM work: its stamp lands between our phase-2 and phase-4 checks.
            rl.last_request_time = time.monotonic()
            return outcome

        def probe_refund(tokens):
            held.append(rl._lock.locked())
            return real_refund(tokens)

        rl.tpm_bucket.try_reserve = probe_reserve  # type: ignore[method-assign]
        rl.tpm_bucket.refund = probe_refund  # type: ignore[method-assign]
        try:
            ok, reason, wait = await rl.try_admit(100)
        finally:
            rl.tpm_bucket.try_reserve = real_reserve  # type: ignore[method-assign]
            rl.tpm_bucket.refund = real_refund  # type: ignore[method-assign]

        assert ok is False
        assert reason == "RPS spacing"
        assert wait > 0
        assert len(held) == 1, "the denied loser must refund its TPM exactly once"
        assert held[0] is False, "tpm_bucket.refund must not run while the admit lock is held"
        assert rl.tpm_bucket.reserved == 0, "the loser's reservation is fully refunded"


# ---------------------------------------------------------------------------
# Fix 2: stale success must not defeat a fresh OPEN circuit
# ---------------------------------------------------------------------------

class TestCircuitFlapping:
    @pytest.mark.asyncio
    async def test_stale_success_keeps_fresh_open(self, rate_limiter):
        limiter = rate_limiter
        limiter.circuit_state = "OPEN"
        open_until = time.monotonic() + 30.0
        limiter.circuit_open_until = open_until
        limiter.circuit_failure_count = 5
        # The concurrent failure that just opened the breaker (below threshold,
        # so the OPEN deadline we set is preserved).
        assert await limiter.record_circuit_failure() is False
        assert limiter.circuit_open_until == open_until
        count_after_failure = limiter.circuit_failure_count

        # An out-of-order success arriving after the failure must not close it.
        await limiter.record_completion(
            estimated_tokens=0, actual_tokens=0, model="m",
            latency_ms=1.0, request_bytes=1, response_bytes=1,
            circuit_success=True,
        )
        assert limiter.circuit_state == "OPEN"
        assert limiter.circuit_open_until == open_until
        assert limiter.circuit_failure_count == count_after_failure
        assert limiter.circuit_is_open() is True

        # But a HALF_OPEN probe success is the intended closing path.
        limiter.circuit_state = "HALF_OPEN"
        limiter.circuit_probe_in_flight = True
        limiter.circuit_open_until = time.monotonic() + 5.0
        await limiter.record_completion(
            estimated_tokens=0, actual_tokens=0, model="m",
            latency_ms=1.0, request_bytes=1, response_bytes=1,
            circuit_success=True,
        )
        assert limiter.circuit_state == "CLOSED"
        assert limiter.circuit_probe_in_flight is False
        assert limiter.circuit_failure_count == 0
        assert limiter.circuit_open_until == 0.0

    @pytest.mark.asyncio
    async def test_record_circuit_success_keeps_fresh_open(self, rate_limiter):
        limiter = rate_limiter
        limiter.circuit_state = "OPEN"
        open_until = time.monotonic() + 30.0
        limiter.circuit_open_until = open_until
        limiter.circuit_failure_count = 7
        limiter.circuit_probe_in_flight = False

        await limiter.record_circuit_success()

        assert limiter.circuit_state == "OPEN"
        assert limiter.circuit_open_until == open_until
        assert limiter.circuit_failure_count == 7
        assert limiter.circuit_is_open() is True

    @pytest.mark.asyncio
    async def test_success_from_closed_state_still_clears_counters(self, rate_limiter):
        limiter = rate_limiter
        limiter.circuit_state = "CLOSED"
        limiter.circuit_failure_count = 3

        await limiter.record_circuit_success()

        assert limiter.circuit_state == "CLOSED"
        assert limiter.circuit_failure_count == 0
        assert limiter.circuit_open_until == 0.0

    @pytest.mark.asyncio
    async def test_success_from_closed_clears_counters_in_completion(self, rate_limiter):
        limiter = rate_limiter
        limiter.circuit_state = "CLOSED"
        limiter.circuit_failure_count = 4

        await limiter.record_completion(
            estimated_tokens=0, actual_tokens=0, model="m",
            latency_ms=1.0, request_bytes=1, response_bytes=1,
            circuit_success=True,
        )
        assert limiter.circuit_state == "CLOSED"
        assert limiter.circuit_failure_count == 0
        assert limiter.circuit_open_until == 0.0


# ---------------------------------------------------------------------------
# Fix 3: probe-claim watchdog
# ---------------------------------------------------------------------------

class TestProbeClaimWatchdog:
    def _rl(self, dashscope_module):
        return dashscope_module.RateLimiter(_config())

    def test_default_probe_max_hold(self, dashscope_module):
        rl = self._rl(dashscope_module)
        assert rl.probe_max_hold_s == 300.0
        assert rl.circuit_probe_claimed_at == 0.0

    def test_claim_stamps_claimed_at(self, dashscope_module):
        rl = self._rl(dashscope_module)
        rl.circuit_state = "OPEN"
        rl.circuit_open_until = time.monotonic() - 1.0
        before = time.monotonic()
        assert rl.can_attempt_probe() is True
        assert rl.circuit_state == "HALF_OPEN"
        assert before <= rl.circuit_probe_claimed_at <= time.monotonic()

    def test_stuck_probe_reclaimed_after_hold_window(self, dashscope_module):
        rl = self._rl(dashscope_module)
        rl.probe_max_hold_s = 5.0
        rl.circuit_state = "OPEN"
        rl.circuit_open_until = time.monotonic() - 1.0
        assert rl.can_attempt_probe() is True  # claim the probe

        # Simulate a holder that neither records a result nor releases:
        # backdate the claim past the hold window.
        rl.circuit_probe_claimed_at = time.monotonic() - 10.0
        assert rl.can_attempt_probe() is True, "watchdog must reclaim a stuck probe"
        assert rl.circuit_state == "HALF_OPEN"
        assert rl.circuit_probe_in_flight is True
        # The timestamp is reset, so immediate re-entry is denied again.
        assert rl.can_attempt_probe() is False

    def test_probe_within_hold_window_still_denied(self, dashscope_module):
        rl = self._rl(dashscope_module)
        rl.circuit_state = "HALF_OPEN"
        rl.circuit_probe_in_flight = True
        rl.circuit_probe_claimed_at = time.monotonic() - 1.0  # within the 300s default
        assert rl.can_attempt_probe() is False

    def test_open_during_cooldown_still_denied(self, dashscope_module):
        rl = self._rl(dashscope_module)
        rl.circuit_state = "OPEN"
        rl.circuit_open_until = time.monotonic() + 30.0
        assert rl.can_attempt_probe() is False


# ---------------------------------------------------------------------------
# Fix 4: record_completion(circuit_success=None) leaves circuit untouched
# ---------------------------------------------------------------------------

class TestRecordCompletionCircuitNone:
    @pytest.mark.asyncio
    async def test_none_leaves_circuit_state_untouched(self, rate_limiter):
        limiter = rate_limiter
        limiter.circuit_state = "OPEN"
        open_until = time.monotonic() + 30.0
        limiter.circuit_open_until = open_until
        limiter.circuit_failure_count = 4
        limiter.circuit_probe_in_flight = False

        await limiter.record_completion(
            estimated_tokens=0, actual_tokens=42, model="m-none",
            latency_ms=5.0, request_bytes=1, response_bytes=2,
            circuit_success=None,
        )

        # Circuit state and failure count: untouched.
        assert limiter.circuit_state == "OPEN"
        assert limiter.circuit_open_until == open_until
        assert limiter.circuit_failure_count == 4
        assert limiter.circuit_is_open() is True
        # Model stats, totals, and latency: still recorded.
        assert limiter.model_usage["m-none"].requests == 1
        assert limiter.model_usage["m-none"].tokens == 42
        assert limiter.total_forwarded == 1
        assert limiter.total_tokens_consumed == 42
        assert 5.0 in limiter.recent_latencies
        assert limiter.total_request_bytes == 1
        assert limiter.total_response_bytes == 2

    @pytest.mark.asyncio
    async def test_none_still_reconciles_tpm(self, rate_limiter):
        limiter = rate_limiter
        await limiter.try_admit(100)
        assert limiter.tpm_bucket.reserved == 100

        await limiter.record_completion(
            estimated_tokens=100, actual_tokens=60, model="m",
            latency_ms=1.0, request_bytes=1, response_bytes=1,
            circuit_success=None,
        )
        assert limiter.tpm_bucket.reserved == 0
        assert limiter.tpm_bucket._used_total == 60


# ---------------------------------------------------------------------------
# Fix 5: zero-limit WARNING (deny-all, not unlimited)
# ---------------------------------------------------------------------------

class TestZeroLimitWarning:
    _LOGGER = "dashscope_proxy_lib.logging_config"

    def test_zero_limits_emit_warning(self, dashscope_module, caplog):
        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            dashscope_module.RateLimiter(_config(rpm_limit=0, tpm_limit=0))
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING and r.name == self._LOGGER]
        assert any("deny-all" in r.message for r in warnings), \
            "expected a zero-limit deny-all warning"

    def test_zero_rpm_only_still_warns(self, dashscope_module, caplog):
        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            dashscope_module.RateLimiter(_config(rpm_limit=0))
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING and r.name == self._LOGGER]
        assert len(warnings) >= 1

    def test_nonzero_limits_emit_no_warning(self, dashscope_module, caplog):
        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            dashscope_module.RateLimiter(_config())
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING and r.name == self._LOGGER]
        assert not warnings, f"unexpected warnings: {[r.message for r in warnings]}"

    @pytest.mark.asyncio
    async def test_zero_limits_deny_all_traffic(self, dashscope_module):
        rl = dashscope_module.RateLimiter(_config(rpm_limit=0, tpm_limit=0))
        assert rl.rpm_limit == 0
        assert rl.tpm_limit == 0
        ok, reason, wait = await rl.try_admit(100)
        assert ok is False
        assert "RPM" in reason
        assert wait >= 0
        # Denial semantics unchanged: zero is deny-all, not unlimited.
        allowed, reason2, _ = await rl.can_proceed()
        assert allowed is False
        assert reason2 == "RPM limit reached"


# ---------------------------------------------------------------------------
# Fix 6: out-of-order reconcile timestamps
# ---------------------------------------------------------------------------

class TestReconcileOutOfOrder:
    def test_stale_now_keeps_window_sorted_and_consistent(self, dashscope_module):
        counter = dashscope_module.TokenWindowCounter(capacity=10_000, window_seconds=60)
        t0 = time.monotonic()
        counter.try_reserve(4000, now=t0)
        counter.reconcile(4000, 4000, now=t0)

        # Out-of-order completion carries a timestamp 10s older than the
        # last recorded one — it must be clamped to the newest stamp.
        counter.reconcile(0, 3000, now=t0 - 10)

        stamps = [ts for ts, _ in counter.window]
        assert stamps == sorted(stamps), "window must stay sorted for front-only pruning"
        assert counter._used_total == 7000
        assert counter._used_total == sum(a for _, a in counter.window)
        assert counter.available(now=t0 + 10) == 3000.0

    def test_stale_now_does_not_undercount_available(self, dashscope_module):
        counter = dashscope_module.TokenWindowCounter(capacity=10_000, window_seconds=60)
        t0 = time.monotonic()
        counter.try_reserve(4000, now=t0)
        counter.reconcile(4000, 4000, now=t0)
        counter.reconcile(0, 3000, now=t0 - 10)  # clamped to t0

        # Both events age out together from the clamped stamp, so the full
        # capacity returns — no stale entry lingers to undercount it.
        assert counter.available(now=t0 + 61) == 10_000.0
        assert counter._used_total == 0
        assert len(counter.window) == 0

    def test_in_order_reconcile_unaffected(self, dashscope_module):
        counter = dashscope_module.TokenWindowCounter(capacity=10_000, window_seconds=60)
        t0 = time.monotonic()
        counter.try_reserve(4000, now=t0)
        counter.reconcile(4000, 4000, now=t0)
        counter.try_reserve(2000, now=t0 + 5)
        counter.reconcile(2000, 2000, now=t0 + 5)
        assert counter._used_total == 6000
        assert counter.available(now=t0 + 5) == 4000.0
        # At t0+61 the t0 event has expired but the t0+5 event is still inside
        # its 60s window.
        assert counter.available(now=t0 + 61) == 8000.0
        assert counter._used_total == 2000
