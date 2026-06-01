"""Queue wait logic for the DashScope proxy."""

import asyncio
import logging
import random
import time

from aiohttp import web

from dashscope_proxy_lib.rate_limiter import RateLimiter
from dashscope_proxy_lib.http_helpers import _client_disconnected
from dashscope_proxy_lib.logging_config import _log


async def wait_for_slot(rate_limiter: RateLimiter, request: web.Request, estimated_tokens: int = 0, deadline_seconds: float = 120.0) -> float | None:
    """
    Wait until the rate limiter allows a request through.
    Passes estimated_tokens to can_proceed() for TPM check.
    Returns total wait time, or None if queue is full, client disconnected, or deadline exceeded.
    Checks for client disconnect between sleep iterations.
    """
    if rate_limiter.is_queue_full():
        return None

    _log(logging.DEBUG, "wait_for_slot: entering queue",
         pending=rate_limiter.pending_requests, max_queue=rate_limiter.max_queue_size)

    deadline = time.monotonic() + deadline_seconds
    total_wait = 0.0
    while time.monotonic() < deadline:
        allowed, reason, wait = await rate_limiter.can_proceed(estimated_tokens)
        if allowed:
            return total_wait

        if rate_limiter.is_queue_full():
            return None

        # Bail if the client gave up while we were sleeping
        if _client_disconnected(request):
            return None

        if "quota" in reason.lower():
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
            return None

        total_wait += wait_time
        _log(logging.INFO, "rate limited, waiting before retry",
             reason=reason, wait_seconds=round(wait_time, 1))
        await asyncio.sleep(wait_time)

    return None  # deadline exceeded
