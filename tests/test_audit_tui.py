"""Audit tests for the proxy TUI dashboard fixes.

Covers the durable fixes tracked in the TUI audit: stale status-key validation,
the paged log drain loop, per-provider latency merging, the failover time-gate,
the `errors="replace"` session-log reader, `mini_bar` clamping / used-vs-limit
TPM semantics, and the zero-attempt success-rate display guard.

`tui_status` is importable without a running Textual app, so its pure helpers
are tested directly. For `proxy_tui` pieces that need the full App (query_one,
widgets, the poll worker), nothing is extracted: only the testable seams are
exercised here and the rest is left to manual/integration verification.
"""
import json
import sys
import os
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_TUI_CONFIG = {
    "rpm_limit": 6000,
    "tpm_limit": 10_000_000,
    "safety_factor": 0.8,
    "max_queue_size": 50,
    "max_retries": 2,
    "base_backoff": 0.05,
}


def _flat_status(module=None):
    """Build a real flat RateLimiter.status() dict (read-only use of the lib)."""
    from dashscope_proxy_lib.rate_limiter import RateLimiter
    return RateLimiter(_TUI_CONFIG).status()


def _multi_status(module=None):
    """Build a real multi-provider status() dict (primary-only)."""
    from dashscope_proxy_lib.rate_limiter import MultiProviderRateLimiter
    return MultiProviderRateLimiter(_TUI_CONFIG).status()


def _bare_tui():
    """A ProxyTUI instance without running Textual's App init.

    Uses `object.__new__` (matching the convention in test_units.py) and
    hand-sets the attributes the code-under-test actually touches.
    """
    from proxy_tui import ProxyTUI
    tui = object.__new__(ProxyTUI)
    tui._log_seqs = {}
    tui._displayed_log_count = 0
    tui.error_count = 0
    tui._autoscroll_enabled = True
    tui._logs_paused = False
    # session-log reader state
    tui._session_log_offset = 0
    tui._session_log_inode = None
    tui._session_log_path = None
    tui._session_log_tail = deque(maxlen=200)
    tui._session_log_partial = ""
    # latency trackers
    from proxy_tui import LatencyTracker
    tui.latency_tracker = LatencyTracker()
    tui._latency_trackers = {"primary": tui.latency_tracker}
    return tui


class FakeLogHandler:
    """Forward-paging stand-in for TUILogHandler.get_logs.

    `get_logs(limit, from_seq)` returns the NEXT forward slice of up to
    `limit` entries at/after `from_seq` — the pagination contract the drain
    loop relies on. Records every call for assertions.
    """

    def __init__(self, n=0):
        self.entries = [{"seq": i, "level": "INFO", "message": f"m{i}",
                        "timestamp": ""} for i in range(n)]
        self.calls = []

    def get_logs(self, limit=100, from_seq=0):
        self.calls.append((limit, from_seq))
        return self.entries[from_seq:from_seq + limit]


class FakeLogWidget:
    def __init__(self, max_lines=0, level_filter="ALL", text_filter="", time_range="ALL"):
        self.max_lines = max_lines
        self.level_filter = level_filter
        self.text_filter = text_filter
        self.time_range = time_range
        self.lines = []

    def write_line(self, line):
        self.lines.append(line)

    def clear(self):
        self.lines = []

    def auto_scroll_set(self, value):
        self.auto_scroll = value

    # The code reads these off a Select/Input, emulate as attributes.
    @property
    def value(self):
        return None


# ---------------------------------------------------------------------------
# Item 1: _REQUIRED_STATUS_KEYS is a subset of both the flat and the
# multi-provider (primary) real status shapes.
# ---------------------------------------------------------------------------

class TestRequiredStatusKeys:
    def test_no_stale_keys_present(self):
        from proxy_tui import ProxyTUI
        required = ProxyTUI._REQUIRED_STATUS_KEYS
        stale = {
            "requests_5h", "requests_5h_limit",
            "requests_week", "requests_week_limit",
            "requests_month", "requests_month_limit",
        }
        assert not (required & stale), f"stale keys still required: {required & stale}"

    def test_subset_of_flat_status(self, dashscope_module):
        from proxy_tui import ProxyTUI
        flat = _flat_status(dashscope_module)
        missing = ProxyTUI._REQUIRED_STATUS_KEYS - flat.keys()
        assert not missing, f"required keys missing from flat status: {missing}"

    def test_subset_of_multi_provider_primary(self, dashscope_module):
        from proxy_tui import ProxyTUI
        multi = _multi_status(dashscope_module)
        primary = multi["primary"]
        missing = ProxyTUI._REQUIRED_STATUS_KEYS - primary.keys()
        assert not missing, f"required keys missing from primary entry: {missing}"

    def test_validate_status_passes_flat(self, dashscope_module):
        tui = _bare_tui()
        assert tui._validate_status(_flat_status(dashscope_module)) is True

    def test_validate_status_passes_multi_primary(self, dashscope_module):
        tui = _bare_tui()
        multi = _multi_status(dashscope_module)
        assert tui._validate_status(multi["primary"]) is True


# ---------------------------------------------------------------------------
# Item 2: paged drain loop
# ---------------------------------------------------------------------------

class TestDrainLogBuffer:
    def test_full_backlog_drains_until_cap(self):
        tui = _bare_tui()
        handler = FakeLogHandler(n=2000)
        tui.log_handler = handler
        written_seqs = []

        new_seq, written = tui._drain_log_buffer(0, cap=0, write_entry=lambda e: written_seqs.append(e["seq"]) or True)

        # cap=0 means unbounded: the whole 2000-entry backlog drains.
        assert written == 2000
        assert len(written_seqs) == 2000
        # from_seq advanced to one past the last appended entry.
        assert new_seq == 2000
        # Every page is a full PAGE until the final empty page.
        from proxy_tui import LOG_DRAIN_PAGE_SIZE
        assert all(limit == LOG_DRAIN_PAGE_SIZE for limit, _ in handler.calls)

    def test_drain_stops_at_widget_cap(self):
        tui = _bare_tui()
        handler = FakeLogHandler(n=2000)
        tui.log_handler = handler
        seen = []

        new_seq, written = tui._drain_log_buffer(0, cap=1000, write_entry=lambda e: seen.append(e["seq"]) or True)

        assert written == 1000
        assert len(seen) == 1000
        # Cursor is one past the highest entry consumed this drain.
        assert new_seq > 0

    def test_short_page_stops_the_loop(self):
        tui = _bare_tui()
        handler = FakeLogHandler(n=1200)
        tui.log_handler = handler

        new_seq, written = tui._drain_log_buffer(0, cap=0, write_entry=lambda e: True)

        assert written == 1200
        assert new_seq == 1200
        # The final page served 200 (< PAGE) entries -> loop stopped there.
        from proxy_tui import LOG_DRAIN_PAGE_SIZE
        last_limit, last_from = handler.calls[-1]
        assert last_from == 1000
        assert len(handler.entries[last_from:last_from + last_limit]) < LOG_DRAIN_PAGE_SIZE

    def test_incremental_poll_appends_new_entries(self):
        """Small incremental batch: all new entries are appended, none skipped."""
        tui = _bare_tui()
        handler = FakeLogHandler(n=3)
        tui.log_handler = handler

        # First poll drains 0..2.
        new_seq, written = tui._drain_log_buffer(0, cap=0, write_entry=lambda e: True)
        assert written == 3 and new_seq == 3
        # Simulate three more entries arriving.
        for i in range(3):
            handler.entries.append({"seq": 3 + i, "level": "INFO", "message": f"n{i}", "timestamp": ""})
        new_seq2, written2 = tui._drain_log_buffer(3, cap=0, write_entry=lambda e: True)
        assert written2 == 3 and new_seq2 == 6


# ---------------------------------------------------------------------------
# Item 4: per-provider latency merge -> union math
# ---------------------------------------------------------------------------

class TestPerProviderLatencyMerge:
    def test_two_disjoint_windows_union_no_duplicates(self):
        tui = _bare_tui()
        from proxy_tui import LatencyTracker
        tui._latency_trackers = {
            "primary": LatencyTracker(),
            "secondary": LatencyTracker(),
        }
        # Disjoint 100-sample windows per provider.
        raw = {
            "primary": {"recent_latencies": [float(i) for i in range(100)]},
            "secondary": {"recent_latencies": [float(100 + i) for i in range(100)]},
        }
        tui._feed_latency_trackers(raw)

        # Union over all trackers: exactly 200 distinct samples, no duplicates.
        union = tui._latency_union_values()
        assert len(union) == 200
        assert len(set(union)) == 200
        # Stats computed over the union.
        stats = tui._latency_union_stats()
        assert stats["count"] == 200

    def test_single_provider_path_unchanged(self):
        tui = _bare_tui()
        flat = {
            "primary": None,  # not a dict -> is_multi_provider is False
            "recent_latencies": [float(i) for i in range(100)],
        }
        tui._feed_latency_trackers(flat)
        # Only the "primary" tracker exists and holds the 100 samples.
        assert list(tui._latency_trackers.keys()) == ["primary"]
        assert len(tui._latency_union_values()) == 100
        assert tui._latency_union_stats()["count"] == 100

    def test_equal_latency_samples_from_providers_are_preserved(self):
        tui = _bare_tui()
        from proxy_tui import LatencyTracker
        tui._latency_trackers = {"primary": LatencyTracker(), "secondary": LatencyTracker()}
        tui._latency_trackers["primary"].latencies = [100.0, 100.0]
        tui._latency_trackers["secondary"].latencies = [100.0]
        assert tui._latency_union_values() == [100.0, 100.0, 100.0]

    def test_multi_provider_does_not_double_count_on_repeat_poll(self):
        """Feeding the same two-provider snapshot twice must not re-add samples.

        (The old single-tracker merge double-counted a concatenated window;
        per-provider merge_snapshot only appends the true new suffix.)
        """
        tui = _bare_tui()
        from proxy_tui import LatencyTracker
        tui._latency_trackers = {"primary": LatencyTracker(), "secondary": LatencyTracker()}
        raw = {
            "primary": {"recent_latencies": [float(i) for i in range(100)]},
            "secondary": {"recent_latencies": [float(100 + i) for i in range(100)]},
        }
        tui._feed_latency_trackers(raw)
        first = len(tui._latency_union_values())
        # Re-feed the identical snapshot: merge overlap means nothing new added.
        tui._feed_latency_trackers(raw)
        second = len(tui._latency_union_values())
        assert first == second == 200


# ---------------------------------------------------------------------------
# Item 5: failover time-gate
# ---------------------------------------------------------------------------

class TestFailoverTimeGate:
    def _iso(self, dt):
        return dt.isoformat()

    def test_four_minutes_ago_is_recent(self):
        from tui_status import is_failover_recent
        now = datetime.now(timezone.utc)
        assert is_failover_recent(self._iso(now - timedelta(minutes=4)), now) is True

    def test_six_minutes_ago_is_not_recent(self):
        from tui_status import is_failover_recent
        now = datetime.now(timezone.utc)
        assert is_failover_recent(self._iso(now - timedelta(minutes=6)), now) is False

    def test_z_suffixed_iso_recent(self):
        from tui_status import is_failover_recent
        now = datetime.now(timezone.utc)
        z_stamp = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        assert is_failover_recent(z_stamp, now) is True

    def test_future_timestamp_is_not_recent(self):
        from tui_status import is_failover_recent
        now = datetime.now(timezone.utc)
        assert is_failover_recent(self._iso(now + timedelta(minutes=2)), now) is False

    def test_unparseable_timestamp_is_not_recent(self):
        from tui_status import is_failover_recent
        now = datetime.now(timezone.utc)
        assert is_failover_recent("not-a-time", now) is False
        assert is_failover_recent(None, now) is False

    def test_alert_badge_integration_recent_failover(self, tmp_path, monkeypatch):
        """End-to-end: a recent (4-min) failover entry drives the badge."""
        tui = _bare_tui()
        tui.proxy_app = _FakeProxyApp()
        tui.rate_limiter = None

        now = datetime.now(timezone.utc)
        recent = {"attempted_providers": ["primary", "secondary"],
                  "timestamp_utc": (now - timedelta(minutes=4)).isoformat()}
        tui._read_session_log_entries = lambda: [recent]

        badge = _FakeBadge()
        tui._widgets = {"#alert-badge": badge}
        tui.query_one = lambda sel, _t=None: tui._widgets.get(sel)

        status = {"primary": _flat_status(None), "shared_limits": False}
        tui._update_alert_badge(status)
        assert "Failover" in badge.text

    def test_alert_badge_stale_failover_suppressed(self, tmp_path, monkeypatch):
        """A 6-min-old failover is time-gated out of the badge."""
        tui = _bare_tui()
        tui.proxy_app = _FakeProxyApp()
        tui.rate_limiter = None

        now = datetime.now(timezone.utc)
        stale = {"attempted_providers": ["primary", "secondary"],
                 "timestamp_utc": (now - timedelta(minutes=6)).isoformat()}
        tui._read_session_log_entries = lambda: [stale]

        badge = _FakeBadge()
        tui._widgets = {"#alert-badge": badge}
        tui.query_one = lambda sel, _t=None: tui._widgets.get(sel)

        status = {"primary": _flat_status(None), "shared_limits": False}
        tui._update_alert_badge(status)
        assert "Failover" not in badge.text


class _FakeBadge:
    def __init__(self):
        self.text = ""
        self.classes = set()

    def update(self, text):
        self.text = text

    def add_class(self, c):
        self.classes.add(c)

    def remove_class(self, c):
        self.classes.discard(c)


class _FakeProxyApp:
    def get(self, key, default=None):
        return None


# ---------------------------------------------------------------------------
# Item 6: session-log reader with errors="replace"
# ---------------------------------------------------------------------------

class TestSessionLogReaderReplaceErrors:
    def test_invalid_utf8_byte_does_not_raise_and_advances(self, tmp_path, monkeypatch):
        from proxy_tui import ProxyTUI
        monkeypatch.chdir(tmp_path)
        log_dir = tmp_path / "session_logs"
        log_dir.mkdir()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = log_dir / f"{today}.jsonl"

        tui = _bare_tui()

        # A valid JSONL line followed by a line containing a bad UTF-8 byte.
        valid = json.dumps({"request_id": "ok", "attempted_providers": ["primary"]})
        bad = b'\xff\xfe' + b'{"request_id":"garbled"}' + b'\n'
        path.write_bytes(valid.encode("utf-8") + b"\n" + bad)

        # Must not raise despite the undecodable byte.
        entries = tui._read_session_log_entries()
        ids = [e.get("request_id") for e in entries]
        assert "ok" in ids
        # The offset advanced past the whole file (did not freeze at the bad byte).
        assert tui._session_log_offset == path.stat().st_size

    def test_valid_entries_still_parse_after_bad_byte(self, tmp_path, monkeypatch):
        from proxy_tui import ProxyTUI
        monkeypatch.chdir(tmp_path)
        log_dir = tmp_path / "session_logs"
        log_dir.mkdir()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = log_dir / f"{today}.jsonl"

        tui = _bare_tui()
        valid_a = json.dumps({"request_id": "a", "attempted_providers": ["primary"]})
        path.write_bytes((valid_a + "\n").encode("utf-8") + b"\x80\n")

        first = [e["request_id"] for e in tui._read_session_log_entries()]
        assert "a" in first

        # A later valid entry is still picked up on the next poll.
        valid_b = json.dumps({"request_id": "b", "attempted_providers": ["secondary", "primary"]})
        with path.open("ab") as f:
            f.write(valid_b.encode("utf-8") + b"\n")
        second = [e["request_id"] for e in tui._read_session_log_entries()]
        assert "b" in second


# ---------------------------------------------------------------------------
# Item 10: mini_bar clamp + provider_grid_rows TPM used/limit
# ---------------------------------------------------------------------------

class TestMiniBarClamp:
    def test_over_quota_clamps_percent_label(self):
        from tui_status import mini_bar
        # current > maximum -> percent label clamped to the upper bound (999
        # cap; 200/100 == 200% shows the clamped fill logic, never >100% bar).
        cell = mini_bar(200, 100)
        # The bar is full (8 filled) and the percent is not negative; the label
        # for >100% is bounded — assert no bracket markup and full fill.
        assert cell.count("█") == 8
        assert "[" not in cell

    def test_negative_current_clamps_to_zero(self):
        from tui_status import mini_bar
        cell = mini_bar(-12, 100)
        assert "0%" in cell
        assert cell.count("░") == 8
        assert "█" not in cell

    def test_full_quota_renders_full_bar(self):
        from tui_status import mini_bar
        cell = mini_bar(100, 100)
        assert "100%" in cell
        assert cell.count("█") == 8

    def test_grid_rows_tpm_cell_is_used_over_limit(self):
        from tui_status import provider_grid_rows
        # available=100_000 / limit=1_000_000 -> used = 900_000 -> 90% bar.
        status = {"secondary": {
            "rpm_current": 3000, "rpm_limit": 6000,
            "tpm_limit": 1_000_000, "tpm_available": 100_000,
            "total_forwarded": 2500, "total_429s": 3,
            "circuit_open": False, "circuit_failure_count": 0,
        }}
        row = provider_grid_rows(status)[0]
        assert "90%" in row["tpm"]
        # Both cells now mean "used/limit": RPM 3000/6000 = 50%.
        assert "50%" in row["rpm"]

    def test_grid_rows_tpm_cell_full_quota_when_available_zero(self):
        from tui_status import provider_grid_rows
        status = {"primary": {
            "rpm_current": 100, "rpm_limit": 6000,
            "tpm_limit": 1_000_000, "tpm_available": 0,
            "total_forwarded": 5, "total_429s": 0,
            "circuit_open": False, "circuit_failure_count": 0,
        }}
        row = provider_grid_rows(status)[0]
        assert "100%" in row["tpm"]
        assert row["tpm"].count("█") == 8


# ---------------------------------------------------------------------------
# Item 11: success-rate display guard
# ---------------------------------------------------------------------------

class TestSuccessRateDisplay:
    def test_zero_attempts_returns_na(self):
        from tui_status import success_rate_display
        assert success_rate_display(0, 0, 0) == "n/a"

    def test_nonzero_attempts_returns_percent(self):
        from tui_status import success_rate_display
        # 9 forwarded, 1 429, 0 rejected -> 90.0%
        assert success_rate_display(9, 1, 0) == "90.0%"

    def test_overview_stats_still_zero_for_api_stability(self):
        # overview_request_stats keeps returning 0.0 (not "n/a") so the numeric
        # API is stable; the UI display guard is what surfaces "n/a".
        from tui_status import overview_request_stats
        assert overview_request_stats({})["success_rate"] == 0.0
