"""Queue wait logic for the DashScope proxy."""

import asyncio
import logging
import random
import time

from aiohttp import web

from dashscope_proxy_lib.rate_limiter import RateLimiter
from dashscope_proxy_lib.http_helpers import _client_disconnected
from dashscope_proxy_lib.logging_config import _log


async def wait_for_slot(
    rate_limiter: RateLimiter,
    request: web.Request,
    estimated_tokens: int = 0,
    deadline_seconds: float = 120.0,
    queue_limiter: RateLimiter | None = None,
) -> tuple[float, str | None, float]:
    """
    Wait until the rate limiter allows a request through.
    Passes estimated_tokens to can_proceed() for TPM check.
    Checks for client disconnect between sleep iterations.

    queue_limiter: optional global queue counter (e.g. MultiProviderRateLimiter).
    When omitted, rate_limiter is used for queue-full checks.

    Returns a 3-tuple ``(total_wait, reason, last_wait)``:
    - ``(total_wait, None, last_wait)`` — admitted within the deadline;
      ``last_wait`` is 0.0 when no denial was observed.
    - ``(total_wait, "queue_full", last_wait)`` — the queue exceeded its
      size limit.
    - ``(total_wait, "deadline_exceeded", last_wait)`` — the deadline elapsed
      while waiting for an RPM/TPM/RPS slot.
    - ``(total_wait, "client_disconnected", last_wait)`` — the client went
      away while queued.

    ``last_wait`` is the most recent ``can_proceed`` wait (seconds) observed
    before the outcome, so callers can expose it as a Retry-After hint.
    """
    queue = queue_limiter or rate_limiter
    if queue.is_queue_full():
        return 0.0, "queue_full", 0.0

    _log(logging.DEBUG, "wait_for_slot: entering queue",
         pending=queue.pending_requests, max_queue=queue.max_queue_size)

    deadline = time.monotonic() + deadline_seconds
    total_wait = 0.0
    last_wait = 0.0
    while time.monotonic() < deadline:
        allowed, reason, wait = await rate_limiter.can_proceed(estimated_tokens)
        if allowed:
            return total_wait, None, last_wait
        last_wait = wait

        if queue.is_queue_full():
            return total_wait, "queue_full", last_wait

        # Bail if the client gave up while we were sleeping
        if _client_disconnected(request):
            return total_wait, "client_disconnected", last_wait

        # can_proceed reasons are "RPM limit reached" / "TPM limit reached" /
        # "RPS spacing". RPM/TPM denials are coarse window waits — use the
        # longer jitter floor so we do not hammer the window check.
        if "RPM" in reason or "TPM" in reason:
            jitter = random.uniform(0.5, 2.0)
            wait_time = max(wait, 5.0) + jitter
        else:
            jitter = random.uniform(0.05, 0.2)
            wait_time = wait + jitter

        # Don't sleep past deadline — leave a safety buffer
        remaining = deadline - time.monotonic() - wait_time
        if remaining < 0:
            _log(logging.INFO, "queue wait exceeded deadline, aborting",
                 total_wait=round(total_wait, 1), deadline_seconds=deadline_seconds)
            return total_wait, "deadline_exceeded", last_wait

        total_wait += wait_time
        _log(logging.INFO, "rate limited, waiting before retry",
             reason=reason, wait_seconds=round(wait_time, 1))
        await asyncio.sleep(wait_time)

        if _client_disconnected(request):
            return total_wait, "client_disconnected", last_wait

    return total_wait, "deadline_exceeded", last_wait
