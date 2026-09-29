"""Server lifecycle: app factory, resource management, and entry point."""

import asyncio
import logging
import signal
import time
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from aiohttp.web_exceptions import HTTPRequestEntityTooLarge

from dashscope_proxy_lib.config import (
    DASHSCOPE_API_KEY, MAX_BODY_SIZE, MAX_CONNECTIONS, MAX_CONNECTIONS_PER_HOST,
    PROXY_HOST, PROXY_PORT, TARGET_BASE, UPSTREAM_TIMEOUT_CONNECT,
    UPSTREAM_TIMEOUT_TOTAL, SECONDARY_API_KEY, SECONDARY_BASE_URL,
    SECONDARY_CODING_PLAN_CONFIG, TERTIARY_API_KEY, TERTIARY_BASE_URL,
    TERTIARY_CODING_PLAN_CONFIG, QUATERNARY_API_KEY, QUATERNARY_BASE_URL,
    QUATERNARY_CODING_PLAN_CONFIG, QUINARY_API_KEY, QUINARY_BASE_URL,
    QUINARY_CODING_PLAN_CONFIG, SENARY_API_KEY, SENARY_BASE_URL,
    SENARY_CODING_PLAN_CONFIG, SEPTENARY_API_KEY, SEPTENARY_BASE_URL,
    SEPTENARY_CODING_PLAN_CONFIG, OCTONARY_API_KEY, OCTONARY_BASE_URL,
    OCTONARY_CODING_PLAN_CONFIG, NONARY_API_KEY, NONARY_BASE_URL,
    NONARY_CODING_PLAN_CONFIG, DECENARY_API_KEY, DECENARY_BASE_URL,
    DECENARY_CODING_PLAN_CONFIG,
)
from dashscope_proxy_lib.rate_limiter import RateLimiter, MultiProviderRateLimiter
from dashscope_proxy_lib.session_log import SessionLogWriter, SESSION_LOG_DIR, SESSION_LOG_ENABLED
from dashscope_proxy_lib.logging_config import _log, tui_handler, configure_logging
from dashscope_proxy_lib.handlers import handle_request
from dashscope_proxy_lib.config import _load_config
import os

# Absolute path against the project root (same pattern as SESSION_LOG_DIR) so
# the PID file does not depend on the process working directory.
PID_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy.pid"
)
SESSION_LOG_CLOSE_TIMEOUT = 5.0


def _write_pid_file() -> None:
    """Write current PID + ISO-8601 UTC start timestamp to the PID file."""
    try:
        started_utc = datetime.now(timezone.utc).isoformat()
        with open(PID_FILE, "w") as f:
            f.write(f"{os.getpid()}\n{started_utc}\n")
        _log(logging.INFO, "PID file written", pid=os.getpid(), path=PID_FILE)
    except OSError as e:
        _log(logging.WARNING, "failed to write PID file", error=str(e))


def _remove_pid_file() -> None:
    """Remove PID file on shutdown."""
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
            _log(logging.INFO, "PID file removed", path=PID_FILE)
    except OSError as e:
        _log(logging.WARNING, "failed to remove PID file", error=str(e))


def _check_stale_pid_file() -> None:
    """Warn if a PID file exists but the process is no longer running."""
    if not os.path.exists(PID_FILE):
        return
    try:
        with open(PID_FILE, "r") as f:
            lines = f.read().splitlines()
        old_pid = int(lines[0].strip()) if lines and lines[0].strip() else 0
        started_utc = lines[1].strip() if len(lines) > 1 else "(no start timestamp)"
        # Check if process is still running (cross-platform)
        import psutil
        if psutil.pid_exists(old_pid):
            _log(logging.WARNING, "stale PID file found — proxy may already be running",
                 pid=old_pid, started_utc=started_utc, path=PID_FILE)
        else:
            _log(logging.INFO, "removing stale PID file", pid=old_pid,
                 started_utc=started_utc, path=PID_FILE)
            os.remove(PID_FILE)
    except (ValueError, ImportError, OSError) as e:
        _log(logging.WARNING, "could not check stale PID file", error=str(e))



@web.middleware
async def error_middleware(request: web.Request, handler):
    """Catch unhandled exceptions so clients get JSON errors instead of aiohttp 500 pages."""
    try:
        return await handler(request)
    except HTTPRequestEntityTooLarge:
        return web.json_response({"error": "payload too large"}, status=413)
    except Exception as e:
        _log(logging.ERROR, "unhandled proxy exception",
             path=request.path, method=request.method, exc_info=True)
        return web.json_response({"error": "internal proxy error", "details": f"{type(e).__name__}: {str(e)}"}, status=502)


def create_app() -> web.Application:
    # Allow slightly over MAX_BODY_SIZE so the handler can return a proper 413 JSON body.
    app = web.Application(middlewares=[error_middleware], client_max_size=MAX_BODY_SIZE + 1024)
    app.add_routes([web.route("*", "/{tail:.*}", handle_request)])
    return app


async def create_proxy_resources() -> tuple[MultiProviderRateLimiter, web.Application, web.AppRunner] | None:
    """Create and start all proxy resources (rate limiter, app, client session, runner).

    Returns (rate_limiter, app, runner) ready for TUI integration, or ``None``
    when startup must abort (invalid port, port in use).
    """
    _check_stale_pid_file()

    if not DASHSCOPE_API_KEY:
        _log(logging.ERROR, "DASHSCOPE_API_KEY not set")
        raise SystemExit(1)

    if not isinstance(PROXY_PORT, int) or not (1 <= PROXY_PORT <= 65535):
        _log(logging.ERROR,
             "DASHSCOPE_PROXY_PORT is not a valid port; expected an integer in 1..65535",
             port=repr(PROXY_PORT))
        return None


    config = _load_config()

    # Create multi-provider rate limiter
    secondary_config = SECONDARY_CODING_PLAN_CONFIG if (SECONDARY_API_KEY and SECONDARY_BASE_URL) else None
    tertiary_config = TERTIARY_CODING_PLAN_CONFIG if (TERTIARY_API_KEY and TERTIARY_BASE_URL) else None
    quaternary_config = QUATERNARY_CODING_PLAN_CONFIG if (QUATERNARY_API_KEY and QUATERNARY_BASE_URL) else None
    quinary_config = QUINARY_CODING_PLAN_CONFIG if (QUINARY_API_KEY and QUINARY_BASE_URL) else None
    senary_config = SENARY_CODING_PLAN_CONFIG if (SENARY_API_KEY and SENARY_BASE_URL) else None
    septenary_config = SEPTENARY_CODING_PLAN_CONFIG if (SEPTENARY_API_KEY and SEPTENARY_BASE_URL) else None
    octonary_config = OCTONARY_CODING_PLAN_CONFIG if (OCTONARY_API_KEY and OCTONARY_BASE_URL) else None
    nonary_config = NONARY_CODING_PLAN_CONFIG if (NONARY_API_KEY and NONARY_BASE_URL) else None
    decenary_config = DECENARY_CODING_PLAN_CONFIG if (DECENARY_API_KEY and DECENARY_BASE_URL) else None
    rate_limiter = MultiProviderRateLimiter(config, secondary_config, tertiary_config, quaternary_config, quinary_config, senary_config, septenary_config, octonary_config, nonary_config, decenary_config)

    if secondary_config or tertiary_config or quaternary_config or quinary_config or senary_config or septenary_config or octonary_config or nonary_config or decenary_config:
        _log(logging.INFO, "multi-provider mode enabled",
             secondary_url=SECONDARY_BASE_URL if secondary_config else None,
             tertiary_url=TERTIARY_BASE_URL if tertiary_config else None,
             quaternary_url=QUATERNARY_BASE_URL if quaternary_config else None,
             quinary_url=QUINARY_BASE_URL if quinary_config else None,
             senary_url=SENARY_BASE_URL if senary_config else None,
             septenary_url=SEPTENARY_BASE_URL if septenary_config else None,
             octonary_url=OCTONARY_BASE_URL if octonary_config else None,
             nonary_url=NONARY_BASE_URL if nonary_config else None,
             decenary_url=DECENARY_BASE_URL if decenary_config else None)
    else:
        _log(logging.INFO, "single-provider mode (secondary/tertiary/quaternary/quinary/senary/septenary/octonary/nonary/decenary not configured)")

    app = create_app()
    timeout = aiohttp.ClientTimeout(total=UPSTREAM_TIMEOUT_TOTAL, connect=UPSTREAM_TIMEOUT_CONNECT)
    connector = aiohttp.TCPConnector(
        limit=MAX_CONNECTIONS,
        limit_per_host=MAX_CONNECTIONS_PER_HOST,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    app["client_session"] = aiohttp.ClientSession(timeout=timeout, connector=connector)
    app["connector"] = connector
    app["rate_limiter"] = rate_limiter
    app["shutting_down"] = asyncio.Event()
    app["event_loop"] = asyncio.get_event_loop()
    if SESSION_LOG_ENABLED:
        app["session_log"] = SessionLogWriter(SESSION_LOG_DIR)
        _log(logging.INFO, "session log writer initialized", log_dir=SESSION_LOG_DIR)

    runner = web.AppRunner(app)

    async def _cleanup_startup_failure() -> None:
        session = app.get("client_session")
        if session and not session.closed:
            await session.close()
        connector = app.get("connector")
        if connector is not None and not connector.closed:
            await connector.close()
        session_log = app.get("session_log")
        if session_log:
            try:
                session_log.close()
            except Exception:
                pass
        try:
            await runner.cleanup()
        except Exception:
            pass

    try:
        await runner.setup()
        site = web.TCPSite(runner, PROXY_HOST, PROXY_PORT)
        await site.start()
    except Exception as e:
        await _cleanup_startup_failure()
        if isinstance(e, OSError):
            _log(logging.ERROR,
                 f"proxy port {PROXY_HOST}:{PROXY_PORT} is in use or unavailable; aborting startup",
                 error=str(e))
            return None
        raise
    _write_pid_file()

    _log(logging.INFO, "proxy started",
         host=PROXY_HOST, port=PROXY_PORT, target=TARGET_BASE,
         rps=rate_limiter.primary.rps_limit, rpm=rate_limiter.primary.rpm_limit,
         tpm=rate_limiter.primary.tpm_limit,
         safety_factor=config["safety_factor"])

    # Signal handlers
    shutdown_event = app["shutting_down"]

    def _handle_signal():
        if not shutdown_event.is_set():
            _log(logging.INFO, "shutdown signal received")
            shutdown_event.set()

    def _fallback_signal_handler(_signum, _frame):
        # Runs on the main thread; route to the same shutdown event.
        if loop.is_running():
            loop.call_soon_threadsafe(shutdown_event.set)

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, _handle_signal)
        loop.add_signal_handler(signal.SIGINT, _handle_signal)
    except (NotImplementedError, OSError):
        # Windows: add_signal_handler is unavailable; fall back to signal.signal
        # for SIGINT and (Windows-only) SIGBREAK so Ctrl+C / Ctrl+Break still
        # trigger the same graceful shutdown. Best effort.
        _fallback_signals = [signal.SIGINT]
        _sig_break = getattr(signal, "SIGBREAK", None)
        if _sig_break is not None:
            _fallback_signals.append(_sig_break)
        _registered = False
        for _sig in _fallback_signals:
            try:
                signal.signal(_sig, _fallback_signal_handler)
                _registered = True
            except (OSError, ValueError, RuntimeError, TypeError):
                continue
        if not _registered:
            _log(logging.WARNING, "signal handlers not supported on this platform")

    return rate_limiter, app, runner


async def cleanup_proxy_resources(
    app: web.Application,
    runner: web.AppRunner,
) -> None:
    """Gracefully shut down the proxy: drain requests, close session, cleanup runner."""
    rate_limiter: RateLimiter = app["rate_limiter"]
    shutdown_event = app.get("shutting_down")
    if shutdown_event and not shutdown_event.is_set():
        shutdown_event.set()

    # Stop accepting new queued work immediately
    rate_limiter.max_queue_size = 0

    # Brief drain window — don't block indefinitely on pending work
    drain_deadline = time.monotonic() + 5
    while rate_limiter.pending_requests > 0 and time.monotonic() < drain_deadline:
        await asyncio.sleep(0.2)
    if rate_limiter.pending_requests > 0:
        _log(logging.WARNING, "shutdown timed out with pending requests",
             pending=rate_limiter.pending_requests)
    else:
        _log(logging.INFO, "all in-flight requests drained")

    _log(logging.INFO, "final metrics", metrics=rate_limiter.status())

    session = app.get("client_session")
    connector = app.get("connector")
    if session and not session.closed:
        await session.close()
        # Allow in-flight transports to finish closing before runner teardown.
        await asyncio.sleep(0.25)
    if connector is not None and not connector.closed:
        await connector.close()
    session_log = app.get("session_log")
    if session_log:
        # Run the (potentially slow) close off the event loop so a stuck
        # writer thread cannot freeze shutdown; best effort.
        loop = asyncio.get_running_loop()
        try:
            close_future = loop.run_in_executor(None, session_log.close)
            await asyncio.wait_for(close_future, timeout=SESSION_LOG_CLOSE_TIMEOUT)
            _log(logging.INFO, "session log writer closed")
        except Exception as e:
            _log(logging.WARNING, "session log close raised or timed out", error=str(e))
    await runner.cleanup()
    _remove_pid_file()
    _log(logging.INFO, "shutdown complete")



async def main(headless: bool = False):
    """Entry point: create proxy resources, then launch TUI with shared event loop."""
    configure_logging(enable_tui_handler=not headless)
    resources = await create_proxy_resources()
    if resources is None:
        # Startup was aborted (invalid port / port in use) — already logged.
        return
    rate_limiter, app, runner = resources

    tui_app = None
    tui_thread = None
    try:
        if not headless:
            from proxy_tui import ProxyTUI
            import threading

            tui_app = ProxyTUI(
                rate_limiter=rate_limiter,
                tui_log_handler=tui_handler,
                proxy_app=app,
            )

            def run_tui():
                tui_app.run()

            tui_thread = threading.Thread(target=run_tui, daemon=True, name="tui-thread")
            tui_thread.start()
            _log(logging.INFO, "TUI started in background thread")

            # Wait for shutdown signal
            await app["shutting_down"].wait()
            _log(logging.INFO, "shutdown signal received, closing TUI")
            if tui_app:
                tui_app.exit()
            if tui_thread:
                tui_thread.join(timeout=5)
                if tui_thread.is_alive():
                    raise RuntimeError("TUI thread did not stop within the shutdown deadline")
        else:
            _log(logging.INFO, "proxy running in headless mode (no TUI)")
            await app["shutting_down"].wait()
    finally:
        await cleanup_proxy_resources(app, runner)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DashScope API Proxy")
    parser.add_argument("--headless", action="store_true", help="Run without TUI")
    args = parser.parse_args()
    asyncio.run(main(headless=args.headless))
