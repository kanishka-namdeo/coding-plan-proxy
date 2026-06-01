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
        """Return the most recent log entries with seq >= from_seq."""
        with self._lock:
            return [e for e in self.buffer if e.get("seq", 0) >= from_seq][-limit:]

    def clear(self) -> None:
        with self._lock:
            self.buffer.clear()
            self._next_seq = 0


def _log(level: int, msg: str, **extra):
    """Emit a structured log with optional key-value context."""
    if not logger.isEnabledFor(level):
        return
    record = logger.makeRecord(logger.name, level, "", 0, msg, (), None)
    if extra:
        record.extra_context = extra
    logger.handle(record)


logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
tui_handler = TUILogHandler()
logger.addHandler(tui_handler)
