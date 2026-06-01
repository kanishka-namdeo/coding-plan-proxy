"""Session log file writer with daily rotation."""

import json
import os
import typing

# These are resolved via the facade module at runtime so that tests can
# patch ``dashscope_proxy.datetime`` / ``dashscope_proxy.timezone``.
# No top-level ``from datetime import …`` — the late import inside
# ``_ensure_file`` avoids circular imports because ``dashscope_proxy`` is
# fully loaded by the time any method is called.

# Session log file configuration
SESSION_LOG_DIR = os.environ.get("SESSION_LOG_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session_logs"))
SESSION_LOG_ENABLED = os.environ.get("SESSION_LOG_ENABLED", "1") == "1"


class SessionLogWriter:
    """Append one JSON-line entry per request to a daily-rotating file."""

    def __init__(self, log_dir: str):
        import threading
        self.log_dir = log_dir
        self._current_date: str | None = None
        self._file: typing.TextIO | None = None
        self._lock = threading.Lock()

    def _ensure_file(self) -> None:
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

    def log(self, entry: dict) -> None:
        """Write one JSON line. Thread-safe via threading.Lock."""
        self._ensure_file()
        self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
