"""Rate limiting primitives for the DashScope proxy."""

import time
import threading
import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field

from dashscope_proxy_lib.config import DEQUE_MAX_SIZE
from dashscope_proxy_lib.logging_config import _log


class SlidingWindowCounter:
    """Tracks count of events within a sliding time window with bounded memory.
    
    Thread-safe via threading.Lock for cross-thread access (TUI polling thread
    reads while async event loop writes).
    """

    def __init__(self, window_seconds: int, max_size: int = DEQUE_MAX_SIZE):
        self.window = window_seconds
        self.max_size = max_size
        self.events: deque[float] = deque()
        self._lock = threading.Lock()

    def add(self, now: float | None = None):
        now = now or time.monotonic()
        with self._lock:
            self.events.append(now)
            self._prune(now)

    def count(self, now: float | None = None) -> int:
        now = now or time.monotonic()
        with self._lock:
            self._prune(now)
            return len(self.events)

    def _prune(self, now: float):
        """Must be called with self._lock held."""
        cutoff = now - self.window
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        if len(self.events) > self.max_size:
            excess = len(self.events) - self.max_size
            for _ in range(excess):
                self.events.popleft()


class TokenBucket:
    """
    Token bucket algorithm for TPM enforcement.

    Tokens refill at a constant rate. Each request drains tokens proportional
    to its estimated size. Burst capacity equals the full bucket (tpm_limit).
    O(1) state — tracks only current tokens and last refill time.

    Pattern: reserve before send, reconcile after response, refund on error.
    """

    def __init__(self, capacity: int, window_seconds: int = 60):
        self.capacity = capacity
        self.window_seconds = window_seconds
        self.refill_rate = capacity / window_seconds  # tokens per second
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self.reserved = 0  # tokens reserved for in-flight requests

    def _refill(self, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now

    def available(self, now: float | None = None) -> float:
        """Return currently available tokens (after refill, minus reservations)."""
        now = now or time.monotonic()
        self._refill(now)
        return max(0.0, self.tokens - self.reserved)

    def try_reserve(self, tokens: int, now: float | None = None) -> bool:
        """Reserve tokens if available. Returns True on success."""
        now = now or time.monotonic()
        self._refill(now)
        if self.tokens - self.reserved >= tokens:
            self.reserved += tokens
            return True
        return False

    def reconcile(self, estimated: int, actual: int) -> None:
        """Adjust bucket after response. If actual > estimated, drain extra. If less, refund."""
        diff = actual - estimated
        self.reserved = max(0, self.reserved - estimated)
        if diff > 0:
            self.tokens = max(0, self.tokens - diff)

    def refund(self, tokens: int) -> None:
        """Release reserved tokens back (on upstream error)."""
        self.reserved = max(0, self.reserved - tokens)

    def status(self) -> dict:
        now = time.monotonic()
        self._refill(now)
        return {
            "tpm_capacity": self.capacity,
            "tpm_available": int(self.tokens - self.reserved),
            "tpm_reserved": self.reserved,
        }


@dataclass
class ModelStats:
    """Per-model usage statistics."""
    requests: int = 0
    tokens: int = 0
    errors_429: int = 0
    total_latency_ms: float = 0.0


class RateLimiter:
    """
    Multi-layer rate limiter for DashScope Coding Plan.
    Thread-safe via asyncio.Lock for concurrent request handling.
    """

    def __init__(self, config: dict):
        sf = config["safety_factor"]
        self.rpm_limit = int(config["rpm_limit"] * sf)
        self.tpm_limit = int(config["tpm_limit"] * sf)
        self.rps_limit = max(1, int(self.rpm_limit / 60))

        self.rpm_window = SlidingWindowCounter(60)
        self.tpm_bucket = TokenBucket(self.tpm_limit)
        self.hour5_window = SlidingWindowCounter(5 * 3600)

        self.week_count = 0
        self.month_count = 0
        self.week_start = time.time()
        self.month_start = time.time()

        self.week_limit = config["requests_per_week"]
        self.month_limit = config["requests_per_month"]
        self.hour5_limit = config["requests_per_5h"]

        self.max_queue_size = config["max_queue_size"]
        self.max_retries = config["max_retries"]
        self.base_backoff = config["base_backoff"]
        self.pending_requests = 0

        self.total_forwarded = 0
        self.total_queued = 0
        self.total_429s = 0
        self.total_rejected = 0
        self.total_tokens_consumed = 0

        self.last_request_time: float = 0.0
        self._lock = asyncio.Lock()

        # Circuit breaker: rejects requests immediately when upstream is unhealthy
        self.circuit_failure_count = 0
        self.circuit_open_until = 0.0
        self.circuit_cooldown = 30.0
        self.circuit_threshold = config.get("circuit_threshold", 10)

        # Per-model usage tracking
        self.model_usage: dict[str, ModelStats] = {}

        # Recent request latencies for percentile computation
        self.recent_latencies: deque = deque(maxlen=1000)

        # Start time for uptime tracking
        self.start_time = time.time()

    async def can_proceed(self, estimated_tokens: int = 0) -> tuple[bool, str, float]:
        """
        Check if a request can proceed. Pure read-only check — no side effects.
        When estimated_tokens > 0, both RPM and TPM must have headroom.
        Returns (allowed, reason, wait_seconds).
        """
        async with self._lock:
            now_mono = time.monotonic()
            now_wall = time.time()

            if now_wall - self.week_start >= 7 * 24 * 3600:
                self.week_count = 0
                self.week_start = now_wall
            if now_wall - self.month_start >= 30 * 24 * 3600:
                self.month_count = 0
                self.month_start = now_wall

            h5_count = self.hour5_window.count(now_mono)
            if h5_count >= self.hour5_limit:
                oldest = self.hour5_window.events[0] if self.hour5_window.events else now_mono
                wait = max(0, oldest + 5 * 3600 - now_mono)
                _log(logging.DEBUG, "can_proceed denied: 5-hour quota exhausted", wait_seconds=wait)
                return False, "5-hour quota exhausted", wait

            if self.week_count >= self.week_limit:
                _log(logging.DEBUG, "can_proceed denied: weekly quota exhausted", wait_seconds=60)
                return False, "Weekly quota exhausted", 60

            if self.month_count >= self.month_limit:
                _log(logging.DEBUG, "can_proceed denied: monthly quota exhausted", wait_seconds=60)
                return False, "Monthly quota exhausted", 60

            rpm_count = self.rpm_window.count(now_mono)
            if rpm_count >= self.rpm_limit:
                wait = 60.0 / max(1, self.rpm_limit)
                _log(logging.DEBUG, "can_proceed denied: RPM limit reached", wait_seconds=wait)
                return False, "RPM limit reached", wait

            if estimated_tokens > 0:
                avail = self.tpm_bucket.available(now_mono)
                if avail < estimated_tokens:
                    shortfall = estimated_tokens - avail
                    wait = max(1.0, shortfall / self.tpm_bucket.refill_rate)
                    _log(logging.DEBUG, "can_proceed denied: TPM limit reached", wait_seconds=wait,
                         estimated_tokens=estimated_tokens, available_tokens=int(avail), shortfall=shortfall)
                    return False, "TPM limit reached", wait

            min_gap = 1.0 / self.rps_limit
            time_since_last = now_mono - self.last_request_time
            if time_since_last < min_gap:
                _log(logging.DEBUG, "can_proceed denied: RPS spacing",
                     wait_seconds=min_gap - time_since_last)
                return False, "RPS spacing", min_gap - time_since_last

            return True, "ok", 0.0

    async def reserve_tokens(self, estimated_tokens: int) -> bool:
        """
        Reserve TPM before sending to upstream. Call this AFTER can_proceed()
        returns True but BEFORE forwarding the request. Returns False if the
        bucket no longer has sufficient tokens (race condition guard).
        """
        async with self._lock:
            if estimated_tokens <= 0:
                return True
            return self.tpm_bucket.try_reserve(estimated_tokens)

    async def record_request(self, tokens_used: int = 0, now: float | None = None):
        """Record a successfully completed request. Does NOT touch TPM bucket —
        that is handled by reconcile_tokens() which releases the reservation."""
        async with self._lock:
            now = now or time.monotonic()
            self.rpm_window.add(now)
            self.hour5_window.add(now)
            self.week_count += 1
            self.month_count += 1
            self.total_forwarded += 1
            self.total_tokens_consumed += tokens_used
            self.last_request_time = now

    def record_model_stats(self, model: str, tokens: int, latency_ms: float, is_429: bool = False) -> None:
        """Record per-model usage statistics. Thread-safe for async callers."""
        if model not in self.model_usage:
            self.model_usage[model] = ModelStats()
        stats = self.model_usage[model]
        stats.requests += 1
        stats.tokens += tokens
        stats.total_latency_ms += latency_ms
        if is_429:
            stats.errors_429 += 1
        self.recent_latencies.append(latency_ms)

    async def reconcile_tokens(self, estimated: int, actual: int) -> None:
        """
        Release the reservation and adjust the bucket after receiving the real
        token count. The reservation is always for `estimated`; actual usage
        is reconciled by draining or refunding the difference.
        """
        async with self._lock:
            if estimated <= 0:
                return
            self.tpm_bucket.reconcile(estimated, actual)

    async def refund_tokens(self, estimated_tokens: int) -> None:
        """Refund reserved tokens when an upstream request fails permanently."""
        async with self._lock:
            if estimated_tokens > 0:
                self.tpm_bucket.refund(estimated_tokens)

    async def remaining_tpm(self) -> int:
        """Return available TPM in the current minute window."""
        async with self._lock:
            return int(self.tpm_bucket.available())

    def is_queue_full(self) -> bool:
        """Check if the queue is full. Thread-safe read without lock."""
        return self.pending_requests > self.max_queue_size

    def circuit_is_open(self) -> bool:
        """Check if the circuit breaker is open (upstream unhealthy)."""
        if self.circuit_open_until > time.monotonic():
            return True
        self.circuit_open_until = 0.0
        return False

    def record_circuit_success(self) -> None:
        """Reset failure counter on a successful upstream response."""
        self.circuit_failure_count = 0
        self.circuit_open_until = 0.0

    def record_circuit_failure(self) -> None:
        """Record an upstream failure. Returns True if circuit should open."""
        self.circuit_failure_count += 1
        if self.circuit_failure_count >= self.circuit_threshold:
            self.circuit_open_until = time.monotonic() + self.circuit_cooldown
            _log(logging.WARNING, "circuit breaker opened after consecutive failures",
                 failure_count=self.circuit_failure_count, threshold=self.circuit_threshold,
                 cooldown_seconds=self.circuit_cooldown)
            return True
        return False

    def status(self) -> dict:
        now = time.monotonic()
        tpm_status = self.tpm_bucket.status()
        return {
            "rps_limit": self.rps_limit,
            "rpm_limit": self.rpm_limit,
            "rpm_current": self.rpm_window.count(now),
            "tpm_limit": tpm_status["tpm_capacity"],
            "tpm_available": tpm_status["tpm_available"],
            "tpm_reserved": tpm_status["tpm_reserved"],
            "requests_5h": self.hour5_window.count(now),
            "requests_5h_limit": self.hour5_limit,
            "requests_week": self.week_count,
            "requests_week_limit": self.week_limit,
            "requests_month": self.month_count,
            "requests_month_limit": self.month_limit,
            "total_forwarded": self.total_forwarded,
            "total_queued": self.total_queued,
            "total_429s": self.total_429s,
            "total_rejected": self.total_rejected,
            "total_tokens_consumed": self.total_tokens_consumed,
            "pending_requests": self.pending_requests,
            "circuit_open": self.circuit_is_open(),
            "circuit_failure_count": self.circuit_failure_count,
            "model_usage": {k: {"requests": v.requests, "tokens": v.tokens, "errors_429": v.errors_429, "avg_latency_ms": round(v.total_latency_ms / v.requests, 1) if v.requests > 0 else 0.0} for k, v in self.model_usage.items()},
            "recent_latencies": list(self.recent_latencies)[-100:],
            "uptime_seconds": round(time.time() - self.start_time, 1),
        }
