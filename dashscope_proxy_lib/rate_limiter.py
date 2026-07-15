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

    def __init__(self, capacity: int, window_seconds: int = 60, max_size: int = 100_000):
        self.capacity = capacity
        self.window_seconds = window_seconds
        self.max_size = max_size
        self.window: deque[tuple[float, int]] = deque()
        self.reserved = 0  # tokens reserved for in-flight requests
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Must be called with self._lock held."""
        cutoff = now - self.window_seconds
        while self.window and self.window[0][0] < cutoff:
            self.window.popleft()
        # Enforce max_size to prevent unbounded growth
        while len(self.window) > self.max_size:
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
            self._prune(ts)

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
        self._thread_lock = threading.Lock()  # For cross-thread safety (TUI polling)

        # Circuit breaker: rejects requests immediately when upstream is unhealthy
        self.circuit_failure_count = 0
        self.circuit_open_until = 0.0
        self.circuit_cooldown = 30.0
        self.circuit_threshold = config.get("circuit_threshold", 10)

        # Per-model usage tracking (capped to prevent unbounded growth)
        self.model_usage: dict[str, ModelStats] = {}
        self.model_usage_max = 100  # Evict least-recently-used models beyond this

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

    async def record_model_stats(self, model: str, tokens: int, latency_ms: float, is_429: bool = False) -> None:
        """Record per-model usage statistics. Thread-safe via locks."""
        async with self._lock:
            with self._thread_lock:  # Cross-thread safety for status() reads
                if model not in self.model_usage:
                    # Evict oldest entries if at capacity
                    if len(self.model_usage) >= self.model_usage_max:
                        oldest_keys = sorted(self.model_usage, key=lambda k: self.model_usage[k].requests)[:10]
                        for k in oldest_keys:
                            del self.model_usage[k]
                    self.model_usage[model] = ModelStats()
                stats = self.model_usage[model]
                stats.requests += 1
                stats.tokens += tokens
                stats.total_latency_ms += latency_ms
                stats.recent_latencies.append(latency_ms)
                if is_429:
                    stats.errors_429 += 1
                self.recent_latencies.append(latency_ms)

    async def record_body_sizes(self, request_bytes: int, response_bytes: int) -> None:
        """Record request/response body sizes. Thread-safe via locks."""
        async with self._lock:
            with self._thread_lock:  # Cross-thread safety for status() reads
                self.total_request_bytes += request_bytes
                self.total_response_bytes += response_bytes

    def record_queue_wait(self, wait_ms: float) -> None:
        """Record queue wait time. Thread-safe for cross-thread status() reads."""
        with self._thread_lock:
            self.queue_wait_times.append(wait_ms)

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
        """Check if the circuit breaker is open (upstream unhealthy). Read-only."""
        return self.circuit_open_until > time.monotonic()

    async def record_circuit_success(self) -> None:
        """Reset failure counter on a successful upstream response."""
        async with self._lock:
            self.circuit_failure_count = 0
            self.circuit_open_until = 0.0

    async def record_circuit_failure(self) -> bool:
        """Record an upstream failure. Returns True if circuit should open."""
        async with self._lock:
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

        # Protect cross-thread reads of all shared state
        with self._thread_lock:
            # Snapshot queue_wait_times to avoid mutation during sort
            queue_wait_snapshot = list(self.queue_wait_times)
            model_usage_snapshot = {k: {
                "requests": v.requests,
                "tokens": v.tokens,
                "errors_429": v.errors_429,
                "avg_latency_ms": round(v.total_latency_ms / v.requests, 1) if v.requests > 0 else 0.0,
                "p50_latency_ms": self._compute_percentile(v.recent_latencies, 50) if v.recent_latencies else 0.0,
                "p95_latency_ms": self._compute_percentile(v.recent_latencies, 95) if v.recent_latencies else 0.0,
            } for k, v in self.model_usage.items()}
            recent_latencies_snapshot = list(self.recent_latencies)[-100:]
            # Snapshot circuit breaker state for consistent read
            circuit_open = self.circuit_open_until > time.monotonic()
            circuit_failure_count = self.circuit_failure_count
            # Snapshot counters for consistency
            total_forwarded = self.total_forwarded
            queue_drops = self.queue_drops
            total_429s = self.total_429s
            total_rejected = self.total_rejected
            total_tokens_consumed = self.total_tokens_consumed
            total_request_bytes = self.total_request_bytes
            total_response_bytes = self.total_response_bytes
            pending_requests = self.pending_requests

        queue_p50 = 0.0
        queue_p95 = 0.0
        queue_p99 = 0.0
        if queue_wait_snapshot:
            sorted_waits = sorted(queue_wait_snapshot)
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
            "total_forwarded": total_forwarded,
            "queue_drops": queue_drops,
            "queue_p50_ms": round(queue_p50, 1),
            "queue_p95_ms": round(queue_p95, 1),
            "queue_p99_ms": round(queue_p99, 1),
            "total_429s": total_429s,
            "total_rejected": total_rejected,
            "total_tokens_consumed": total_tokens_consumed,
            "total_request_bytes": total_request_bytes,
            "total_response_bytes": total_response_bytes,
            "pending_requests": pending_requests,
            "circuit_open": circuit_open,
            "circuit_failure_count": circuit_failure_count,
            "model_usage": model_usage_snapshot,
            "recent_latencies": recent_latencies_snapshot,
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


class MultiProviderRateLimiter:
    """
    Container for multiple rate limiters (one per provider).
    
    When secondary provider has different limits than primary,
    maintains independent rate limiters for each provider.
    Otherwise, uses shared limits for backward compatibility.
    
    Duck-types as RateLimiter for queue management (pending_requests,
    is_queue_full, max_queue_size) while delegating rate limiting
    decisions to the provider-specific sub-limiter.
    """

    def __init__(
        self,
        primary_config: dict,
        secondary_config: dict | None = None,
        tertiary_config: dict | None = None,
    ):
        self.primary = RateLimiter(primary_config)
        self.primary_config = primary_config

        self.secondary: RateLimiter | None = None
        self.secondary_config = secondary_config

        self.tertiary: RateLimiter | None = None
        self.tertiary_config = tertiary_config

        if secondary_config:
            self.secondary = RateLimiter(secondary_config)
            limits_differ = any(
                primary_config.get(k) != secondary_config.get(k)
                for k in ["rpm_limit", "tpm_limit", "requests_per_5h",
                         "requests_per_week", "requests_per_month"]
            )
            if limits_differ:
                _log(logging.INFO, "secondary rate limiter created with independent limits")
            else:
                _log(logging.INFO, "secondary rate limiter created with shared limits")

        if tertiary_config:
            self.tertiary = RateLimiter(tertiary_config)
            limits_differ = any(
                primary_config.get(k) != tertiary_config.get(k)
                for k in ["rpm_limit", "tpm_limit", "requests_per_5h",
                         "requests_per_week", "requests_per_month"]
            )
            if limits_differ:
                _log(logging.INFO, "tertiary rate limiter created with independent limits")
            else:
                _log(logging.INFO, "tertiary rate limiter created with shared limits")

        # Global pending request counter (shared across providers for queue management)
        self._pending_requests = 0
        self._pending_lock = asyncio.Lock()  # Protects _pending_requests from concurrent modification

    def get_limiter_for_provider(self, provider_name: str) -> RateLimiter:
        """Get the appropriate rate limiter for a provider."""
        if provider_name == "tertiary" and self.tertiary:
            return self.tertiary
        if provider_name == "secondary" and self.secondary:
            return self.secondary
        return self.primary

    # --- Queue management (global, shared across providers) ---

    @property
    def pending_requests(self) -> int:
        return self._pending_requests

    @pending_requests.setter
    def pending_requests(self, value: int):
        self._pending_requests = max(0, value)

    async def increment_pending(self) -> None:
        """Atomically increment pending_requests counter."""
        async with self._pending_lock:
            self._pending_requests += 1

    async def decrement_pending(self) -> None:
        """Atomically decrement pending_requests counter."""
        async with self._pending_lock:
            self._pending_requests = max(0, self._pending_requests - 1)

    @property
    def max_queue_size(self) -> int:
        return self.primary.max_queue_size

    @max_queue_size.setter
    def max_queue_size(self, value: int):
        self.primary.max_queue_size = value
        if self.secondary:
            self.secondary.max_queue_size = value
        if self.tertiary:
            self.tertiary.max_queue_size = value

    def is_queue_full(self) -> bool:
        return self._pending_requests > self.primary.max_queue_size

    def record_queue_wait(self, wait_ms: float) -> None:
        """Record queue wait time on the primary limiter."""
        self.primary.record_queue_wait(wait_ms)
    # --- Provider-aware rate limiting ---

    async def can_proceed_for_provider(self, estimated_tokens: int = 0, provider_name: str = "primary") -> tuple[bool, str, float]:
        """Check if a request can proceed for a specific provider."""
        limiter = self.get_limiter_for_provider(provider_name)
        return await limiter.can_proceed(estimated_tokens)

    async def reserve_tokens_for_provider(self, estimated_tokens: int, provider_name: str = "primary") -> bool:
        limiter = self.get_limiter_for_provider(provider_name)
        return await limiter.reserve_tokens(estimated_tokens)

    async def reconcile_tokens_for_provider(self, estimated: int, actual: int, provider_name: str = "primary") -> None:
        limiter = self.get_limiter_for_provider(provider_name)
        await limiter.reconcile_tokens(estimated, actual)

    async def refund_tokens_for_provider(self, estimated_tokens: int, provider_name: str = "primary") -> None:
        limiter = self.get_limiter_for_provider(provider_name)
        await limiter.refund_tokens(estimated_tokens)

    # --- Backward-compatible delegates (use primary) ---
    # These allow existing code that calls rate_limiter.can_proceed() etc.
    # to work without changes when no provider is specified.

    async def can_proceed(self, estimated_tokens: int = 0) -> tuple[bool, str, float]:
        return await self.primary.can_proceed(estimated_tokens)

    async def reserve_tokens(self, estimated_tokens: int) -> bool:
        return await self.primary.reserve_tokens(estimated_tokens)

    async def reconcile_tokens(self, estimated: int, actual: int) -> None:
        await self.primary.reconcile_tokens(estimated, actual)

    async def refund_tokens(self, estimated_tokens: int) -> None:
        await self.primary.refund_tokens(estimated_tokens)

    async def record_request(self, tokens_used: int = 0) -> None:
        await self.primary.record_request(tokens_used)

    async def remaining_tpm(self) -> int:
        return await self.primary.remaining_tpm()

    # --- Delegated to primary (used by handler for shared stats) ---

    @property
    def rps_limit(self) -> int:
        return self.primary.rps_limit

    @property
    def max_retries(self) -> int:
        return self.primary.max_retries

    @property
    def queue_drops(self) -> int:
        return self.primary.queue_drops

    @queue_drops.setter
    def queue_drops(self, value: int):
        self.primary.queue_drops = value

    @property
    def total_rejected(self) -> int:
        return self.primary.total_rejected

    @total_rejected.setter
    def total_rejected(self, value: int):
        self.primary.total_rejected = value

    @property
    def queue_wait_times(self):
        return self.primary.queue_wait_times

    # --- Status ---

    def status(self) -> dict:
        """Return combined status from all limiters."""
        primary_status = self.primary.status()
        result = {
            "primary": primary_status,
            "shared_limits": self.secondary is None and self.tertiary is None,
        }

        secondary_status = self.secondary.status() if self.secondary else None
        tertiary_status = self.tertiary.status() if self.tertiary else None

        if self.secondary:
            result["secondary"] = secondary_status
        else:
            result["secondary"] = None

        if self.tertiary:
            result["tertiary"] = tertiary_status
        else:
            result["tertiary"] = None

        # Aggregate stats across all providers (using thread-safe status dicts)
        all_statuses = [primary_status]
        if secondary_status:
            all_statuses.append(secondary_status)
        if tertiary_status:
            all_statuses.append(tertiary_status)

        # Sum counters across all providers
        total_forwarded = sum(s.get("total_forwarded", 0) for s in all_statuses)
        total_429s = sum(s.get("total_429s", 0) for s in all_statuses)
        total_rejected = sum(s.get("total_rejected", 0) for s in all_statuses)
        total_tokens_consumed = sum(s.get("total_tokens_consumed", 0) for s in all_statuses)
        total_request_bytes = sum(s.get("total_request_bytes", 0) for s in all_statuses)
        total_response_bytes = sum(s.get("total_response_bytes", 0) for s in all_statuses)

        # Merge model_usage dicts (already thread-safe snapshots from status())
        merged_model_usage = {}
        for status_dict in all_statuses:
            for model, stats in status_dict.get("model_usage", {}).items():
                if model not in merged_model_usage:
                    merged_model_usage[model] = {
                        "requests": 0,
                        "tokens": 0,
                        "errors_429": 0,
                        "avg_latency_ms": 0.0,
                        "p50_latency_ms": 0.0,
                        "p95_latency_ms": 0.0,
                    }
                merged = merged_model_usage[model]
                merged["requests"] += stats.get("requests", 0)
                merged["tokens"] += stats.get("tokens", 0)
                merged["errors_429"] += stats.get("errors_429", 0)
                # Weighted average of latency
                if merged["requests"] > 0:
                    merged["avg_latency_ms"] = round(
                        (merged["avg_latency_ms"] * (merged["requests"] - stats.get("requests", 0)) +
                         stats.get("avg_latency_ms", 0) * stats.get("requests", 0)) / merged["requests"], 1)

        # Combine recent latencies across all providers (already thread-safe snapshots)
        all_recent_latencies = []
        for status_dict in all_statuses:
            all_recent_latencies.extend(status_dict.get("recent_latencies", []))
        combined_recent_latencies = all_recent_latencies[-100:]

        # Add aggregated stats to result at top level for backward compatibility
        result["total_forwarded"] = total_forwarded
        result["total_429s"] = total_429s
        result["total_rejected"] = total_rejected
        result["total_tokens_consumed"] = total_tokens_consumed
        result["total_request_bytes"] = total_request_bytes
        result["total_response_bytes"] = total_response_bytes
        result["model_usage"] = merged_model_usage
        result["recent_latencies"] = combined_recent_latencies
        result["pending_requests"] = self._pending_requests
        result["queue_drops"] = primary_status.get("queue_drops", 0)
        result["uptime_seconds"] = primary_status.get("uptime_seconds", 0.0)

        return result
