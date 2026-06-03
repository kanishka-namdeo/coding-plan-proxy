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


class TokenWindowCounter:
    """
    Sliding window counter for TPM enforcement.

    Tracks token consumption events with timestamps and amounts within a
    configurable time window (default 60 seconds). Unlike TokenBucket's
    continuous refill model, this accurately reflects tokens consumed in
    the actual TPM window — tokens remain 'used' until they age out of
    the window.

    Pattern: reserve before send, reconcile after response, refund on error.
    """

    def __init__(self, capacity: int, window_seconds: int = 60):
        self.capacity = capacity
        self.window_seconds = window_seconds
        self.window: deque[tuple[float, int]] = deque()
        self.reserved = 0  # tokens reserved for in-flight requests
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Must be called with self._lock held."""
        cutoff = now - self.window_seconds
        while self.window and self.window[0][0] < cutoff:
            self.window.popleft()

    def _tokens_used(self, now: float) -> int:
        """Must be called with self._lock held."""
        self._prune(now)
        return sum(amount for _, amount in self.window)

    def available(self, now: float | None = None) -> float:
        """Return currently available tokens (capacity minus tokens used in window, minus reservations)."""
        now = now or time.monotonic()
        with self._lock:
            used = self._tokens_used(now)
            return max(0.0, self.capacity - used - self.reserved)

    def try_reserve(self, tokens: int, now: float | None = None) -> bool:
        """Reserve tokens if available. Returns True on success."""
        now = now or time.monotonic()
        with self._lock:
            used = self._tokens_used(now)
            if self.capacity - used - self.reserved >= tokens:
                self.reserved += tokens
                return True
            return False

    def reconcile(self, estimated: int, actual: int, now: float | None = None) -> None:
        """
        Release the reservation and record actual token usage in the window.
        The reservation covers `estimated`; actual consumption is recorded
        so the window accurately reflects real usage.
        """
        with self._lock:
            self.reserved = max(0, self.reserved - estimated)
            ts = now if now is not None else time.monotonic()
            self.window.append((ts, actual))

    def refund(self, tokens: int) -> None:
        """Release reserved tokens back (on upstream error)."""
        with self._lock:
            self.reserved = max(0, self.reserved - tokens)

    def wait_seconds_for(self, tokens: int, now: float | None = None) -> float:
        """Return how long until enough tokens become available.

        Computes when the oldest consumed tokens will expire from the window,
        freeing up enough capacity for the requested amount.
        """
        now = now or time.monotonic()
        with self._lock:
            used = self._tokens_used(now)
            needed = tokens + self.reserved - (self.capacity - used)
            if needed <= 0:
                return 0.0
            # Walk through events in order, find when enough will expire
            cumulative = 0
            for ts, amount in self.window:
                cumulative += amount
                if cumulative >= needed:
                    wait = ts + self.window_seconds - now
                    return max(0.0, wait)
            # If even expiring all events isn't enough (shouldn't happen normally),
            # return the time until the window fully clears
            if self.window:
                return max(0.0, self.window[0][0] + self.window_seconds - now)
            return 0.0

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            used = self._tokens_used(now)
            return {
                "tpm_capacity": self.capacity,
                "tpm_available": int(self.capacity - used - self.reserved),
                "tpm_reserved": self.reserved,
            }


@dataclass
class ModelStats:
    """Per-model usage statistics."""
    requests: int = 0
    tokens: int = 0
    errors_429: int = 0
    total_latency_ms: float = 0.0
    recent_latencies: deque = field(default_factory=lambda: deque(maxlen=100))


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
        self.tpm_bucket = TokenWindowCounter(self.tpm_limit)
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
        self.queue_drops = 0
        self.total_429s = 0
        self.total_rejected = 0
        self.total_tokens_consumed = 0
        self.total_request_bytes = 0
        self.total_response_bytes = 0

        self.queue_wait_times: deque = deque(maxlen=1000)

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
                    wait = self.tpm_bucket.wait_seconds_for(estimated_tokens, now_mono)
                    wait = max(1.0, wait)
                    _log(logging.DEBUG, "can_proceed denied: TPM limit reached", wait_seconds=wait,
                         estimated_tokens=estimated_tokens, available_tokens=int(avail), shortfall=estimated_tokens - avail)
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
        stats.recent_latencies.append(latency_ms)
        if is_429:
            stats.errors_429 += 1
        self.recent_latencies.append(latency_ms)

    def record_body_sizes(self, request_bytes: int, response_bytes: int) -> None:
        """Record request/response body sizes for aggregate metrics."""
        self.total_request_bytes += request_bytes
        self.total_response_bytes += response_bytes

    async def reconcile_tokens(self, estimated: int, actual: int, now: float | None = None) -> None:
        """
        Release the reservation and adjust the bucket after receiving the real
        token count. The reservation is always for `estimated`; actual usage
        is reconciled by draining or refunding the difference.
        """
        async with self._lock:
            if estimated <= 0:
                return
            self.tpm_bucket.reconcile(estimated, actual, now=now)

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

        queue_p50 = 0.0
        queue_p95 = 0.0
        queue_p99 = 0.0
        if self.queue_wait_times:
            sorted_waits = sorted(self.queue_wait_times)
            n = len(sorted_waits)
            queue_p50 = sorted_waits[int(n * 0.50)]
            queue_p95 = sorted_waits[int(n * 0.95)]
            queue_p99 = sorted_waits[int(n * 0.99)]

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
            "queue_drops": self.queue_drops,
            "queue_p50_ms": round(queue_p50, 1),
            "queue_p95_ms": round(queue_p95, 1),
            "queue_p99_ms": round(queue_p99, 1),
            "total_429s": self.total_429s,
            "total_rejected": self.total_rejected,
            "total_tokens_consumed": self.total_tokens_consumed,
            "total_request_bytes": self.total_request_bytes,
            "total_response_bytes": self.total_response_bytes,
            "pending_requests": self.pending_requests,
            "circuit_open": self.circuit_is_open(),
            "circuit_failure_count": self.circuit_failure_count,
            "model_usage": {k: {
                "requests": v.requests,
                "tokens": v.tokens,
                "errors_429": v.errors_429,
                "avg_latency_ms": round(v.total_latency_ms / v.requests, 1) if v.requests > 0 else 0.0,
                "p50_latency_ms": self._compute_percentile(v.recent_latencies, 50) if v.recent_latencies else 0.0,
                "p95_latency_ms": self._compute_percentile(v.recent_latencies, 95) if v.recent_latencies else 0.0,
            } for k, v in self.model_usage.items()},
            "recent_latencies": list(self.recent_latencies)[-100:],
            "uptime_seconds": round(time.time() - self.start_time, 1),
        }

    @staticmethod
    def _compute_percentile(latencies: deque, p: float) -> float:
        """Compute percentile from a deque of latencies."""
        if not latencies:
            return 0.0
        sorted_vals = sorted(latencies)
        k = (len(sorted_vals) - 1) * (p / 100.0)
        f = int(k)
        c = f + 1
        if c >= len(sorted_vals):
            return round(sorted_vals[f], 1)
        return round(sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f), 1)
