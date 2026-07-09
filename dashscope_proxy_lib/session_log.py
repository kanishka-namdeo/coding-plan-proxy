"""Session log file writer with daily rotation and async support."""

import asyncio
import json
import os
import typing
from concurrent.futures import ThreadPoolExecutor

# These are resolved via the facade module at runtime so that tests can
# patch ``dashscope_proxy.datetime`` / ``dashscope_proxy.timezone``.
# No top-level ``from datetime import …`` — the late import inside
# ``_ensure_file`` avoids circular imports because ``dashscope_proxy`` is
# fully loaded by the time any method is called.

# Session log file configuration
SESSION_LOG_DIR = os.environ.get("SESSION_LOG_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session_logs"))
SESSION_LOG_ENABLED = os.environ.get("SESSION_LOG_ENABLED", "1") == "1"


class SessionLogWriter:
    """Append one JSON-line entry per request to a daily-rotating file.
    
    Uses a thread pool executor for blocking I/O to avoid blocking the event loop.
    """

    def __init__(self, log_dir: str):
        import threading
        self.log_dir = log_dir
        self._current_date: str | None = None
        self._file: typing.TextIO | None = None
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-log")

    def _ensure_file(self) -> None:
        """Must be called with self._lock held."""
        import sys
        _ds = sys.modules.get("dashscope_proxy")
        if _ds is not None and hasattr(_ds, "datetime"):
            today = _ds.datetime.now(_ds.timezone.utc).strftime("%Y-%m-%d")
        else:
            from datetime import datetime as _dt, timezone as _tz
            today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
        if today == self._current_date and self._file is not None:
            return
        os.makedirs(self.log_dir, exist_ok=True)
        path = os.path.join(self.log_dir, f"{today}.jsonl")
        new_file = open(path, "a", encoding="utf-8")
        if self._file is not None:
            self._file.close()
        self._file = new_file
        self._current_date = today

    def _write_sync(self, entry: dict) -> None:
        """Synchronous write (called from thread pool). Must be called with self._lock held."""
        self._ensure_file()
        self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._file.flush()

    async def log_async(self, entry: dict) -> None:
        """Async wrapper that offloads blocking I/O to a thread pool."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._log_with_lock, entry)

    def _log_with_lock(self, entry: dict) -> None:
        """Helper to acquire lock and write (runs in thread pool)."""
        with self._lock:
            self._write_sync(entry)

    def log(self, entry: dict) -> None:
        """Write one JSON line synchronously. Thread-safe via threading.Lock.
        
        For async contexts, use log_async() instead to avoid blocking the event loop.
        """
        with self._lock:
            self._write_sync(entry)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._executor.shutdown(wait=True)
