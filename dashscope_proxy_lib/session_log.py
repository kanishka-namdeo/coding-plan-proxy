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

from dashscope_proxy_lib.config import _safe_float, _safe_int

# These are resolved via the facade module at runtime so that tests can
# patch ``dashscope_proxy.datetime`` / ``dashscope_proxy.timezone``.
# No top-level ``from datetime import …`` — the late import inside
# ``_ensure_file`` avoids circular imports because ``dashscope_proxy`` is
# fully loaded by the time any method is called.

# Session log file configuration. Numeric env vars are parsed with the shared
# safe helpers so a .env typo (e.g. SESSION_LOG_QUEUE_MAX=abc) falls back to
# the default instead of crashing the proxy at import time.
SESSION_LOG_DIR = os.environ.get(
    "SESSION_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session_logs"),
)
SESSION_LOG_ENABLED = os.environ.get("SESSION_LOG_ENABLED", "1") == "1"
SESSION_LOG_FLUSH_EVERY = _safe_int("SESSION_LOG_FLUSH_EVERY", 32)
SESSION_LOG_FLUSH_INTERVAL = _safe_float("SESSION_LOG_FLUSH_INTERVAL", 0.25)
SESSION_LOG_SYNC_FLUSH = os.environ.get("SESSION_LOG_SYNC_FLUSH", "0") == "1"
SESSION_LOG_QUEUE_MAX = _safe_int("SESSION_LOG_QUEUE_MAX", 10000)

_logger = logging.getLogger(__name__)
_SENTINEL = object()


class SessionLogWriter:
    """Append one JSON-line entry per request to a daily-rotating file.

    By default ``log_async`` enqueues onto a bounded ``queue.Queue`` and returns
    immediately (``put_nowait``). A single background thread drains the queue and
    flushes every ``SESSION_LOG_FLUSH_EVERY`` lines or ``SESSION_LOG_FLUSH_INTERVAL``
    seconds. Set ``SESSION_LOG_SYNC_FLUSH=1`` to restore await+flush-every-line.
    """

    def __init__(
        self,
        log_dir: str,
        *,
        sync_flush: bool | None = None,
        flush_every: int | None = None,
        flush_interval: float | None = None,
        queue_max: int | None = None,
    ):
        self.log_dir = log_dir
        self._current_date: str | None = None
        self._file: typing.TextIO | None = None
        self._lock = threading.Lock()
        # Serializes accept/enqueue vs close so entries cannot land after the
        # writer exits (TOCTOU between ``_closed`` check and ``put_nowait``).
        self._accept_lock = threading.Lock()
        self._closed = False
        # Raised under ``_lock`` in ``close()`` when the writer join times
        # out; the writer loop and ``_ensure_file`` check it so the still-live
        # writer stops and can never re-open/rotate the file after close.
        self._close_requested = False
        self._lines_since_flush = 0

        # Reuse the module-level constants (safely parsed at import time);
        # optional keyword overrides exist for tests and special callers.
        self._sync_flush = SESSION_LOG_SYNC_FLUSH if sync_flush is None else sync_flush
        self._flush_every = SESSION_LOG_FLUSH_EVERY if flush_every is None else flush_every
        self._flush_interval = SESSION_LOG_FLUSH_INTERVAL if flush_interval is None else flush_interval
        queue_max = SESSION_LOG_QUEUE_MAX if queue_max is None else queue_max

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
        if self._close_requested:
            # close() timed out while the writer was still alive and kept the
            # file open. Never re-open or rotate mid-shutdown: writes keep
            # going into the existing handle (if any), otherwise they are
            # skipped — a re-opened file would outlive close() and escape
            # shutdown with a ValueError.
            return
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
                    close_requested = self._close_requested
                    self._flush_unlocked()
                last_flush = time.monotonic()
                if close_requested:
                    # close() is done waiting for us: stop instead of idling.
                    _logger.info("session log writer exiting (close requested, queue idle)")
                    return
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
            with self._accept_lock:
                if self._closed:
                    return
                loop = asyncio.get_running_loop()
                fut = loop.run_in_executor(self._executor, self._log_with_lock, entry)
            await fut
            return

        with self._accept_lock:
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
        # Hold _accept_lock through the write so close() cannot slip between
        # the closed check and the write and re-open the file after shutdown.
        with self._accept_lock:
            if self._closed:
                return
            with self._lock:
                self._write_sync(entry)

    def _enqueue_close_sentinel(self) -> list:
        """Enqueue stop sentinel; on Full, pull items aside so drain still happens.

        Returns entries removed to make room (must be written by caller so close
        does not lose pending work).
        """
        assert self._queue is not None
        aside: list = []
        deadline = time.monotonic() + 5.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _logger.error("session log close: could not enqueue sentinel")
                return aside
            try:
                self._queue.put(_SENTINEL, timeout=min(remaining, 0.25))
                return aside
            except queue.Full:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    continue
                if item is _SENTINEL:
                    # Another closer already signaled; keep room for our path.
                    continue
                aside.append(item)

    def _drain_queue_unlocked(self) -> list:
        """Pull remaining queue items (non-sentinel). Caller holds no queue lock."""
        drained: list = []
        if self._queue is None:
            return drained
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not _SENTINEL:
                drained.append(item)
        return drained

    def close(self, join_timeout: float = 30.0) -> None:
        """Stop accepting, drain queue, final flush, join writer (QueueListener shape).

        ``join_timeout`` bounds how long we wait for the background writer to
        exit. On a successful join the leftovers are drained and the file is
        closed. On a join timeout the writer is still alive and owns the open
        file handle, so the file is deliberately left open: ``_close_requested``
        keeps the writer from re-opening it, and ``log()`` remains a no-op
        (``_closed`` is set before the join).
        """
        with self._accept_lock:
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

        aside: list = []
        if self._queue is not None:
            aside = self._enqueue_close_sentinel()
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=join_timeout)
            if self._writer_thread.is_alive():
                # Writer is stuck (e.g. hung filesystem flush holding
                # ``_lock``). It still owns the open file handle, so do NOT
                # close it: a later _ensure_file would re-open a new file
                # and write into a closed handle (ValueError escaping
                # shutdown). Raise the stop flag — under ``_lock`` when the
                # writer is not holding it, with a bounded fallback, since a
                # stuck writer may hold it indefinitely. After this point the
                # writer never re-opens/rotates the file, and it exits as
                # soon as it can; entries left on the queue are dropped.
                if self._lock.acquire(timeout=0.1):
                    try:
                        self._close_requested = True
                    finally:
                        self._lock.release()
                else:
                    self._close_requested = True
                _logger.error(
                    "session log close: writer join timed out after %.1fs; "
                    "%d entries still queued and will be dropped",
                    join_timeout,
                    self._queue.qsize() if self._queue is not None else 0,
                )
                return
            self._writer_thread = None
        # Post-join: write items pulled to make room for the sentinel, plus any
        # leftovers still on the queue after the writer exited.
        leftovers = aside + self._drain_queue_unlocked()
        with self._lock:
            for item in leftovers:
                self._write_sync(item)
            self._flush_unlocked()
            if self._file is not None:
                self._file.close()
                self._file = None
