"""Request handlers for the DashScope proxy."""

import asyncio
import json
import logging
import random
import re
import sys
import time
import typing
import uuid
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

# Resolve mutable constants through the facade at runtime so that tests
# patching ``dashscope_proxy.TARGET_BASE`` / ``DASHSCOPE_API_KEY`` etc. work.
# We import immutable things (classes, functions) directly.
from dashscope_proxy_lib.rate_limiter import RateLimiter
from dashscope_proxy_lib.session_log import SessionLogWriter
from dashscope_proxy_lib.logging_config import _log
from dashscope_proxy_lib.token_utils import (
    estimate_tokens_for_request, extract_tokens_from_response, extract_tokens_from_stream,
)
from dashscope_proxy_lib.request_transform import (
    _is_chat_endpoint, map_developer_to_system, normalize_model_name,
)
from dashscope_proxy_lib.http_helpers import (
    _add_forwarded_headers, _add_ratelimit_headers, _client_disconnected, _compute_backoff,
    _make_error_response, _sleep_interruptible, _strip_hop_by_hop, parse_retry_after,
    should_retry_429, _upstream_error_response, _sse_response_headers, _finalize_stream_response,
)
from dashscope_proxy_lib.queue import wait_for_slot
from dashscope_proxy_lib.provider_router import ProviderRouter, ProviderConfig


_provider_router: ProviderRouter | None = None


def get_provider_router() -> ProviderRouter:
    """Lazy initialization of provider router."""
    global _provider_router
    if _provider_router is None:
        _provider_router = ProviderRouter()
    return _provider_router


def _cfg(name: str):
    """Resolve a constant through the facade module (supports runtime patching by tests)."""
    _ds = sys.modules.get("dashscope_proxy")
    if _ds is not None:
        return getattr(_ds, name)
    from dashscope_proxy_lib import config as _c
    return getattr(_c, name)


async def handle_request(request: web.Request) -> web.StreamResponse:
    path = request.path
    method = request.method
    rate_limiter = request.app["rate_limiter"]
    request_start = time.monotonic()

    request_id = request.headers.get("X-Request-ID", f"{uuid.uuid4().hex[:12]}")
    log_prefix = f"[{request_id}]"

    model_name = None
    is_stream = False
    estimated_tokens = 0
    actual_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0
    queue_wait_ms = 0.0
    retry = 0
    retry_5xx = 0
    tokens_reserved = False
    error_reason = None
    status_code = 200
    request_body_bytes = 0
    response_body_bytes = 0
    remote_addr = None
    upstream_latency_ms = 0.0

    session_entry = {
        "request_id": request_id,
        "method": method,
        "path": path,
    }

    app_shutting_down = request.app.get("shutting_down")
    if app_shutting_down and app_shutting_down.is_set():
        error_reason = "shutting_down"
        status_code = 503
        _log(logging.WARNING, "request rejected: proxy shutting down",
             request_id=request_id, method=method, path=path)
        return _make_error_response(
            503, b'{"error":"shutting down"}', request_id, retry_after=30
        )

    # Health / Ready endpoints — no rate limiting, no stats
    if path == "/health" and method == "GET":
        return web.json_response({"status": "ok"})
    if path == "/ready" and method == "GET":
        session = request.app.get("client_session")
        ready = session is not None and not session.closed
        status_code = 200 if ready else 503
        return web.json_response({"status": "ready" if ready else "not_ready"}, status=status_code)

    if path == "/v1/proxy/status" and method == "GET":
        router = get_provider_router()
        status = {
            "rate_limits": rate_limiter.status(),
            "providers": router.get_provider_status(),
            "model_overlaps": router.get_model_overlaps(),
        }
        return web.json_response(status)

    if method == "GET" and path in ("/v1/models", "/models"):
        router = get_provider_router()
        models = router.get_all_models()
        _log(logging.INFO, "model list returned",
             request_id=request_id, method=method, path=path,
             model_count=len(models.get("data", [])))
        resp = web.json_response(models)
        resp.headers["X-Request-ID"] = request_id
        return resp

    body_bytes = await request.read()
    request_body_bytes = len(body_bytes)
    remote_addr = request.remote

    if method == "POST" and not body_bytes:
        error_reason = "empty_body"
        status_code = 400
        _log(logging.WARNING, "request rejected: empty body",
             request_id=request_id, method=method, path=path, reason="empty_body")
        session_entry["status_code"] = status_code
        session_entry["error_reason"] = error_reason
        await _maybe_flush_session_log(request.app, session_entry)
        return _make_error_response(400, b'{"error":"empty request body"}', request_id)

    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        error_reason = "method_not_allowed"
        status_code = 405
        _log(logging.WARNING, "request rejected: method not allowed",
             request_id=request_id, method=method, path=path, reason="method_not_allowed")
        session_entry["status_code"] = status_code
        session_entry["error_reason"] = error_reason
        await _maybe_flush_session_log(request.app, session_entry)
        return _make_error_response(405, b'{"error":"method not allowed"}', request_id)

    if _is_chat_endpoint(path) and method != "POST":
        error_reason = "method_not_allowed_chat"
        status_code = 405
        _log(logging.WARNING, "request rejected: method not allowed for chat endpoint",
             request_id=request_id, method=method, path=path, reason="method_not_allowed")
        session_entry["status_code"] = status_code
        session_entry["error_reason"] = error_reason
        await _maybe_flush_session_log(request.app, session_entry)
        return _make_error_response(405, b'{"error":"method not allowed"}', request_id)

    if len(body_bytes) > _cfg("MAX_BODY_SIZE"):
        error_reason = "body_too_large"
        status_code = 413
        _log(logging.WARNING, "request rejected: payload too large",
             request_id=request_id, method=method, path=path,
             reason="body_too_large", body_size=len(body_bytes))
        session_entry["status_code"] = status_code
        session_entry["error_reason"] = error_reason
        await _maybe_flush_session_log(request.app, session_entry)
        return _make_error_response(413, b'{"error":"payload too large"}', request_id)

    body = None
    if body_bytes and method == "POST":
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError:
            error_reason = "invalid_json"
            status_code = 400
            _log(logging.WARNING, "request rejected: invalid JSON",
                 request_id=request_id, method=method, path=path, reason="invalid_json")
            session_entry["status_code"] = status_code
            session_entry["error_reason"] = error_reason
            await _maybe_flush_session_log(request.app, session_entry)
            return _make_error_response(400, b'{"error":"invalid JSON"}', request_id)

        if not isinstance(body, dict):
            error_reason = "invalid_body_type"
            status_code = 400
            _log(logging.WARNING, "request rejected: body must be a JSON object",
                 request_id=request_id, method=method, path=path, reason="invalid_body_type")
            session_entry["status_code"] = status_code
            session_entry["error_reason"] = error_reason
            await _maybe_flush_session_log(request.app, session_entry)
            return _make_error_response(400, b'{"error":"request body must be a JSON object"}', request_id)

        if "model" not in body:
            error_reason = "missing_model"
            status_code = 400
            _log(logging.WARNING, "request rejected: missing model field",
                 request_id=request_id, method=method, path=path, reason="missing_model")
            session_entry["status_code"] = status_code
            session_entry["error_reason"] = error_reason
            await _maybe_flush_session_log(request.app, session_entry)
            return _make_error_response(400, b'{"error":"missing required field: model"}', request_id)

        messages = body.get("messages")
        if messages is None or (isinstance(messages, list) and len(messages) == 0):
            error_reason = "missing_messages"
            status_code = 400
            _log(logging.WARNING, "request rejected: missing messages field",
                 request_id=request_id, method=method, path=path, reason="missing_messages")
            session_entry["status_code"] = status_code
            session_entry["error_reason"] = error_reason
            await _maybe_flush_session_log(request.app, session_entry)
            return _make_error_response(400, b'{"error":"missing required field: messages"}', request_id)

        body = map_developer_to_system(body)
        if isinstance(body.get("model"), str):
            body["model"] = normalize_model_name(body["model"])
        try:
            body_bytes = json.dumps(body).encode()
        except (TypeError, ValueError) as e:
            error_reason = "unserializable"
            status_code = 400
            _log(logging.WARNING, "request rejected: unserializable body",
                 request_id=request_id, method=method, path=path, reason="unserializable", error=str(e))
            session_entry["status_code"] = status_code
            session_entry["error_reason"] = error_reason
            await _maybe_flush_session_log(request.app, session_entry)
            return _make_error_response(400, json.dumps({"error": f"unserializable body: {e}"}).encode(), request_id)

    estimated_tokens = estimate_tokens_for_request(body_bytes) if body_bytes else 0
    model_name = body.get("model") if isinstance(body, dict) else None
    is_stream = isinstance(body, dict) and body.get("stream") is True

    # Determine which provider should handle this request
    router = get_provider_router()
    from dashscope_proxy_lib.request_transform import split_provider_prefix
    pinned_name, bare_name = split_provider_prefix(model_name or "")
    if "/" in (model_name or "") and pinned_name is None:
        error_reason = "unknown_provider_prefix"
        status_code = 400
        _log(logging.WARNING, "request rejected: unknown provider prefix",
             request_id=request_id, method=method, path=path, reason="unknown_provider_prefix",
             model=model_name)
        session_entry["model"] = model_name
        session_entry["status_code"] = status_code
        session_entry["error_reason"] = error_reason
        await _maybe_flush_session_log(request.app, session_entry)
        # Provide list of valid provider slugs as hint
        provider_slugs = list(_cfg("PROVIDER_SLUGS").keys())
        return _make_error_response(
            400,
            json.dumps({
                "error": "unknown provider prefix",
                "model": model_name,
                "available_providers": provider_slugs,
            }).encode(),
            request_id,
        )
    if pinned_name is not None:
        provider_check = getattr(router, pinned_name)
        if not provider_check.is_available:
            error_reason = "provider_not_configured"
            status_code = 400
            _log(logging.WARNING, "request rejected: provider not configured",
                 request_id=request_id, method=method, path=path, reason="provider_not_configured",
                 model=model_name, provider=pinned_name)
            session_entry["model"] = model_name
            session_entry["status_code"] = status_code
            session_entry["error_reason"] = error_reason
            await _maybe_flush_session_log(request.app, session_entry)
            # Provide list of valid provider slugs as hint
            provider_slugs = list(_cfg("PROVIDER_SLUGS").keys())
            return _make_error_response(
                400,
                json.dumps({
                    "error": f"provider '{pinned_name}' not configured",
                    "model": model_name,
                    "available_providers": provider_slugs,
                }).encode(),
                request_id,
            )
        model_name = bare_name
        if isinstance(body, dict):
            body["model"] = bare_name
        body_bytes = json.dumps(body).encode()

    # Build candidate provider list for failover
    if pinned_name is not None:
        # Pinned request: single provider, no failover
        candidates = [getattr(router, pinned_name)]
    else:
        # Check for overlapping models
        overlap = router.get_providers_for_model(model_name or "")
        if overlap:
            candidates = overlap
        else:
            # Single provider, no failover
            candidates = [router.get_provider_for_model(model_name or "")]

    provider = candidates[0]
    provider_name = provider.name
    attempted_providers: list[str] = [provider_name]
    candidate_idx = 0

    session_entry["model"] = model_name
    session_entry["is_stream"] = is_stream
    session_entry["estimated_tokens"] = estimated_tokens
    session_entry["provider"] = provider_name
    session_entry["attempted_providers"] = attempted_providers

    # Get the appropriate rate limiter for this provider.
    # Duck-type instead of isinstance — the facade may reload rate_limiter
    # after the app instance is created (TUI imports dashscope_proxy).
    get_provider_limiter = getattr(rate_limiter, "get_limiter_for_provider", None)
    if get_provider_limiter is not None:
        limiter = get_provider_limiter(provider_name)
    else:
        limiter = rate_limiter

    await rate_limiter.increment_pending()
    try:
        wait_time = await wait_for_slot(limiter, request, estimated_tokens, queue_limiter=rate_limiter)
        if wait_time is None:
            if _client_disconnected(request):
                error_reason = "client_disconnected"
                status_code = 499
                _log(logging.INFO, "client disconnected while queued",
                     request_id=request_id, method=method, path=path)
                return _make_error_response(499, b'{"error":"client disconnected"}', request_id)
            rate_limiter.queue_drops += 1
            rate_limiter.total_rejected += 1
            error_reason = "queue_full"
            status_code = 503
            _log(logging.WARNING, "request rejected: queue full",
                 request_id=request_id, method=method, path=path,
                 model=model_name, provider=provider_name,
                 pending=rate_limiter.pending_requests,
                 max_queue=rate_limiter.max_queue_size)
            retry_sec = max(1, rate_limiter.pending_requests // max(1, rate_limiter.rps_limit))
            return _make_error_response(
                503,
                json.dumps({"error": "queue full", "retry_after": retry_sec}).encode(),
                request_id,
                retry_after=retry_sec,
            )

        # Reserve TPM before forwarding (post-queue guard — bucket may have drained)
        if estimated_tokens > 0 and not await limiter.reserve_tokens(estimated_tokens):
            rate_limiter.queue_drops += 1
            rate_limiter.total_rejected += 1
            error_reason = "tpm_reservation_failed"
            status_code = 503
            _log(logging.WARNING, "request rejected: TPM reservation failed after queue",
                 request_id=request_id, method=method, path=path,
                 model=model_name, provider=provider_name,
                 estimated_tokens=estimated_tokens)
            retry_sec = max(1, rate_limiter.pending_requests // max(1, rate_limiter.rps_limit))
            return _make_error_response(
                503,
                json.dumps({"error": "TPM quota exceeded while queued", "retry_after": retry_sec}).encode(),
                request_id,
                retry_after=retry_sec,
            )
        tokens_reserved = estimated_tokens > 0

        if wait_time > 0:
            queue_wait_ms = round(wait_time * 1000, 1)
            rate_limiter.record_queue_wait(queue_wait_ms)
            _log(logging.INFO, "request queued",
                 request_id=request_id, wait_ms=queue_wait_ms)

        # Build target URL using provider's base URL
        # Handle versioned base URLs (e.g. /v1, /v3) by preserving the base's version
        # and normalizing the request path to match.
        base_url = provider.base_url.rstrip("/")
        # Detect version suffix like /v1, /v2, /v3, etc.
        version_match = re.search(r'/v(\d+)$', base_url)
        if version_match:
            base_version = f"v{version_match.group(1)}"
            base_url = base_url[:version_match.start()]  # Strip version from base
        else:
            base_version = "v1"  # Default to v1

        # Normalize request path: strip any /vN prefix, then add the correct version
        target_path = re.sub(r'^/v\d+', '', path)
        if not target_path.startswith('/'):
            target_path = '/' + target_path
        target_url = f"{base_url}/{base_version}{target_path}"
        if request.query_string:
            target_url += f"?{request.query_string}"

        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "transfer-encoding", "content-length", "authorization")
        }
        headers["Content-Type"] = "application/json"
        headers["Authorization"] = f"Bearer {provider.api_key}"
        headers["X-Request-ID"] = request_id

        _log(logging.INFO, "forwarding to upstream",
             request_id=request_id, method=method, path=path,
             model=model_name, provider=provider_name,
             target_url=target_url,
             is_stream=is_stream, estimated_tokens=estimated_tokens)

        stream_prepared = False
        stream_resp: web.StreamResponse | None = None
        quota_retries = 0
        quota_max = getattr(limiter, "quota_max_retries", 0)
        quota_cooldown = getattr(limiter, "quota_retry_cooldown", 1800)

        client_session = request.app.get("client_session")
        if client_session is None or getattr(client_session, "closed", False) is True:
            error_reason = "proxy_unavailable"
            status_code = 503
            _log(logging.ERROR, "client session unavailable",
                 request_id=request_id, method=method, path=path)
            if estimated_tokens:
                await limiter.refund_tokens(estimated_tokens)
            return _make_error_response(
                503, b'{"error":"proxy unavailable, restart the server"}', request_id, retry_after=5
            )

        # Helper for provider failover
        def _advance_candidate() -> bool:
            """Advance to next candidate provider. Returns False if exhausted."""
            nonlocal candidate_idx, provider, provider_name, limiter, retry, retry_5xx
            
            # Skip providers with open circuits
            while candidate_idx + 1 < len(candidates):
                next_provider = candidates[candidate_idx + 1]
                next_limiter = get_provider_limiter(next_provider.name) if get_provider_limiter else rate_limiter
                if not next_limiter.circuit_is_open():
                    # Advance to next provider
                    candidate_idx += 1
                    provider = next_provider
                    provider_name = provider.name
                    limiter = next_limiter
                    attempted_providers.append(provider_name)
                    retry = 0
                    retry_5xx = 0
                    
                    _log(logging.WARNING, "provider failover",
                         request_id=request_id, model=model_name,
                         from_provider=attempted_providers[-2], to_provider=provider_name,
                         reason="upstream failure")
                    return True
                candidate_idx += 1  # Skip this candidate, try the next
            return False

        async def _try_failover_with_tpm() -> tuple[bool, bool]:
            """
            Try failover to next provider with proper TPM handling.
            Returns (success, tokens_reserved) - if success is False, request should be terminated.
            """
            nonlocal tokens_reserved
            
            # Refund tokens to old limiter before failover
            if tokens_reserved and estimated_tokens > 0:
                await limiter.refund_tokens(estimated_tokens)
                tokens_reserved = False
            
            # Try to advance to next provider
            if not _advance_candidate():
                # No more candidates - tokens already refunded
                return False, False
            
            # Reserve tokens from new limiter
            if estimated_tokens > 0:
                if await limiter.reserve_tokens(estimated_tokens):
                    tokens_reserved = True
                    return True, True
                else:
                    # TPM quota exceeded on new provider - log and try next
                    _log(logging.WARNING, "TPM reservation failed on failover provider",
                         request_id=request_id, model=model_name,
                         provider=provider_name, estimated_tokens=estimated_tokens)
                    # Recursively try next candidate
                    return await _try_failover_with_tpm()
            
            return True, False  # No tokens needed, failover successful

        def _rebuild_request_for_provider():
            """Rebuild target_url and headers for the current provider."""
            nonlocal target_url, headers
            
            base_url_new = provider.base_url.rstrip("/")
            version_match_new = re.search(r'/v(\d+)$', base_url_new)
            if version_match_new:
                base_version_new = f"v{version_match_new.group(1)}"
                base_url_new = base_url_new[:version_match_new.start()]
            else:
                base_version_new = "v1"
            target_path_new = re.sub(r'^/v\d+', '', path)
            if not target_path_new.startswith('/'):
                target_path_new = '/' + target_path_new
            target_url = f"{base_url_new}/{base_version_new}{target_path_new}"
            if request.query_string:
                target_url += f"?{request.query_string}"
            
            headers = {
                k: v for k, v in request.headers.items()
                if k.lower() not in ("host", "transfer-encoding", "content-length", "authorization")
            }
            headers["Content-Type"] = "application/json"
            headers["Authorization"] = f"Bearer {provider.api_key}"
            headers["X-Request-ID"] = request_id

        while retry <= limiter.max_retries and retry_5xx <= _cfg("MAX_5XX_RETRIES"):
            try:
                if app_shutting_down and app_shutting_down.is_set():
                    error_reason = "shutting_down"
                    status_code = 503
                    await limiter.refund_tokens(estimated_tokens)
                    tokens_reserved = False
                    return _make_error_response(
                        503, b'{"error":"shutting down"}', request_id, retry_after=30
                    )

                # Circuit breaker: reject immediately if circuit is open
                if limiter.circuit_is_open():
                    error_reason = "circuit_open"
                    status_code = 503
                    _log(logging.WARNING, "request rejected: circuit breaker open",
                         request_id=request_id, failure_count=limiter.circuit_failure_count)
                    await limiter.refund_tokens(estimated_tokens)
                    tokens_reserved = False
                    return _make_error_response(
                        503,
                        json.dumps({"error": "upstream unavailable", "retry_after": int(limiter.circuit_cooldown)}).encode(),
                        request_id,
                        retry_after=int(limiter.circuit_cooldown),
                    )

                if _client_disconnected(request):
                    error_reason = "client_disconnected"
                    status_code = 499
                    _log(logging.INFO, "client disconnected before upstream",
                         request_id=request_id)
                    await limiter.refund_tokens(estimated_tokens)
                    tokens_reserved = False
                    return _make_error_response(499, b'{"error":"client disconnected"}', request_id)

                if is_stream:
                    upstream = await request.app["client_session"].request(
                        method=method,
                        url=target_url,
                        headers=headers,
                        data=body_bytes,
                    )
                    try:
                        # Check for error status codes BEFORE streaming
                        if upstream.status == 429:
                            limiter.total_429s += 1
                            await limiter.record_model_stats(model_name or "unknown", 0, 0.0, is_429=True)
                            error_body = await upstream.read()
                            # Capture headers before closing
                            upstream_headers = dict(upstream.headers)
                            upstream.close()
                            if not should_retry_429(error_body):
                                if quota_retries < quota_max:
                                    quota_retries += 1
                                    _log(logging.WARNING, "upstream quota exceeded, retrying after cooldown",
                                         request_id=request_id, model=model_name,
                                         quota_retry=quota_retries, quota_max=quota_max,
                                         cooldown_sec=quota_cooldown)
                                    del error_body
                                    if not await _sleep_interruptible(request, quota_cooldown):
                                        error_reason = "client_disconnected"
                                        status_code = 499
                                        await limiter.refund_tokens(estimated_tokens)
                                        return _make_error_response(499, b'{"error":"client disconnected"}', request_id)
                                    continue
                                error_reason = "upstream_quota_exceeded"
                                status_code = 429
                                _log(logging.WARNING, "upstream quota exceeded, not retrying",
                                     request_id=request_id, model=model_name)
                                await limiter.refund_tokens(estimated_tokens)
                                resp = web.Response(status=429, body=error_body, content_type="application/json")
                                resp.headers["X-Request-ID"] = request_id
                                _add_forwarded_headers(resp, upstream_headers)
                                return resp
                            retry += 1
                            if retry > limiter.max_retries:
                                error_reason = "max_retries_429"
                                status_code = 429
                                _log(logging.ERROR, "max retries exceeded after 429",
                                     request_id=request_id, model=model_name,
                                     retry_count=retry, max_retries=limiter.max_retries)
                                # Try failover to next provider with proper TPM handling
                                success, _ = await _try_failover_with_tpm()
                                if success:
                                    _rebuild_request_for_provider()
                                    continue
                                # Tokens already refunded inside _try_failover_with_tpm()
                                resp = web.Response(status=429, body=error_body, content_type="application/json")
                                resp.headers["X-Request-ID"] = request_id
                                _add_forwarded_headers(resp, upstream_headers)
                                return resp

                            retry_wait = _compute_backoff(limiter, retry)
                            _log(logging.INFO, "429 received, backing off",
                                 request_id=request_id, model=model_name,
                                 attempt=retry, max_retries=limiter.max_retries,
                                 backoff_seconds=round(retry_wait, 1))
                            del error_body  # Release error body before backoff sleep
                            if not await _sleep_interruptible(request, retry_wait):
                                error_reason = "client_disconnected"
                                status_code = 499
                                await limiter.refund_tokens(estimated_tokens)
                                return _make_error_response(499, b'{"error":"client disconnected"}', request_id)
                            continue

                        if 400 <= upstream.status < 500:
                            error_body = await upstream.read()
                            upstream_headers = dict(upstream.headers)
                            upstream.close()
                            error_reason = "upstream_4xx"
                            status_code = upstream.status
                            _log(logging.WARNING, "upstream 4xx on streaming request",
                                 request_id=request_id, model=model_name,
                                 status=upstream.status,
                                 body_preview=error_body[:500].decode(errors="replace"))
                            await limiter.refund_tokens(estimated_tokens)
                            return _upstream_error_response(
                                upstream.status, error_body, upstream_headers, request_id
                            )

                        if 500 <= upstream.status < 600:
                            upstream_body = await upstream.read()
                            # Capture headers before closing
                            upstream_headers = dict(upstream.headers)
                            upstream.close()
                            retry_5xx += 1
                            await limiter.record_circuit_failure()
                            if retry_5xx > _cfg("MAX_5XX_RETRIES"):
                                error_reason = "upstream_5xx"
                                status_code = upstream.status
                                _log(logging.WARNING, "upstream 5xx after max retries",
                                     request_id=request_id, model=model_name,
                                     status=upstream.status, retry_5xx=retry_5xx,
                                     max_retries=_cfg("MAX_5XX_RETRIES"))
                                # Try failover to next provider with proper TPM handling
                                success, _ = await _try_failover_with_tpm()
                                if success:
                                    _rebuild_request_for_provider()
                                    continue
                                # Tokens already refunded inside _try_failover_with_tpm()
                                resp = web.Response(status=upstream.status, body=upstream_body)
                                resp.headers["X-Request-ID"] = request_id
                                _add_forwarded_headers(resp, upstream_headers)
                                return resp

                            retry_wait = 1.0 * (2 ** retry_5xx) * random.uniform(0.5, 1.5)
                            _log(logging.WARNING, "upstream 5xx, retrying",
                                 request_id=request_id, model=model_name,
                                 status=upstream.status, retry_5xx=retry_5xx,
                                 max_retries=_cfg("MAX_5XX_RETRIES"),
                                 backoff_seconds=round(retry_wait, 1))
                            del upstream_body  # Release error body before backoff sleep
                            if not await _sleep_interruptible(request, retry_wait):
                                error_reason = "client_disconnected"
                                status_code = 499
                                await limiter.refund_tokens(estimated_tokens)
                                return _make_error_response(499, b'{"error":"client disconnected"}', request_id)
                            continue

                        # Keep only a small tail buffer for token extraction
                        # (usage data is in the final SSE line). Track total
                        # bytes separately to avoid accumulating the full stream
                        # in memory (which could be 50MB+ per request).
                        tail_buffer = bytearray()
                        tail_max = 8192  # enough for the final SSE data line
                        total_stream_bytes = 0
                        tokens_from_stream = 0

                        if not (200 <= upstream.status < 400):
                            error_body = await upstream.read()
                            upstream_headers = dict(upstream.headers)
                            upstream.close()
                            error_reason = "upstream_unexpected_status"
                            status_code = 502
                            _log(logging.ERROR, "unexpected upstream status on streaming request",
                                 request_id=request_id, model=model_name, status=upstream.status)
                            await limiter.refund_tokens(estimated_tokens)
                            return _make_error_response(
                                502,
                                json.dumps({"error": "unexpected upstream response", "upstream_status": upstream.status}).encode(),
                                request_id,
                            )

                        _log(logging.DEBUG, "streaming started",
                             request_id=request_id, model=model_name,
                             upstream_status=upstream.status)

                        resp = web.StreamResponse(
                            status=upstream.status,
                            headers=_sse_response_headers(),
                        )
                        stream_resp = resp
                        resp.headers["X-Request-ID"] = request_id
                        _add_ratelimit_headers(resp, limiter)
                        await resp.prepare(request)
                        stream_prepared = True

                        try:
                            async for chunk in upstream.content:
                                total_stream_bytes += len(chunk)
                                tail_buffer.extend(chunk)
                                if len(tail_buffer) > tail_max:
                                    del tail_buffer[:len(tail_buffer) - tail_max]
                                await resp.write(chunk)
                        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError, ConnectionAbortedError):
                            _log(logging.INFO, "client disconnected during stream",
                                 request_id=request_id, model=model_name,
                                 total_bytes=total_stream_bytes)
                            upstream.close()
                            await limiter.refund_tokens(estimated_tokens)
                            return await _finalize_stream_response(resp)

                        await resp.write_eof()
                        stream_prepared = False

                        response_body_bytes = total_stream_bytes
                        token_info = extract_tokens_from_stream(bytes(tail_buffer))
                        del tail_buffer
                        duration_ms = round((time.monotonic() - request_start) * 1000, 1)
                        if token_info["total_tokens"] == 0:
                            _log(logging.WARNING, "no token usage found in stream, using estimate",
                                 request_id=request_id, estimated=estimated_tokens)
                            tokens_from_stream = estimated_tokens
                        else:
                            tokens_from_stream = token_info["total_tokens"]

                        await limiter.reconcile_tokens(estimated_tokens, tokens_from_stream)
                        tokens_reserved = False
                        await limiter.record_request(tokens_from_stream)
                        await limiter.record_circuit_success()
                        await limiter.record_model_stats(model_name or "unknown", tokens_from_stream, duration_ms)
                        await limiter.record_body_sizes(request_body_bytes, response_body_bytes)
                        actual_tokens = tokens_from_stream
                        prompt_tokens = token_info.get("prompt_tokens", 0)
                        completion_tokens = token_info.get("completion_tokens", 0)
                        cached_tokens = token_info.get("cached_tokens", 0)
                        upstream_latency_ms = round(max(0, duration_ms - queue_wait_ms), 1)
                        status_code = upstream.status
                        if response_body_bytes == 0:
                            _log(logging.WARNING, "stream completed with zero bytes",
                                 request_id=request_id, model=model_name, provider=provider_name)
                        _log(logging.INFO, "stream complete",
                             request_id=request_id, method=method, path=path,
                             model=model_name, status_code=upstream.status,
                             is_stream=True, stream_buffer_bytes=response_body_bytes,
                             estimated_tokens=estimated_tokens,
                             actual_tokens=tokens_from_stream,
                             duration_ms=duration_ms, queue_wait_ms=queue_wait_ms,
                             retry_count=retry + retry_5xx,
                             upstream_latency_ms=upstream_latency_ms,
                             response_body_bytes=response_body_bytes)
                        return resp
                    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError, ConnectionAbortedError):
                        await limiter.refund_tokens(estimated_tokens)
                        if not upstream.closed:
                            upstream.close()
                        error_reason = "client_disconnected"
                        status_code = 499
                        if stream_prepared and stream_resp is not None:
                            return await _finalize_stream_response(stream_resp)
                        return _make_error_response(499, b'{"error":"client disconnected"}', request_id)
                    except Exception as e:
                        await limiter.refund_tokens(estimated_tokens)
                        if not upstream.closed:
                            upstream.close()
                        error_reason = "streaming_error"
                        status_code = 502
                        _log(logging.ERROR, "streaming error",
                             request_id=request_id, model=model_name,
                             error_type=type(e).__name__, error=str(e))
                        if stream_prepared and stream_resp is not None:
                            return await _finalize_stream_response(stream_resp)
                        return _make_error_response(
                            502,
                            json.dumps({"error": "proxy streaming error"}).encode(),
                            request_id,
                        )
                    finally:
                        if not upstream.closed:
                            upstream.close()

                else:
                    async with request.app["client_session"].request(
                        method=method,
                        url=target_url,
                        headers=headers,
                        data=body_bytes,
                    ) as resp_up:
                        resp_body = await resp_up.read()
                        resp_headers = dict(resp_up.headers)
                        status_code = resp_up.status

                    if _client_disconnected(request):
                        error_reason = "client_disconnected"
                        await limiter.refund_tokens(estimated_tokens)
                        _log(logging.INFO, "client disconnected after upstream response",
                             request_id=request_id, model=model_name)
                        return _make_error_response(499, b'{"error":"client disconnected"}', request_id)

                    if status_code == 429:
                        limiter.total_429s += 1
                        await limiter.record_model_stats(model_name or "unknown", 0, 0.0, is_429=True)
                        if not should_retry_429(resp_body):
                            if quota_retries < quota_max:
                                quota_retries += 1
                                _log(logging.WARNING, "upstream quota exceeded, retrying after cooldown",
                                     request_id=request_id, model=model_name,
                                     quota_retry=quota_retries, quota_max=quota_max,
                                     cooldown_sec=quota_cooldown)
                                del resp_body, resp_headers
                                if not await _sleep_interruptible(request, quota_cooldown):
                                    error_reason = "client_disconnected"
                                    await limiter.refund_tokens(estimated_tokens)
                                    return _make_error_response(499, b'{"error":"client disconnected"}', request_id)
                                continue
                            error_reason = "upstream_quota_exceeded"
                            _log(logging.WARNING, "upstream quota exceeded, not retrying",
                                 request_id=request_id, model=model_name)
                            await limiter.refund_tokens(estimated_tokens)
                            out = web.Response(status=429, body=resp_body, content_type="application/json")
                            out.headers["X-Request-ID"] = request_id
                            retry_after_raw = resp_headers.get("Retry-After")
                            if retry_after_raw:
                                out.headers["Retry-After"] = retry_after_raw
                            _add_forwarded_headers(out, resp_headers)
                            return out
                        retry += 1
                        retry_after_raw = resp_headers.get("Retry-After")
                        if retry > limiter.max_retries:
                            error_reason = "max_retries_429"
                            _log(logging.ERROR, "max retries exceeded after 429",
                                 request_id=request_id, model=model_name,
                                 retry_count=retry, max_retries=limiter.max_retries)
                            # Try failover to next provider with proper TPM handling
                            success, _ = await _try_failover_with_tpm()
                            if success:
                                _rebuild_request_for_provider()
                                continue
                            # Tokens already refunded inside _try_failover_with_tpm()
                            out = web.Response(status=429, body=resp_body, content_type="application/json")
                            out.headers["X-Request-ID"] = request_id
                            if retry_after_raw:
                                out.headers["Retry-After"] = retry_after_raw
                            return out
                        if retry_after_raw:
                            parsed = parse_retry_after(retry_after_raw)
                            if parsed is not None:
                                wait = parsed
                            else:
                                wait = _compute_backoff(limiter, retry)
                        else:
                            wait = _compute_backoff(limiter, retry)

                        _log(logging.INFO, "429 received, backing off",
                             request_id=request_id, model=model_name,
                             attempt=retry, max_retries=limiter.max_retries,
                             backoff_seconds=round(wait, 1))
                        del resp_body, resp_headers  # Release response body before backoff sleep
                        if not await _sleep_interruptible(request, wait):
                            error_reason = "client_disconnected"
                            await limiter.refund_tokens(estimated_tokens)
                            return _make_error_response(499, b'{"error":"client disconnected"}', request_id)
                        continue

                    if 500 <= status_code < 600:
                        retry_5xx += 1
                        await limiter.record_circuit_failure()
                        if retry_5xx > _cfg("MAX_5XX_RETRIES"):
                            error_reason = "upstream_5xx"
                            _log(logging.WARNING, "upstream 5xx after max retries",
                                 request_id=request_id, model=model_name,
                                 status=status_code, retry_5xx=retry_5xx,
                                 max_retries=_cfg("MAX_5XX_RETRIES"))
                            # Try failover to next provider with proper TPM handling
                            success, _ = await _try_failover_with_tpm()
                            if success:
                                _rebuild_request_for_provider()
                                continue
                            # Tokens already refunded inside _try_failover_with_tpm()
                            out = web.Response(status=status_code, body=resp_body)
                            out.headers["X-Request-ID"] = request_id
                            _add_forwarded_headers(out, resp_headers)
                            return out

                        retry_wait = 1.0 * (2 ** retry_5xx) * random.uniform(0.5, 1.5)
                        _log(logging.WARNING, "upstream 5xx, retrying",
                             request_id=request_id, model=model_name,
                             status=status_code, retry_5xx=retry_5xx,
                             max_retries=_cfg("MAX_5XX_RETRIES"),
                             backoff_seconds=round(retry_wait, 1))
                        del resp_body, resp_headers  # Release response body before backoff sleep
                        if not await _sleep_interruptible(request, retry_wait):
                            error_reason = "client_disconnected"
                            await limiter.refund_tokens(estimated_tokens)
                            return _make_error_response(499, b'{"error":"client disconnected"}', request_id)
                        continue

                    tokens_used = extract_tokens_from_response(resp_body)
                    if tokens_used["total_tokens"] == 0:
                        tokens_used = {**tokens_used, "total_tokens": estimated_tokens}
                    duration_ms = round((time.monotonic() - request_start) * 1000, 1)
                    await limiter.reconcile_tokens(estimated_tokens, tokens_used["total_tokens"])
                    tokens_reserved = False
                    await limiter.record_request(tokens_used["total_tokens"])
                    await limiter.record_circuit_success()
                    await limiter.record_model_stats(model_name or "unknown", tokens_used["total_tokens"], duration_ms)
                    await limiter.record_body_sizes(request_body_bytes, len(resp_body))

                    _log(logging.DEBUG, "token reconciliation",
                         request_id=request_id, estimated=estimated_tokens,
                         actual=tokens_used["total_tokens"],
                         diff=tokens_used["total_tokens"] - estimated_tokens)

                    if status_code != 200:
                        _log(logging.ERROR, "upstream non-200 response",
                             request_id=request_id, model=model_name,
                             status=status_code,
                             body_preview=resp_body[:500].decode(errors="replace"))

                    content_type = resp_headers.get("Content-Type", "application/json")
                    content_type = content_type.split(";")[0].strip()
                    out = web.Response(
                        status=status_code,
                        body=resp_body,
                        content_type=content_type,
                    )
                    out.headers["X-Request-ID"] = request_id
                    _add_forwarded_headers(out, resp_headers)
                    _add_ratelimit_headers(out, limiter)

                    duration_ms = round((time.monotonic() - request_start) * 1000, 1)
                    actual_tokens = tokens_used["total_tokens"]
                    prompt_tokens = tokens_used.get("prompt_tokens", 0)
                    completion_tokens = tokens_used.get("completion_tokens", 0)
                    cached_tokens = tokens_used.get("cached_tokens", 0)
                    response_body_bytes = len(resp_body)
                    upstream_latency_ms = round(max(0, duration_ms - queue_wait_ms), 1)
                    _log(logging.INFO, "request complete",
                         request_id=request_id, method=method, path=path,
                         model=model_name, status_code=status_code,
                         is_stream=False,
                         estimated_tokens=estimated_tokens,
                         actual_tokens=actual_tokens,
                         duration_ms=duration_ms, queue_wait_ms=queue_wait_ms,
                         retry_count=retry + retry_5xx,
                         upstream_latency_ms=upstream_latency_ms,
                         response_body_bytes=response_body_bytes)
                    return out

            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                _log(logging.ERROR, "forward error",
                     request_id=request_id, method=method, path=path,
                     model=model_name, target_url=target_url,
                     attempt=retry + 1, error_type=type(e).__name__,
                     error=str(e))
                await limiter.record_circuit_failure()
                retry += 1
                if retry > limiter.max_retries:
                    error_reason = "max_retries_exceeded"
                    status_code = 502
                    # Try failover to next provider with proper TPM handling
                    success, _ = await _try_failover_with_tpm()
                    if success:
                        _rebuild_request_for_provider()
                        continue
                    # Tokens already refunded inside _try_failover_with_tpm()
                    return _make_error_response(502, b'{"error":"proxy forward error"}', request_id)
                if not await _sleep_interruptible(request, _compute_backoff(limiter, retry)):
                    error_reason = "client_disconnected"
                    await limiter.refund_tokens(estimated_tokens)
                    return _make_error_response(499, b'{"error":"client disconnected"}', request_id)
                continue

        error_reason = "max_retries_exhausted"
        status_code = 502
        await limiter.refund_tokens(estimated_tokens)
        tokens_reserved = False
        return _make_error_response(502, b'{"error":"proxy forward error"}', request_id)
    except asyncio.CancelledError:
        if tokens_reserved and estimated_tokens > 0:
            await limiter.refund_tokens(estimated_tokens)
        raise
    finally:
        await rate_limiter.decrement_pending()
        session_log: SessionLogWriter | None = request.app.get("session_log")
        if session_log is not None:
            session_entry["status_code"] = status_code
            session_entry["actual_tokens"] = actual_tokens
            session_entry["prompt_tokens"] = prompt_tokens
            session_entry["completion_tokens"] = completion_tokens
            session_entry["cached_tokens"] = cached_tokens
            duration_ms = round((time.monotonic() - request_start) * 1000, 1)
            session_entry["duration_ms"] = duration_ms
            session_entry["queue_wait_ms"] = queue_wait_ms
            session_entry["upstream_latency_ms"] = upstream_latency_ms
            session_entry["retry_count"] = retry + retry_5xx
            session_entry["error_reason"] = error_reason
            session_entry["request_body_bytes"] = request_body_bytes
            session_entry["response_body_bytes"] = response_body_bytes
            session_entry["remote_addr"] = remote_addr
            session_entry["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
            try:
                await session_log.log_async(session_entry)
            except Exception as e:
                _log(logging.ERROR, "session log write failed", request_id=request_id, error=str(e))


async def _maybe_flush_session_log(app: web.Application, entry: dict) -> None:
    """Best-effort session log write for early-exit paths outside the main try/finally."""
    session_log: SessionLogWriter | None = app.get("session_log")
    if session_log is None:
        return
    entry.setdefault("actual_tokens", 0)
    entry.setdefault("prompt_tokens", 0)
    entry.setdefault("completion_tokens", 0)
    entry.setdefault("cached_tokens", 0)
    entry.setdefault("duration_ms", 0.0)
    entry.setdefault("queue_wait_ms", 0.0)
    entry.setdefault("upstream_latency_ms", 0.0)
    entry.setdefault("retry_count", 0)
    entry.setdefault("request_body_bytes", 0)
    entry.setdefault("response_body_bytes", 0)
    entry.setdefault("remote_addr", None)
    entry.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat())
    try:
        await session_log.log_async(entry)
    except Exception:
        pass
