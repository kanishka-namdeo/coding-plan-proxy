"""Session log file writer with daily rotation, async enqueue, and batched flush."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
import typing
from concurrent.futures import ThreadPoolExecutor

# These are resolved via the facade module at runtime so that tests can
# patch ``dashscope_proxy.datetime`` / ``dashscope_proxy.timezone``.
# No top-level ``from datetime import …`` — the late import inside
# ``_ensure_file`` avoids circular imports because ``dashscope_proxy`` is
# fully loaded by the time any method is called.

# Session log file configuration
SESSION_LOG_DIR = os.environ.get(
    "SESSION_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session_logs"),
)
SESSION_LOG_ENABLED = os.environ.get("SESSION_LOG_ENABLED", "1") == "1"
SESSION_LOG_FLUSH_EVERY = int(os.environ.get("SESSION_LOG_FLUSH_EVERY", "32"))
SESSION_LOG_FLUSH_INTERVAL = float(os.environ.get("SESSION_LOG_FLUSH_INTERVAL", "0.25"))
SESSION_LOG_SYNC_FLUSH = os.environ.get("SESSION_LOG_SYNC_FLUSH", "0") == "1"
SESSION_LOG_QUEUE_MAX = int(os.environ.get("SESSION_LOG_QUEUE_MAX", "10000"))

_logger = logging.getLogger(__name__)
_SENTINEL = object()


class SessionLogWriter:
    """Append one JSON-line entry per request to a daily-rotating file.

    By default ``log_async`` enqueues onto a bounded ``queue.Queue`` and returns
    immediately (``put_nowait``). A single background thread drains the queue and
    flushes every ``SESSION_LOG_FLUSH_EVERY`` lines or ``SESSION_LOG_FLUSH_INTERVAL``
    seconds. Set ``SESSION_LOG_SYNC_FLUSH=1`` to restore await+flush-every-line.
    """

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self._current_date: str | None = None
        self._file: typing.TextIO | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._lines_since_flush = 0

        # Re-read env at construct so tests can monkeypatch before init.
        self._sync_flush = os.environ.get("SESSION_LOG_SYNC_FLUSH", "0") == "1"
        self._flush_every = int(os.environ.get("SESSION_LOG_FLUSH_EVERY", str(SESSION_LOG_FLUSH_EVERY)))
        self._flush_interval = float(
            os.environ.get("SESSION_LOG_FLUSH_INTERVAL", str(SESSION_LOG_FLUSH_INTERVAL))
        )
        queue_max = int(os.environ.get("SESSION_LOG_QUEUE_MAX", str(SESSION_LOG_QUEUE_MAX)))

        self._executor: ThreadPoolExecutor | None = None
        self._queue: queue.Queue | None = None
        self._writer_thread: threading.Thread | None = None

        if self._sync_flush:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-log")
        else:
            self._queue = queue.Queue(maxsize=max(1, queue_max))
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                name="session-log",
                daemon=True,
            )
            self._writer_thread.start()

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
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            path = os.path.join(self.log_dir, f"{today}.jsonl")
            new_file = open(path, "a", encoding="utf-8")
        except OSError as e:
            _logger.warning("session log file open failed: %s", e)
            return
        if self._file is not None:
            try:
                self._file.flush()
            except OSError:
                pass
            self._file.close()
        self._file = new_file
        self._current_date = today
        self._lines_since_flush = 0

    def _flush_unlocked(self) -> None:
        """Flush open file if present. Must be called with self._lock held."""
        if self._file is None:
            return
        try:
            self._file.flush()
        except OSError as e:
            _logger.warning("session log flush failed: %s", e)
        self._lines_since_flush = 0

    def _write_sync(self, entry: dict) -> None:
        """Synchronous write. Must be called with self._lock held.

        Full entry dicts are preserved (no field dropping). Flush is skipped
        unless sync-flush mode is on or the batch line count is reached.
        """
        try:
            self._ensure_file()
            if self._file is None:
                return
            self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._lines_since_flush += 1
            if self._sync_flush or self._lines_since_flush >= self._flush_every:
                self._flush_unlocked()
        except OSError as e:
            _logger.warning("session log write failed: %s", e)

    def _log_with_lock(self, entry: dict) -> None:
        """Helper to acquire lock and write (runs in thread pool / sync path)."""
        with self._lock:
            self._write_sync(entry)

    def _writer_loop(self) -> None:
        """Background consumer: drain queue, batch-write, flush on count/interval."""
        assert self._queue is not None
        last_flush = time.monotonic()
        while True:
            try:
                item = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                with self._lock:
                    self._flush_unlocked()
                last_flush = time.monotonic()
                continue

            try:
                if item is _SENTINEL:
                    # Drain any leftover entries that raced ahead of the sentinel.
                    while True:
                        try:
                            leftover = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        try:
                            if leftover is not _SENTINEL:
                                with self._lock:
                                    self._write_sync(leftover)
                        finally:
                            self._queue.task_done()
                    with self._lock:
                        self._flush_unlocked()
                    return

                with self._lock:
                    self._write_sync(item)
                    now = time.monotonic()
                    if (
                        self._lines_since_flush >= self._flush_every
                        or (now - last_flush) >= self._flush_interval
                    ):
                        self._flush_unlocked()
                        last_flush = now
            finally:
                self._queue.task_done()

    async def log_async(self, entry: dict) -> None:
        """Enqueue (default) or await disk write (``SESSION_LOG_SYNC_FLUSH=1``).

        Default path uses ``put_nowait`` and never blocks the event loop on disk
        I/O. On ``queue.Full``, logs ERROR and drops the entry.
        """
        if self._sync_flush:
            assert self._executor is not None
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self._log_with_lock, entry)
            return

        if self._closed or self._queue is None:
            return
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            _logger.error(
                "session log queue full; dropping entry request_id=%s",
                entry.get("request_id"),
            )

    def log(self, entry: dict) -> None:
        """Write one JSON line synchronously. Thread-safe via threading.Lock.

        For async contexts, use log_async() instead to avoid blocking the event loop.
        """
        if self._closed:
            return
        with self._lock:
            self._write_sync(entry)

    def close(self) -> None:
        """Stop accepting, drain queue, final flush, join writer (QueueListener shape)."""
        if self._closed:
            return
        self._closed = True

        if self._sync_flush:
            if self._executor is not None:
                self._executor.shutdown(wait=True)
                self._executor = None
            with self._lock:
                self._flush_unlocked()
                if self._file is not None:
                    self._file.close()
                    self._file = None
            return

        if self._queue is not None:
            # May block briefly if the queue is full; close is off the hot path.
            try:
                self._queue.put(_SENTINEL, timeout=5.0)
            except queue.Full:
                _logger.error("session log close: queue full; could not enqueue sentinel")
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=30.0)
            self._writer_thread = None
        with self._lock:
            self._flush_unlocked()
            if self._file is not None:
                self._file.close()
                self._file = None
