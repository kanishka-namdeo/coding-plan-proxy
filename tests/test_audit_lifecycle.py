"""Lifecycle/audit tests.

Covers the safe-config-parsing, session-log close semantics, logging
topology, and server startup/PID hardening changes:

- module-level safe parsing of ``SESSION_LOG_*`` env vars (no import crash)
- ``SessionLogWriter.close(join_timeout=...)`` timeout path (stuck writer
  keeps the file handle; no reopen; ``log()`` stays a no-op)
- ``_safe_float`` rejecting nan/inf/empty
- ``LOG_BUFFER_SIZE`` floor of 1
- ``MODEL_FALLBACK_ORDER`` provider-slug normalization
- logging topology: root NullHandler, single structured headless stream
  handler, idempotent reconfiguration, TUI mode untouched
- server port validation + two-line PID file round-trip
"""

import importlib
import logging
import os
import threading
import time
from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# Item 1: module-level safe parsing of session log env vars
# ---------------------------------------------------------------------------

class TestSessionLogSafeParsing:
    def test_bad_env_vars_fall_back_to_defaults(self, monkeypatch):
        """A .env typo must not crash ``import dashscope_proxy_lib.session_log``."""
        monkeypatch.setenv("SESSION_LOG_QUEUE_MAX", "abc")
        monkeypatch.setenv("SESSION_LOG_FLUSH_EVERY", "not-a-number")
        monkeypatch.setenv("SESSION_LOG_FLUSH_INTERVAL", "garbage")
        import dashscope_proxy_lib.session_log as sl_mod

        try:
            reloaded = importlib.reload(sl_mod)
        finally:
            # Restore the environment and re-parse so later tests see the
            # original module constants.
            monkeypatch.delenv("SESSION_LOG_QUEUE_MAX", raising=False)
            monkeypatch.delenv("SESSION_LOG_FLUSH_EVERY", raising=False)
            monkeypatch.delenv("SESSION_LOG_FLUSH_INTERVAL", raising=False)
            importlib.reload(sl_mod)

        assert reloaded.SESSION_LOG_QUEUE_MAX == 10000
        assert reloaded.SESSION_LOG_FLUSH_EVERY == 32
        assert reloaded.SESSION_LOG_FLUSH_INTERVAL == 0.25


# ---------------------------------------------------------------------------
# Item 2: close() join timeout — stuck writer keeps the file handle
# ---------------------------------------------------------------------------

class _BlockingFile:
    """File handle wrapper whose flush() blocks on an Event until released."""

    def __init__(self, real):
        self._real = real
        self.block_event = threading.Event()
        self.blocked = False
        self.close_calls = 0

    def write(self, data):
        return self._real.write(data)

    def flush(self):
        self.blocked = True
        self.block_event.wait()
        self.blocked = False

    def close(self):
        self.close_calls += 1
        self._real.close()


class TestSessionLogCloseJoinTimeout:
    def test_close_times_out_without_closing_writer_file(self, tmp_path):
        from dashscope_proxy_lib.session_log import SessionLogWriter

        writer = SessionLogWriter(str(tmp_path / "logs"), flush_interval=0.05)
        writer.log({"request_id": "a"})
        real_file = writer._file
        assert real_file is not None

        blocking = _BlockingFile(real_file)
        with writer._lock:
            writer._file = blocking

        # Wait until the background writer is actually blocked in flush()
        # (holding _lock), so close()'s join genuinely times out.
        deadline = time.monotonic() + 5.0
        while not blocking.blocked and time.monotonic() < deadline:
            time.sleep(0.01)
        assert blocking.blocked, "writer never reached a blocked flush"

        t0 = time.monotonic()
        writer.close(join_timeout=0.2)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, "close() should return after the join timeout"

        # Timeout path: the live writer still owns the file handle.
        assert blocking.close_calls == 0
        assert writer._writer_thread is not None
        assert writer._writer_thread.is_alive()

        # log() after close must be a safe no-op (no reopen, no ValueError).
        writer.log({"request_id": "b"})

        # Release the stuck writer and let it exit on its own.
        blocking.block_event.set()
        writer._writer_thread.join(timeout=5.0)
        assert not writer._writer_thread.is_alive()

        real_file.close()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        content = (tmp_path / "logs" / f"{today}.jsonl").read_text(encoding="utf-8")
        assert content.count('"request_id"') == 1
        assert '"a"' in content
        assert '"b"' not in content


# ---------------------------------------------------------------------------
# Item 4: _safe_float rejects non-finite values
# ---------------------------------------------------------------------------

class TestSafeFloat:
    def test_non_finite_and_bad_values_return_default(self, monkeypatch):
        import dashscope_proxy_lib.config as cfg

        for bad in ("nan", "inf", "-inf", ""):
            monkeypatch.setenv("AUDIT_FLOAT", bad)
            assert cfg._safe_float("AUDIT_FLOAT", 3.5) == 3.5

        monkeypatch.setenv("AUDIT_FLOAT", "2.5")
        assert cfg._safe_float("AUDIT_FLOAT", 3.5) == 2.5

        monkeypatch.delenv("AUDIT_FLOAT")
        assert cfg._safe_float("AUDIT_FLOAT", 3.5) == 3.5

    def test_safe_int_still_parses(self, monkeypatch):
        import dashscope_proxy_lib.config as cfg

        monkeypatch.setenv("AUDIT_INT", "17")
        assert cfg._safe_int("AUDIT_INT", 3) == 17
        monkeypatch.setenv("AUDIT_INT", "x")
        assert cfg._safe_int("AUDIT_INT", 3) == 3


# ---------------------------------------------------------------------------
# Item 5: LOG_BUFFER_SIZE floor
# ---------------------------------------------------------------------------

class TestLogBufferSizeFloor:
    def test_floor_at_one(self, monkeypatch):
        import dashscope_proxy_lib.config as cfg_mod

        try:
            for env_value in ("0", "-5"):
                monkeypatch.setenv("LOG_BUFFER_SIZE", env_value)
                reloaded = importlib.reload(cfg_mod)
                assert reloaded.LOG_BUFFER_SIZE >= 1
        finally:
            monkeypatch.delenv("LOG_BUFFER_SIZE", raising=False)
            importlib.reload(cfg_mod)

    def test_normal_value_passes_through(self, monkeypatch):
        import dashscope_proxy_lib.config as cfg_mod

        try:
            monkeypatch.setenv("LOG_BUFFER_SIZE", "512")
            reloaded = importlib.reload(cfg_mod)
            assert reloaded.LOG_BUFFER_SIZE == 512
        finally:
            monkeypatch.delenv("LOG_BUFFER_SIZE", raising=False)
            importlib.reload(cfg_mod)


# ---------------------------------------------------------------------------
# Item 6: MODEL_FALLBACK_ORDER slug normalization
# ---------------------------------------------------------------------------

class TestModelFallbackOrderNormalization:
    def test_slugs_map_to_canonical_names(self, monkeypatch):
        import dashscope_proxy_lib.config as cfg_mod

        try:
            monkeypatch.setenv("MODEL_FALLBACK_ORDER", "openlux,ark")
            reloaded = importlib.reload(cfg_mod)
            assert reloaded.MODEL_FALLBACK_ORDER == ["tertiary", "quaternary"]
        finally:
            monkeypatch.delenv("MODEL_FALLBACK_ORDER", raising=False)
            importlib.reload(cfg_mod)

    def test_canonical_names_and_unknown_entries_pass_through(self, monkeypatch):
        import dashscope_proxy_lib.config as cfg_mod

        try:
            monkeypatch.setenv("MODEL_FALLBACK_ORDER", "zai,septenary,bogus")
            reloaded = importlib.reload(cfg_mod)
            assert reloaded.MODEL_FALLBACK_ORDER == ["septenary", "septenary", "bogus"]
        finally:
            monkeypatch.delenv("MODEL_FALLBACK_ORDER", raising=False)
            importlib.reload(cfg_mod)

    def test_slug_sources_are_aligned(self, dashscope_module):
        """Every non-canonical key in request_transform.PROVIDER_SLUG_MAP must
        exist in config.PROVIDER_SLUGS with the same target."""
        from dashscope_proxy_lib import config
        from dashscope_proxy_lib.request_transform import PROVIDER_SLUG_MAP

        canonical = {
            "primary", "secondary", "tertiary", "quaternary",
            "quinary", "senary", "septenary",
            "octonary", "nonary", "decenary",
        }
        for slug, target in PROVIDER_SLUG_MAP.items():
            if slug in canonical:
                continue
            assert config.PROVIDER_SLUGS.get(slug) == target, f"missing alias {slug!r}"


# ---------------------------------------------------------------------------
# Item 7: logging topology
# ---------------------------------------------------------------------------

def _headless_handlers(package_logger):
    return [h for h in package_logger.handlers if getattr(h, "audit_headless", False)]


class TestLoggingTopology:
    def test_root_logger_carries_null_handler(self):
        import dashscope_proxy_lib.logging_config  # noqa: F401 — attach runs at import
        root = logging.getLogger()
        assert any(isinstance(h, logging.NullHandler) for h in root.handlers)

    def test_headless_attaches_exactly_one_structured_stream_handler(
        self, dashscope_module
    ):
        import dashscope_proxy_lib.logging_config as lc

        try:
            lc.configure_logging(enable_tui_handler=False)
            handlers = _headless_handlers(lc.package_logger)
            assert len(handlers) == 1
            assert isinstance(handlers[0], logging.StreamHandler)
            assert isinstance(handlers[0].formatter, lc.StructuredLogFormatter)
            assert not any(
                isinstance(h, lc.TUILogHandler) for h in lc.package_logger.handlers
            )

            # Idempotent: re-calling must not duplicate the handler.
            lc.configure_logging(enable_tui_handler=False)
            assert len(_headless_handlers(lc.package_logger)) == 1
        finally:
            lc.configure_logging(enable_tui_handler=True)

    def test_tui_mode_removes_headless_handler_and_keeps_single_tui_handler(
        self, dashscope_module
    ):
        import dashscope_proxy_lib.logging_config as lc

        try:
            lc.configure_logging(enable_tui_handler=False)
            lc.configure_logging(enable_tui_handler=True)
            assert not _headless_handlers(lc.package_logger)
            tui = [h for h in lc.package_logger.handlers if isinstance(h, lc.TUILogHandler)]
            assert tui == [lc.tui_handler]

            # Idempotent in TUI mode too.
            lc.configure_logging(enable_tui_handler=True)
            assert len([
                h for h in lc.package_logger.handlers if isinstance(h, lc.TUILogHandler)
            ]) == 1
        finally:
            lc.configure_logging(enable_tui_handler=False)


# ---------------------------------------------------------------------------
# Item 8: server port validation
# ---------------------------------------------------------------------------

class TestServerPortValidation:
    async def test_invalid_port_aborts_startup(self, monkeypatch, tmp_path):
        import dashscope_proxy_lib.server as srv

        monkeypatch.setattr(srv, "PID_FILE", str(tmp_path / "proxy.pid"))
        for bad_port in (0, 70000, -1):
            monkeypatch.setattr(srv, "PROXY_PORT", bad_port)
            assert await srv.create_proxy_resources() is None

    async def test_port_in_use_aborts_and_cleans_up(self, monkeypatch, tmp_path):
        import socket

        import dashscope_proxy_lib.server as srv

        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        port = blocker.getsockname()[1]
        blocker.listen(1)
        monkeypatch.setattr(srv, "PID_FILE", str(tmp_path / "proxy.pid"))
        monkeypatch.setattr(srv, "PROXY_PORT", port)
        try:
            assert await srv.create_proxy_resources() is None
        finally:
            blocker.close()

    async def test_runner_setup_failure_cleans_created_resources(self, monkeypatch):
        import dashscope_proxy_lib.server as srv

        class FailingRunner:
            def __init__(self, app):
                self.cleaned = False

            async def setup(self):
                raise RuntimeError("setup failed")

            async def cleanup(self):
                self.cleaned = True

        runner = FailingRunner(None)
        monkeypatch.setattr(srv.web, "AppRunner", lambda app: runner)
        monkeypatch.setattr(srv, "DASHSCOPE_API_KEY", "test-key")
        monkeypatch.setattr(srv, "SESSION_LOG_ENABLED", False)
        with pytest.raises(RuntimeError, match="setup failed"):
            await srv.create_proxy_resources()
        assert runner.cleaned is True


# ---------------------------------------------------------------------------
# Item 10: bounded server session-log shutdown
# ---------------------------------------------------------------------------

class TestServerSessionLogShutdown:
    async def test_cleanup_does_not_wait_forever_for_stuck_writer(self, monkeypatch):
        import asyncio
        import dashscope_proxy_lib.server as srv

        class Limiter:
            max_queue_size = 1
            pending_requests = 0

            def status(self):
                return {}

        release = threading.Event()

        class StuckLog:
            def close(self):
                release.wait(30)

        class Runner:
            cleaned = False

            async def cleanup(self):
                self.cleaned = True

        runner = Runner()
        app = {
            "rate_limiter": Limiter(),
            "shutting_down": asyncio.Event(),
            "session_log": StuckLog(),
        }
        monkeypatch.setattr(srv, "SESSION_LOG_CLOSE_TIMEOUT", 0.01, raising=False)
        await srv.cleanup_proxy_resources(app, runner)
        release.set()
        assert runner.cleaned is True


# ---------------------------------------------------------------------------
# Item 11: PID file marker round-trip
# ---------------------------------------------------------------------------

class TestPidFileMarker:
    def test_pid_file_is_absolute(self, dashscope_module):
        import dashscope_proxy_lib.server as srv

        assert os.path.isabs(srv.PID_FILE)
        assert srv.PID_FILE.endswith("proxy.pid")

    def test_pid_marker_round_trip(self, monkeypatch, tmp_path):
        import dashscope_proxy_lib.server as srv

        pid_path = tmp_path / "proxy.pid"
        monkeypatch.setattr(srv, "PID_FILE", str(pid_path))

        srv._write_pid_file()
        lines = pid_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert lines[0] == str(os.getpid())
        started = datetime.fromisoformat(lines[1])
        assert started.tzinfo is not None

        # Live PID: file is kept, warning path with the stored timestamp.
        srv._check_stale_pid_file()
        assert pid_path.exists()

        # Dead PID: stale file is removed on next startup check. Use a very
        # large PID that is never allocated (psutil.pid_exists is reliably
        # False); on Windows, pid 0 is the live System process.
        pid_path.write_text(f"{2**31 - 1}\n2026-01-01T00:00:00+00:00\n", encoding="utf-8")
        srv._check_stale_pid_file()
        assert not pid_path.exists()
