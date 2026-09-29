"""Logging infrastructure for the DashScope proxy."""

import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone

from dashscope_proxy_lib.config import LOG_LEVEL, LOG_BUFFER_SIZE


class StructuredLogFormatter(logging.Formatter):
    """Outputs JSON-structured log lines for machine parsing."""

    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_context", {})
        if extra:
            log_entry.update(extra)
        return json.dumps(log_entry, ensure_ascii=False)


class TUILogHandler(logging.Handler):
    """Thread-safe log handler that feeds records into a shared deque for TUI consumption."""

    def __init__(self, max_size: int = LOG_BUFFER_SIZE):
        super().__init__()
        self.buffer: deque[dict] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._next_seq: int = 0

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "timestamp": self._format_time(record),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra_context") and record.extra_context:
            entry["extra"] = record.extra_context
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.format(record)
        with self._lock:
            entry["seq"] = self._next_seq
            self._next_seq += 1
            self.buffer.append(entry)

    def _format_time(self, record: logging.LogRecord) -> str:
        """Format timestamp from log record."""
        try:
            return self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()

    def get_logs(self, limit: int = 100, from_seq: int = 0) -> list[dict]:
        """Return the earliest log entries with seq >= from_seq."""
        with self._lock:
            matches = [e for e in self.buffer if e.get("seq", 0) >= from_seq]
            return matches[:limit]

    def snapshot(self) -> list[dict]:
        """Return a stable, lock-protected copy of the buffered entries."""
        with self._lock:
            return list(self.buffer)

    def clear(self) -> None:
        with self._lock:
            self.buffer.clear()
            self._next_seq = 0


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Package-level logger. The TUI handler attaches here so records from every
# dashscope_proxy_lib module logger (handlers, session_log, ...) reach the TUI
# feed via propagation, not just _log() callers in this module.
package_logger = logging.getLogger("dashscope_proxy_lib")

tui_handler = TUILogHandler()

# Kill the ``logging.lastResort`` fall-through: with no handler on the root
# logger, every WARNING+ record that propagates up would be re-emitted
# unstructured to stderr (duplicating the TUI/structured handlers). Idempotent.
_root_logger = logging.getLogger()
if not any(isinstance(h, logging.NullHandler) for h in _root_logger.handlers):
    _root_logger.addHandler(logging.NullHandler())


def _log(level: int, msg: str, **extra):
    """Emit a structured log with optional key-value context."""
    if not logger.isEnabledFor(level):
        return
    record = logger.makeRecord(logger.name, level, "", 0, msg, (), None)
    if extra:
        record.extra_context = extra
    logger.handle(record)


def _headless_stream_handler() -> logging.StreamHandler:
    """Structured stderr handler used by ``configure_logging`` in headless mode.

    Marked ``audit_headless`` so ``configure_logging`` can detect and replace
    it on reconfiguration (idempotent, no duplicate handlers).
    """
    handler = logging.StreamHandler()
    handler.audit_headless = True  # type: ignore[attr-defined]
    handler.setFormatter(StructuredLogFormatter())
    return handler


def configure_logging(*, enable_tui_handler: bool = True) -> None:
    """Configure proxy logging; optionally attach the TUI log handler.

    Headless (``enable_tui_handler=False``) attaches exactly one structured
    ``StreamHandler`` to the package logger so INFO/DEBUG records still reach
    stderr as JSON. Re-calls are idempotent: previously attached headless
    handlers are removed, and enabling the TUI handler removes the headless
    stream handler so records are not duplicated.
    """
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    package_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    logger.handlers = [
        h for h in logger.handlers
        if not isinstance(h, (logging.NullHandler, TUILogHandler))
    ]
    package_logger.handlers = [
        h for h in package_logger.handlers
        if not isinstance(h, TUILogHandler) and not getattr(h, "audit_headless", False)
    ]
    if enable_tui_handler:
        package_logger.addHandler(tui_handler)
    else:
        package_logger.addHandler(_headless_stream_handler())
