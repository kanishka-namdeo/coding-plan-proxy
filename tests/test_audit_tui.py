"""Audit tests for the proxy TUI dashboard fixes.

Covers the durable fixes tracked in the TUI audit: stale status-key validation,
the paged log drain loop, per-provider latency merging, the failover time-gate,
the `errors="replace"` session-log reader, `mini_bar` clamping / used-vs-limit
TPM semantics, the zero-attempt success-rate display guard, and the polling
churn work (tab-gated dispatch, preserved table cursor/scroll, the
dirty-gated config rebuild, the once-per-poll session-log read), and the
`capture_screenshots` status-envelope unwrap (`TestStatusEnvelopeUnwrap` —
`/v1/proxy/status` nests the limiter status under `rate_limits`, one level
deeper than the flat shape the TUI consumes).

`tui_status` is importable without a running Textual app, so its pure helpers
are tested directly. For `proxy_tui` pieces that need the full App (query_one,
widgets, the poll worker), nothing is extracted: only the testable seams are
exercised here and the rest is left to manual/integration verification.
"""
import asyncio
import contextlib
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
    import threading

    from proxy_tui import ProxyTUI
    tui = object.__new__(ProxyTUI)
    tui._log_seqs = {}
    tui._displayed_log_count = 0
    tui.error_count = 0
    tui._autoscroll_enabled = True
    tui._logs_paused = False
    tui._error_scanned_seq = 0
    # Tab gating + immediate-refresh state
    tui._active_tab = "tab-overview"
    tui._ui_ready = False
    tui._refresh_wake = threading.Event()
    tui._config_dirty = False
    # session-log reader state
    tui._session_log_offset = 0
    tui._session_log_inode = None
    tui._session_log_path = None
    tui._session_log_tail = deque(maxlen=200)
    tui._session_log_partial = ""
    # once-per-poll session-log snapshot state
    tui._poll_generation = 0
    tui._generation_lock = threading.Lock()
    tui._session_log_gen = -1
    tui._session_log_cache = []
    # latency trackers
    from proxy_tui import LatencyTracker
    tui.latency_tracker = LatencyTracker()
    tui._latency_trackers = {"primary": tui.latency_tracker}
    # provider-grid / detail-pane follow state
    tui._grid_row_keys = []
    tui._detail_provider_key = "primary"
    tui._suppress_detail_follow = False
    tui._last_raw_status = None
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
        tui._widgets = {"#alert-badge": badge,
                        # `_alert_badge_width` reads the always-visible
                        # sibling; a zero-width double means "not laid out",
                        # which is the same branch a hidden Overview tab takes.
                        "#status-indicator": _FakeBadge(width=0)}
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
        tui._widgets = {"#alert-badge": badge,
                        "#status-indicator": _FakeBadge(width=0)}
        tui.query_one = lambda sel, _t=None: tui._widgets.get(sel)

        status = {"primary": _flat_status(None), "shared_limits": False}
        tui._update_alert_badge(status)
        assert "Failover" not in badge.text


class _FakeBadge:
    def __init__(self, width=0):
        self.text = ""
        self.classes = set()
        self.content_size = type("S", (), {"width": width})()

    def update(self, text):
        self.text = text

    def add_class(self, c):
        self.classes.add(c)

    def remove_class(self, c):
        self.classes.discard(c)

    def set_class(self, add, c):
        if add:
            self.classes.add(c)
        else:
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
        from tui_status import GRID_BAR_WIDTH, mini_bar
        # current > maximum -> percent label clamped to the upper bound (999
        # cap; 200/100 == 200% shows the clamped fill logic, never >100% bar).
        cell = mini_bar(200, 100)
        # The bar is full and the percent is not negative; the label
        # for >100% is bounded — assert no bracket markup and full fill.
        assert cell.count("█") == GRID_BAR_WIDTH
        assert "[" not in cell

    def test_negative_current_clamps_to_zero(self):
        from tui_status import GRID_BAR_WIDTH, mini_bar
        cell = mini_bar(-12, 100)
        assert "0%" in cell
        assert cell.count("░") == GRID_BAR_WIDTH
        assert "█" not in cell

    def test_full_quota_renders_full_bar(self):
        from tui_status import GRID_BAR_WIDTH, mini_bar
        cell = mini_bar(100, 100)
        assert "100%" in cell
        assert cell.count("█") == GRID_BAR_WIDTH

    def test_default_bar_width_matches_grid_bar_width(self):
        # mini_bar has exactly one production consumer (provider_grid_rows), so
        # its default and the grid's bar width must be the same value.
        from tui_status import GRID_BAR_WIDTH, mini_bar
        assert mini_bar(50, 100) == mini_bar(50, 100, GRID_BAR_WIDTH)

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
        from tui_status import GRID_BAR_WIDTH, provider_grid_rows
        status = {"primary": {
            "rpm_current": 100, "rpm_limit": 6000,
            "tpm_limit": 1_000_000, "tpm_available": 0,
            "total_forwarded": 5, "total_429s": 0,
            "circuit_open": False, "circuit_failure_count": 0,
        }}
        row = provider_grid_rows(status)[0]
        assert "100%" in row["tpm"]
        # Grid cells use GRID_BAR_WIDTH (narrower than mini_bar's default) so
        # the Overview grid's bar columns fit the 50%-wide metrics panel.
        assert row["tpm"].count("█") == GRID_BAR_WIDTH

    def test_grid_bar_cell_fits_its_column_at_worst_case(self):
        from tui_status import GRID_BAR_CELL_MAX, mini_bar
        # The RPM/TPM columns in proxy_tui.py are sized from GRID_BAR_CELL_MAX:
        # over-quota mark + full bar + space + the 999 percent clamp + "%".
        worst = mini_bar(999, 1)
        assert len(worst) == GRID_BAR_CELL_MAX
        assert worst.startswith("!"), "over-quota must be marked, not silently full"
        assert len(mini_bar(42, 100)) <= GRID_BAR_CELL_MAX

    def test_over_quota_is_distinguishable_from_exactly_at_limit(self):
        from tui_status import LOAD_OVER, OVER_QUOTA_MARK, load_level, mini_bar
        at_limit = mini_bar(100, 100)
        over = mini_bar(250, 100)
        # Both saturate to a full bar — that is deliberate, the fill cannot
        # represent "more than full". The mark and the level are what separate
        # them, so the cell must differ in both.
        assert at_limit.count("█") == over.count("█") == 5
        assert not at_limit.startswith(OVER_QUOTA_MARK)
        assert over.startswith(OVER_QUOTA_MARK)
        assert load_level(100, 100) != LOAD_OVER
        assert load_level(250, 100) == LOAD_OVER

    def test_percent_clamp_stays_bounded_so_cells_cannot_grow(self):
        from tui_status import GRID_BAR_CELL_MAX, mini_bar
        # A pathological ratio must not produce an arbitrarily long cell; that
        # would break the RPM/TPM column widths.
        for current, maximum in ((10**9, 1), (10**18, 3), (10**30, 7)):
            assert len(mini_bar(current, maximum)) <= GRID_BAR_CELL_MAX


class TestLoadLevel:
    """`load_level` is the semantic signal behind the grid's bar styling.

    `tui_status` returns the level as plain data (no Rich/Textual imports);
    `proxy_tui` turns it into a style. The 90% breakpoint deliberately matches
    `_check_quota_thresholds`, so the bar and the alert badge can never
    disagree about what counts as a warning.
    """

    def test_breakpoints(self):
        from tui_status import (
            LOAD_HIGH, LOAD_HIGH_THRESHOLD, LOAD_OK, LOAD_OVER,
            LOAD_WARM, LOAD_WARM_THRESHOLD, load_level,
        )
        assert (LOAD_WARM_THRESHOLD, LOAD_HIGH_THRESHOLD) == (70, 90)
        assert load_level(0, 100) == LOAD_OK
        assert load_level(69, 100) == LOAD_OK
        assert load_level(70, 100) == LOAD_WARM
        assert load_level(89, 100) == LOAD_WARM
        assert load_level(90, 100) == LOAD_HIGH
        assert load_level(100, 100) == LOAD_HIGH
        assert load_level(101, 100) == LOAD_OVER

    def test_degenerate_inputs_are_ok(self):
        from tui_status import LOAD_OK, load_level
        assert load_level(5, 0) == LOAD_OK
        assert load_level(-12, 100) == LOAD_OK

    def test_high_threshold_matches_the_alert_badge_threshold(self):
        # The badge warns at >=90% via `_check_quota_thresholds`. The bar must
        # not call the same value "warm" or "ok", or the two displays disagree.
        from proxy_tui import ProxyTUI
        from tui_status import LOAD_HIGH_THRESHOLD

        tui = ProxyTUI.__new__(ProxyTUI)
        at_89 = {"rpm_current": 890, "rpm_limit": 1000, "tpm_limit": 1000,
                 "tpm_available": 1000, "tpm_reserved": 0}
        at_90 = dict(at_89, rpm_current=900)
        assert tui._check_quota_thresholds(at_89) == []       # badge silent
        assert "RPM" in tui._check_quota_thresholds(at_90)    # badge warns
        assert LOAD_HIGH_THRESHOLD == 90

    def test_grid_rows_report_a_level_per_bar(self):
        from tui_status import LOAD_HIGH, provider_grid_rows
        status = {"primary": _provider(rpm_current=9000, rpm_limit=10000,
                                       tpm_available=100_000)}
        row = provider_grid_rows(status)[0]
        assert row["rpm_level"] == LOAD_HIGH
        assert row["tpm_level"] in ("ok", "warm", "high", "over")

    def test_idle_row_reports_ok_levels(self):
        from tui_status import LOAD_OK, provider_grid_rows
        row = provider_grid_rows({"primary": _provider()})[0]
        assert row["idle"] is True
        assert row["rpm_level"] == row["tpm_level"] == LOAD_OK


def _provider(**overrides):
    base = {
        "rps_limit": 5, "rpm_limit": 6000, "rpm_current": 0,
        "tpm_limit": 1_000_000, "tpm_available": 1_000_000, "tpm_reserved": 0,
        "total_forwarded": 0, "total_429s": 0, "total_rejected": 0,
        "total_tokens_consumed": 0, "circuit_open": False, "circuit_failure_count": 0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Item 12: Overview provider-grid column widths vs the cells tui_status emits
# ---------------------------------------------------------------------------

class TestOverviewStatStrip:
    """The Overview aggregate strip replaced a 9-row DataTable.

    A table cannot silently drop rows the way a joined string can, so the
    information content is pinned here: all nine former `#request-stats` rows
    must still be present, and the strip must not report a success rate for an
    idle proxy.
    """

    _LABELS = [
        "Forwarded", "429s", "Rejected", "Success", "Tokens",
        "Pending", "Drops", "Queue p50/p95/p99", "Avg Req/Resp",
    ]

    def _render(self, status):
        from proxy_tui import _format_stat_strip
        from tui_status import overview_request_stats
        multi = status if "primary" in status else status
        primary = status["primary"] if "primary" in status else status
        return _format_stat_strip(overview_request_stats(status), multi, primary)

    def test_all_nine_stats_survive_the_collapse(self):
        rendered = self._render(_status_with_totals())
        for label in self._LABELS:
            assert label in rendered, f"stat strip lost {label!r}: {rendered!r}"

    def test_two_lines_when_fits(self):
        assert len(self._render(_status_with_totals()).split("\n")) == 2

    def test_idle_reports_na_not_zero_percent(self):
        status = _status_with_totals()
        status["total_forwarded"] = 0
        status["total_429s"] = 0
        status["total_rejected"] = 0
        assert "Success n/a" in self._render(status)


class TestErrorBannerVisibility:
    """`#error-banner` must show when it has content and collapse when empty.

    Guards the Textual trap this rule was written for: `:empty` matches a
    widget with no CHILD NODES, which is permanently true for a Static, so a
    `#error-banner:empty { display: none }` rule hides the banner FOREVER —
    including while it is reporting errors. Visibility is a `.hidden` class.
    """

    async def test_banner_visible_with_content_and_hidden_when_empty(self):
        from proxy_tui import ProxyTUI
        from textual.widgets import Static

        class _LogHandler:
            def get_logs(self, limit=50, from_seq=0):
                return []

            def clear(self):
                pass

        tui = ProxyTUI(rate_limiter=_MultiProviderLimiter(),
                       tui_log_handler=_LogHandler(), proxy_app={})
        async with tui.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            banner = tui.query_one("#error-banner", Static)

            banner.update("something went wrong")
            banner.set_class(False, "hidden")
            await pilot.pause()
            assert banner.display and banner.size.width > 0, (
                "banner with content must be visible; a `:empty` rule would "
                "hide it permanently because a Static has no child nodes"
            )

            banner.update("")
            banner.set_class(True, "hidden")
            await pilot.pause()
            assert not banner.display and banner.size.height == 0, (
                "empty banner must collapse, not leave a blank band"
            )


def _status_with_totals() -> dict:
    """A minimal MultiProvider-shaped status for the stat strip."""
    from tui_status import PROVIDER_KEYS
    provider = {
        "rps_limit": 5, "rpm_limit": 6000, "rpm_current": 1,
        "tpm_limit": 1_000_000, "tpm_available": 900_000, "tpm_reserved": 0,
        "max_queue_size": 500, "total_forwarded": 9, "total_429s": 1,
        "total_rejected": 0, "total_tokens_consumed": 1234,
        "queue_drops": 2, "queue_p50_ms": 5, "queue_p95_ms": 9, "queue_p99_ms": 11,
        "pending_requests": 3, "recent_latencies": [], "circuit_open": False,
        "circuit_failure_count": 0, "total_request_bytes": 1000,
        "total_response_bytes": 2000, "model_usage": {}, "uptime_seconds": 42,
    }
    status = {key: dict(provider) for key in PROVIDER_KEYS}
    status.update({
        "total_forwarded": 9, "total_429s": 1, "total_rejected": 0,
        "total_tokens_consumed": 1234, "max_queue_size": 500,
        "pending_requests": 3, "total_request_bytes": 1000,
        "total_response_bytes": 2000, "model_usage": {}, "uptime_seconds": 42,
    })
    return status


class _MultiProviderLimiter:
    def status(self) -> dict:
        return _status_with_totals()


# ---------------------------------------------------------------------------

class TestPanelVisibilityToggling:
    """No Static may be hidden by a tcss `:empty` rule.

    Textual's `:empty` means "has no child nodes", which is permanently TRUE
    for a Static regardless of its text. Three panels (failover events, poll
    errors, latency histogram) shipped with `.panel:empty { display: none }`
    and were therefore invisible FOREVER — including while holding text, which
    made the show/hide machinery around them dead code.

    Visibility is now a toggled `.hidden` class. This locks all six
    content/empty combinations across the three panels, because the failure
    mode is invisible: a panel can be `display: none` while full of text and
    nothing else in the suite notices.
    """

    PANELS = ("#failover-events-panel", "#poll-error-banner",
              "#latency-histogram-panel")

    async def _app(self):
        """Build a ProxyTUI wired for a headless run.

        Note: all three panels are ALSO refreshed by the app's background
        poller. Tests must call `tui.workers.cancel_all()` inside `run_test`
        before driving `_show_poll_error` and friends directly, or the poller
        recovers and clears the very banner the test just set.
        """
        from proxy_tui import ProxyTUI
        from textual.widgets import Static

        class _Limiter:
            def status(self) -> dict:
                return _status_with_totals()

        class _LogHandler:
            def get_logs(self, limit=50, from_seq=0):
                return []

            def clear(self):
                pass

        return ProxyTUI(rate_limiter=_Limiter(), tui_log_handler=_LogHandler(),
                        proxy_app={}), Static

    async def test_poll_error_show_and_clear_round_trip(self):
        tui, Static = await self._app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            banner = tui.query_one("#poll-error-banner", Static)

            tui._poll_error_count = 3
            tui._show_poll_error()
            await pilot.pause()
            assert banner.display, "poll-error banner must show when there is an error"
            assert "Poll error" in str(banner.content)

            tui._clear_poll_error()
            await pilot.pause()
            assert not banner.display and banner.size.height == 0, (
                "poll-error banner must collapse once polling recovers"
            )

    async def test_histogram_shows_with_latencies_and_collapses_without(self):
        tui, Static = await self._app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-metrics")
            await pilot.pause()
            panel = tui.query_one("#latency-histogram-panel", Static)

            tui._latency_trackers["primary"].merge_snapshot([10.0, 90.0, 300.0, 1500.0])
            tui._update_latency_histogram()
            await pilot.pause()
            assert panel.display, "histogram must show once latencies exist"
            assert panel.size.height > 0
            assert "p50" in str(panel.content)

            # A genuinely empty tracker — merge_snapshot([]) merges nothing and
            # would leave the previous values in place.
            from proxy_tui import LatencyTracker
            tui._latency_trackers = {"primary": LatencyTracker()}
            tui._update_latency_histogram()
            await pilot.pause()
            assert not panel.display and panel.size.height == 0, (
                "histogram must collapse when there are no latencies"
            )

    async def test_failover_panel_shows_with_events_and_collapses_without(self, tmp_path, monkeypatch):
        tui, Static = await self._app()
        monkeypatch.chdir(tmp_path)
        log_dir = tmp_path / "session_logs"
        log_dir.mkdir()
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        (log_dir / f"{today}.jsonl").write_text(
            '{"request_id": "abcdef1234", "attempted_providers": ["primary", "secondary"],'
            ' "model": "qwen3-coder-plus", "timestamp_utc": "%s"}\n'
            % datetime.now(timezone.utc).isoformat(),
            encoding="utf-8",
        )

        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-metrics")
            await pilot.pause()
            panel = tui.query_one("#failover-events-panel", Static)

            tui._update_failover_panel()
            await pilot.pause()
            assert panel.display, "failover panel must show when a failover is recorded"
            assert "qwen3-coder-plus" in str(panel.content)

            (log_dir / f"{today}.jsonl").write_text("", encoding="utf-8")
            tui._session_log_path = None
            # The session log is read at most once per poll generation, so a
            # new generation stands in for "the poller ticked" here. Without
            # this the panel would legitimately keep showing the cached
            # entries until the next tick.
            tui._bump_poll_generation()
            tui._update_failover_panel()
            await pilot.pause()
            assert not panel.display and panel.size.height == 0, (
                "failover panel must collapse when there are no failover events"
            )

    async def test_panels_start_hidden_before_any_content_exists(self):
        """An empty panel must not render as an empty box on the first frame."""
        tui, Static = await self._app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-metrics")
            await pilot.pause()
            for selector in self.PANELS:
                widget = tui.query_one(selector, Static)
                assert not widget.display, (
                    f"{selector} is visible at mount time with no content"
                )

    async def test_no_panel_is_ever_hidden_while_holding_text(self):
        """The exact original bug: `display: none` WITH content."""
        tui, Static = await self._app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-metrics")
            await pilot.pause()
            for selector in self.PANELS:
                widget = tui.query_one(selector, Static)
                widget.update("SENTINEL TEXT")
                widget.set_class(False, "hidden")
                await pilot.pause()
                assert widget.display, (
                    f"{selector} is display:none while holding text — a tcss "
                    f"`:empty` rule is permanently true for a Static"
                )
                assert widget.size.height > 0


class TestProviderGridColumnBudget:
    """Lock the coupling between the grid's column widths and its cell strings.

    Overview is a single full-width column (`#overview-panel`, `width: 1fr`),
    so the grid gets the whole terminal width — 116 cells at 120 columns, 76
    at 80. The grid still runs `cell_padding = 0`, which means there is NO
    implicit gap between columns: a column sized to exactly fit its longest
    possible cell makes adjacent cells run together and read as one token
    ("Agnes Imageok", "111.1K63"). Every column must therefore be at least one
    cell wider than its worst-case content.

    The test asserts the rule at 120 AND at 80 columns, because "fits in the
    panel" and "no two columns touch" are independent constraints.
    """

    @staticmethod
    def _worst_case_cells():
        from tui_status import (
            CIRCUIT_OK, CIRCUIT_OPEN, CIRCUIT_WARN, GRID_BAR_CELL_MAX,
            GRID_BAR_WIDTH, PROVIDER_LABELS, _compact_number, mini_bar,
        )
        return {
            # longest provider label + 0 (must have slack of its own)
            "Provider": max(len(v) for v in PROVIDER_LABELS.values()),
            # header "Circuit" is longer than any state word
            "Circuit": max(len("Circuit"), len(CIRCUIT_OK),
                           len(CIRCUIT_WARN), len(CIRCUIT_OPEN)),
            "RPM": len(mini_bar(999, 1, GRID_BAR_WIDTH)),
            "TPM": len(mini_bar(999, 1, GRID_BAR_WIDTH)),
            "Fwd": len(_compact_number(999_999_999)),
            "429": len(_compact_number(999_999)),
        }
        # The RPM/TPM worst case is the OVER-QUOTA cell, not the plain one:
        # the over-quota mark makes it one char longer than a 999% bar.
        worst_case["RPM"] = GRID_BAR_CELL_MAX
        worst_case["TPM"] = GRID_BAR_CELL_MAX

    @pytest.mark.parametrize("terminal_width", [120, 100, 80])
    async def test_every_column_is_wider_than_its_worst_case_cell(self, terminal_width):
        from proxy_tui import ProxyTUI
        from textual.widgets import DataTable

        class _Limiter:
            def status(self) -> dict:
                return {}

        class _LogHandler:
            def get_logs(self, limit=50, from_seq=0):
                return []

            def clear(self):
                pass

        tui = ProxyTUI(rate_limiter=_Limiter(), tui_log_handler=_LogHandler(), proxy_app={})
        async with tui.run_test(size=(terminal_width, 40)) as pilot:
            await pilot.pause()
            grid = tui.query_one("#provider-grid", DataTable)
            widths = {str(c.label): c.width for c in grid.ordered_columns}

            # cell_padding must stay 0: these widths assume no implicit gap.
            assert grid.cell_padding == 0

            # Full width: the table must fit with no horizontal scrollbar even
            # in an 80-column terminal, and leave slack in the panel.
            assert sum(widths.values()) <= grid.content_size.width, (
                f"grid ({sum(widths.values())}) overflows its {grid.content_size.width}-cell "
                f"panel at {terminal_width} columns -> horizontal scrollbar"
            )
            assert not grid.show_horizontal_scrollbar

            for name, content in self._worst_case_cells().items():
                assert widths[name] > content, (
                    f"{name}: width {widths[name]} exactly fits a {content}-char "
                    f"cell -> cells touch with cell_padding=0"
                )

    def test_compact_number_never_exceeds_six_chars(self):
        # Bounded output is what lets the Fwd/429 columns stay 7 wide.
        from tui_status import _compact_number
        assert _compact_number(999) == "999"
        assert _compact_number(1_000) == "1.0K"
        assert _compact_number(2_500) == "2.5K"
        # Rounding must carry the unit instead of emitting "1000.0K".
        assert _compact_number(999_999) == "1.0M"
        assert _compact_number(999_999_999) == "1.0B"
        assert _compact_number(999_999_999_999) == "1.0T"
        for n in (0, 7, 999, 1_000, 12_345, 999_500, 999_999, 1_000_000,
                  123_456_789, 999_999_999, 1_000_000_000_000):
            assert len(_compact_number(n)) <= 6, f"{n} -> {_compact_number(n)!r}"

    def test_progress_bar_clamps_negative_current_to_empty_bar(self):
        """`_progress_bar` clamps BOTH ends of the fill.

        The old code clamped only the top (`min(filled, width)`), so a
        negative `current` — reachable straight from a negative
        `tpm_available` in the detail pane — made
        `empty = width - filled` EXCEED `width` and overflowed the cell.
        The bar must render exactly `width` cells for negative input, the
        same length as a normal reading.
        """
        from proxy_tui import _progress_bar
        normal = _progress_bar(10000, 20000)
        negative = _progress_bar(-5000, 20000)
        assert len(negative) == len(normal) == 32, (
            f"negative bar changed length: {len(negative)} vs {len(normal)}"
        )
        width = 20
        for current in (-1, -5000, -10 ** 9):
            rendered = _progress_bar(current, 20000)
            bar = rendered.split(" ", 1)[0]
            filled = bar.count("\u2588")
            empty = bar.count("\u2591")
            assert filled == 0, f"{current}: negative fill leaked: {rendered!r}"
            assert empty == width - filled, (
                f"{current}: empty {empty} != width - filled {width - filled}"
            )
            assert filled + empty == width, (
                f"{current}: bar is {filled + empty} cells, expected {width}"
            )

    def test_progress_bar_clamps_over_maximum_to_full_bar(self):
        """The upper clamp survives alongside the lower one: no overflow."""
        from proxy_tui import _progress_bar
        width = 20
        for current in (20000, 99999):
            rendered = _progress_bar(current, 20000)
            bar = rendered.split(" ", 1)[0]
            assert len(bar) == width, (
                f"{current}: bar is {len(bar)} cells, expected {width}"
            )
            assert bar.count("\u2588") == width, f"{current}: not saturated: {rendered!r}"
            assert bar.count("\u2591") == 0, f"{current}: not saturated: {rendered!r}"

    def test_progress_bar_degenerate_maximum_renders_empty_bar(self):
        """`maximum <= 0` renders an empty bar rather than raising."""
        from proxy_tui import _progress_bar
        rendered = _progress_bar(5, 0)
        bar = rendered.split(" ", 1)[0]
        assert len(bar) == 20
        assert bar.count("\u2588") == 0
        assert bar.count("\u2591") == 20

    def test_compact_number_saturates_suffix_at_terae(self):
        """Past "T" the suffix saturates and the <= 6-char bound wins.

        The promotion loop keeps dividing after the suffix has saturated at
        "T", so `10**15` renders as "1.0T" and loses 1000x of magnitude.
        That is DELIBERATE, not an oversight: this function's contract is a
        hard 6-character ceiling, because the Overview grid runs
        `cell_padding = 0` and sizes its RPM/TPM columns from that budget. A
        10**15 counter cannot occur through a local proxy, so the ceiling is
        worth more than the magnitude.
        """
        from tui_status import _compact_number
        assert _compact_number(10 ** 15) == "1.0T"
        # The ceiling holds at every magnitude, however absurd.
        for n in (10 ** 15, 10 ** 18, 10 ** 21):
            rendered = _compact_number(n)
            assert len(rendered) <= 6, f"{n} -> {rendered!r} exceeds the grid budget"
            assert rendered.endswith("T"), f"{n} -> {rendered!r} lost the suffix"
        # Ordinary promotions are unaffected.
        assert _compact_number(999) == "999"
        assert _compact_number(1_000) == "1.0K"
        assert _compact_number(999_999) == "1.0M"

    def test_compact_number_unit_ladder_is_monotonic(self):
        """Parsed magnitudes never decrease up the unit ladder.

        A future reordering of `_COMPACT_UNITS` would show up here as a
        mantissa that runs backwards even while every individual rendering
        still looks plausible. Scoped to <= "T", which is where the ladder
        actually tracks magnitude (past "T" the suffix saturates by design).
        """
        from tui_status import _compact_number

        def _magnitude(rendered):
            multipliers = {"K": 1_000, "M": 1_000_000,
                           "B": 1_000_000_000, "T": 1_000_000_000_000}
            if rendered[-1] in multipliers:
                return float(rendered[:-1]) * multipliers[rendered[-1]]
            return float(rendered)

        inputs = [999, 1_000, 999_999, 10 ** 6, 10 ** 9, 10 ** 12]
        magnitudes = [_magnitude(_compact_number(n)) for n in inputs]
        assert magnitudes == sorted(magnitudes), (
            f"unit ladder runs backwards: {list(zip(inputs, magnitudes))}"
        )



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


# ---------------------------------------------------------------------------
# Item 13: tab gating, cursor/scroll preservation, dirty-gated rebuilds
# ---------------------------------------------------------------------------

def _live_tui_app():
    """A ProxyTUI wired for a headless run, with a log handler we can push to."""
    from proxy_tui import ProxyTUI

    class _Handler:
        def __init__(self):
            self.entries = []
            self._seq = 0

        def add(self, level, message):
            self.entries.append({"seq": self._seq, "level": level,
                                 "message": message, "timestamp": ""})
            self._seq += 1

        def get_logs(self, limit=100, from_seq=0):
            return [e for e in self.entries if e["seq"] >= from_seq][:limit]

        def snapshot(self):
            return list(self.entries)

        def clear(self):
            self.entries.clear()
            self._seq = 0

    handler = _Handler()
    tui = ProxyTUI(rate_limiter=_MultiProviderLimiter(), tui_log_handler=handler,
                   proxy_app={})
    return tui, handler


class TestPollUpdaterGating:
    """Lock the tab-gated dispatch contract.

    The complaint this fixes was "a log line jumps / the model table snaps
    back": every tick rebuilt EVERY table on EVERY tab eight times over,
    whether or not the user could see it. `_POLL_UPDATERS` is now the single
    list of per-tick work, each entry naming the tab that must be active for it
    to be dispatched — so the gating is a data contract that can be asserted,
    not a scattering of `if` statements inside the poll loop.
    """

    EXPECTED = {
        "tab-overview": {"_poll_logs", "_update_metrics", "_update_provider_grid"},
        "tab-logs": {"_poll_logs"},
        "tab-metrics": {"_poll_logs", "_update_sparklines", "_update_failover_panel",
                        "_update_latency_histogram"},
        "tab-models": {"_poll_logs", "_update_model_table"},
        "tab-config": {"_poll_logs", "_update_config_table_filtered"},
    }

    @staticmethod
    async def _dispatched(tui, tab):
        """Run one dispatch and report what it scheduled.

        Must cross a thread boundary: Textual's `call_from_thread` raises if
        it is invoked on the thread that owns the app.
        """
        scheduled = []
        original = tui.call_from_thread

        def record(callback, *args, **kwargs):
            scheduled.append(getattr(callback, "__name__", "?"))
            return original(callback, *args, **kwargs)

        tui.call_from_thread = record
        try:
            count = await asyncio.to_thread(tui._dispatch_poll_updates, {})
        finally:
            tui.call_from_thread = original
        return count, scheduled

    @pytest.mark.parametrize("tab", sorted(EXPECTED))
    async def test_only_the_active_tab_is_dispatched(self, tab):
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui._active_tab = tab
            count, scheduled = await self._dispatched(tui, tab)
            await pilot.pause()

            assert set(scheduled) == self.EXPECTED[tab], (
                f"{tab} dispatched {sorted(scheduled)}, "
                f"expected {sorted(self.EXPECTED[tab])}"
            )
            assert count == len(self.EXPECTED[tab])

    async def test_churn_is_far_below_the_pre_gating_everything(self):
        """The headline number: 10 unconditional updates -> at most 4."""
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            for tab, expected in self.EXPECTED.items():
                tui._active_tab = tab
                count, _ = await self._dispatched(tui, tab)
                await pilot.pause()
                assert count == len(expected)
            # The old poll loop called call_from_thread ten times per tick
            # (eight updaters + the two conditional poll-error banners).
            assert max(len(v) for v in self.EXPECTED.values()) < 10

    def test_every_updater_declares_the_tab_it_belongs_to(self):
        """No updater may be added without saying which tab gates it."""
        from proxy_tui import ProxyTUI
        names = [entry[0] for entry in ProxyTUI._POLL_UPDATERS]
        assert names == sorted(set(names), key=names.index), "duplicate entry"
        for name, tab, _takes_status in ProxyTUI._POLL_UPDATERS:
            assert hasattr(ProxyTUI, name), f"{name} is not a ProxyTUI method"
            if name == "_poll_logs":
                # The one unconditional target: it is the only source of
                # `error_count`, so it self-gates (count-only pass when the
                # Logs tab is hidden) instead of being skipped.
                assert tab is None
                continue
            assert tab in ProxyTUI.EXPECTED_TAB_IDS, f"{name} has no known tab"

    def test_tab_ids_match_the_composed_tab_panes(self):
        from proxy_tui import ProxyTUI
        for name, tab, _ in ProxyTUI._POLL_UPDATERS:
            if tab is not None:
                assert tab in ProxyTUI.EXPECTED_TAB_IDS

    async def test_expected_tab_ids_match_what_is_actually_composed(self):
        from proxy_tui import ProxyTUI
        from textual.widgets import TabPane
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            composed = {str(p.id) for p in tui.query(TabPane) if p.id}
            assert composed == set(ProxyTUI.EXPECTED_TAB_IDS), (
                "`EXPECTED_TAB_IDS` drifted from the composed tab panes; "
                "`on_mount` raises on this, but only when it is already too late"
            )


class TestTabSwitchRefreshesImmediately:
    """A tab switch must not wait out the adaptive interval.

    The interval backs off to 16 s (and to 5 s after five consecutive errors),
    so a switch that only repainted on the next tick would leave the new tab
    stale for up to sixteen seconds.
    """

    async def test_switch_repaints_the_new_tab_without_a_poll_tick(self):
        from textual.widgets import DataTable
        tui, status = await TestTableViewPreservedAcrossRebuilds._app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            await pilot.pause()
            table = tui.query_one("#model-usage-table", DataTable)
            table.clear()
            tui._last_raw_status = None
            await pilot.pause()
            assert table.row_count == 0, (
                "the model table was still being populated by the poller — it "
                "was not actually cancelled, so this test would prove nothing"
            )

            # The poller is dead, so only the activation handler can refill it.
            tui._last_raw_status = status
            tui.switch_to("tab-models")
            await pilot.pause()
            assert tui._active_tab == "tab-models"
            assert table.row_count > 17

    async def test_tab_activation_releases_the_poll_sleep(self):
        """The poll loop waits on `_refresh_wake`, not on the interval alone.

        It is a one-shot pulse — the loop clears it as it wakes — so it is
        tested through the handler rather than by inspecting the flag after a
        real switch (where the running poller has usually consumed it already).
        """
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui._refresh_wake.clear()

            class _Tab:
                id = "tab-logs"

            class _Event:
                pane = _Tab()

            tui.on_tabbed_content_tab_activated(_Event())
            assert tui._active_tab == "tab-logs"
            assert tui._refresh_wake.is_set(), (
                "without this the poller sleeps out the full adaptive interval "
                "(up to 16 s) and the newly-visible tab stays stale"
            )

    async def test_shutdown_also_releases_the_poll_sleep(self):
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            tui._shutdown_event.clear()
            tui._refresh_wake.clear()
            tui._signal_proxy_shutdown()
            assert tui._shutdown_event.is_set()
            assert tui._refresh_wake.is_set(), (
                "the poll loop sleeps on `_refresh_wake`, so the shutdown flag "
                "has to wake it or teardown waits out the rest of the interval"
            )

    async def test_switch_never_reports_a_poll_error(self):
        """`_refresh_visible_tab` runs before layout on early activations."""
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for tab in ("tab-logs", "tab-metrics", "tab-models", "tab-config",
                        "tab-overview"):
                tui.switch_to(tab)
                await pilot.pause()
            assert tui._poll_error_count == 0, (
                "an immediate refresh tripped the poll-error path — it is "
                "running before the tables are configured"
            )


class TestHiddenLogsBacklogPreserved:
    """Skipping the drain while the Logs tab is hidden must not lose entries.

    `#error-banner`'s `error_count` is accumulated as a side effect of writing
    log lines, so gating the drain freezes the banner. The fix gives the count
    its own cursor (`_error_scanned_seq`) which deliberately does NOT advance
    the widget cursor (`_log_seqs`), so nothing is consumed early. The
    invariant is: exactly once, in order, after a hide/show cycle.
    """

    async def test_hidden_entries_drain_once_and_in_order_on_return(self):
        from textual.widgets import Log
        tui, handler = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()

            tui._active_tab = "tab-logs"
            handler.add("INFO", "before")
            tui._poll_logs(visible=True)
            await pilot.pause()

            # Hidden across several poll cycles, with entries arriving.
            tui._active_tab = "tab-overview"
            for cycle in range(4):
                for i in range(6):
                    handler.add("INFO", f"hidden-{cycle}-{i}")
                tui._poll_logs()
                await pilot.pause()

            assert tui._log_seqs["#live-log-full"] == 1, (
                "the hidden pass advanced the WIDGET cursor — the backlog the "
                "user returns to would be missing its first entries"
            )

            tui._active_tab = "tab-logs"
            tui._poll_logs(visible=True)
            await pilot.pause()

            lines = [str(line) for line in tui.query_one("#live-log-full", Log).lines]
            assert len(lines) == 1 + 4 * 6
            assert len(set(lines)) == len(lines), "duplicate entries"
            assert lines[1:] == [
                f"[INFO]  -- hidden-{c}-{i}" for c in range(4) for i in range(6)
            ], "entries missing, duplicated, or out of order"

    async def test_error_count_keeps_running_while_the_tab_is_hidden(self):
        tui, handler = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()

            tui._active_tab = "tab-overview"
            for i in range(5):
                handler.add("ERROR", f"boom-{i}")
                tui._poll_logs()
            assert tui.error_count == 5, (
                "error_count froze while the Logs tab was hidden — "
                "#error-banner would sit wrong until the user switched back"
            )

            # Returning must not count the same errors a second time.
            tui._active_tab = "tab-logs"
            tui._poll_logs(visible=True)
            await pilot.pause()
            assert tui.error_count == 5

    async def test_error_count_is_not_re_counted_by_a_filter_rewind(self):
        """Re-filtering rewinds the widget cursor to 0; the count must not."""
        tui, handler = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()

            tui._active_tab = "tab-overview"
            for i in range(3):
                handler.add("ERROR", f"boom-{i}")
            tui._poll_logs()
            assert tui.error_count == 3

            tui._active_tab = "tab-logs"
            tui._log_seqs["#live-log-full"] = 0
            tui._poll_logs(visible=True)
            await pilot.pause()
            assert tui.error_count == 3, "the rewind re-counted already-counted errors"

    def test_clear_logs_rewinds_both_cursors(self):
        """`TUILogHandler.clear()` restarts seq at 0, so the count cursor must too."""
        tui = _bare_tui()
        cleared = []
        tui.log_handler = type("H", (), {"clear": lambda _s: cleared.append(1)})()
        tui.query_one = lambda sel, _t=None: type("L", (), {"clear": lambda _s: None})()
        tui._error_scanned_seq = 12
        tui._log_seqs["#live-log-full"] = 12
        tui.error_count = 4

        tui.action_clear_logs()

        assert cleared, "the log handler was never cleared"
        assert tui._error_scanned_seq == 0
        assert tui._log_seqs == {}
        assert tui.error_count == 0

    async def test_pausing_freezes_the_log_feed(self):
        """Pause keeps its old meaning: neither the feed nor the count moves."""
        tui, handler = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui._logs_paused = True
            tui._active_tab = "tab-overview"
            handler.add("ERROR", "while paused")
            tui._poll_logs()
            assert tui.error_count == 0


class TestConfigTableDirtyGate:
    """`_config_rows` only changes when the user presses Refresh.

    The poll loop used to clear and re-add ~92 rows (109 grouped) once a
    second to redraw data that had not moved. The dirty flag makes that a
    no-op — and the Refresh button must still visibly re-render.
    """

    async def test_clean_polls_do_not_rebuild_the_table(self, monkeypatch):
        from textual.widgets import DataTable
        import proxy_tui as pt
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui._active_tab = "tab-config"
            table = tui.query_one("#config-table", DataTable)
            rows_before = table.row_count
            assert rows_before > 50

            loads = []
            real = pt._load_display_config
            monkeypatch.setattr(pt, "_load_display_config",
                                lambda: loads.append(1) or real())

            for _ in range(10):
                tui._active_tab = "tab-config"
                tui._update_config_table_filtered()
                await pilot.pause()
            assert loads == [], "a clean poll reloaded the config"
            assert table.row_count == rows_before

    async def test_refresh_button_reloads_and_re_renders(self, monkeypatch):
        from textual.widgets import DataTable
        import proxy_tui as pt
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-config")
            await pilot.pause()

            loads = []
            real = pt._load_display_config
            monkeypatch.setattr(pt, "_load_display_config",
                                lambda: loads.append(1) or real())

            # Shrink the rows behind the table's back, then press Refresh.
            tui._config_rows = real()[:1]
            tui._config_dirty = True
            tui._update_config_table_filtered()
            await pilot.pause()
            assert tui.query_one("#config-table", DataTable).row_count == 1

            tui.query_one("#config-refresh-btn").press()
            await pilot.pause()
            await pilot.pause()
            assert loads, "Refresh did not reload the display config"
            assert tui.query_one("#config-table", DataTable).row_count > 1

    async def test_grouped_toggle_and_filter_still_rebuild(self):
        from textual.widgets import DataTable
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-config")
            await pilot.pause()
            table = tui.query_one("#config-table", DataTable)
            flat = table.row_count

            tui.query_one("#config-grouped-btn").press()
            await pilot.pause()
            grouped = table.row_count
            assert grouped > flat, "grouped view adds group heading rows"

            tui._config_filter = "proxy"
            tui._config_dirty = True
            tui._update_config_table_filtered()
            await pilot.pause()
            assert table.row_count < grouped


class TestTableViewPreservedAcrossRebuilds:
    """`DataTable.clear()` resets the cursor coordinate AND the scroll offset.

    Every table here is rebuilt with `clear()` + `add_row()` on each poll, so
    without an explicit restore the user's cursor and scroll position were
    thrown away once a second — the "I'm scrolling the model table and it
    snaps back" symptom.
    """

    @staticmethod
    def _view(table):
        return (table.cursor_coordinate.row, table.cursor_coordinate.column,
                table.scroll_offset.x, table.scroll_offset.y)

    @staticmethod
    async def _app():
        from proxy_tui import ProxyTUI
        from tui_status import PROVIDER_KEYS

        totals = _status_with_totals()
        provider = dict(totals["primary"])
        provider["model_usage"] = {
            f"model-{i:02d}": {"requests": i + 1, "tokens": 100 * i,
                               "errors_429": 0, "avg_latency_ms": 10.0 + i,
                               "p50_latency_ms": 12.0, "p95_latency_ms": 30.0}
            for i in range(40)
        }
        status = {key: dict(provider) for key in PROVIDER_KEYS}
        # Copy ONLY the aggregates. `totals` also carries a per-provider entry
        # for each of PROVIDER_KEYS, and `update()` with those would silently
        # replace the populated providers with the empty `model_usage` one.
        for key, value in totals.items():
            if key not in PROVIDER_KEYS:
                status[key] = value

        class _Limiter:
            def status(self):
                return status

        class _Handler:
            def get_logs(self, limit=100, from_seq=0):
                return []

            def clear(self):
                pass

        return ProxyTUI(rate_limiter=_Limiter(), tui_log_handler=_Handler(),
                        proxy_app={}), status

    async def test_model_table_cursor_and_scroll_survive_polls(self):
        from textual.widgets import DataTable
        tui, status = await self._app()
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-models")
            await pilot.pause()

            table = tui.query_one("#model-usage-table", DataTable)
            # The poller is cancelled and `switch_to` only repaints from the
            # status the poller published, so seed the table explicitly —
            # `move_cursor(row=17)` on an empty table is clamped to row 0.
            tui._update_model_table(status)
            await pilot.pause()
            assert table.row_count > 17

            table.move_cursor(row=17, column=0)
            table.set_scroll(x=0, y=9)
            await pilot.pause()
            before = self._view(table)
            assert before == (17, 0, 0, 9)

            for _ in range(5):
                tui._active_tab = "tab-models"
                tui._update_model_table(status)
                await pilot.pause()
            assert self._view(table) == before, "model table view moved"

    async def test_config_table_cursor_and_scroll_survive_polls(self):
        from textual.widgets import DataTable
        tui, _ = await self._app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-config")
            await pilot.pause()

            table = tui.query_one("#config-table", DataTable)
            table.move_cursor(row=40, column=1)
            table.set_scroll(x=0, y=25)
            await pilot.pause()
            before = self._view(table)

            tui._config_dirty = True
            for _ in range(5):
                tui._active_tab = "tab-config"
                tui._config_dirty = True
                tui._update_config_table_filtered()
                await pilot.pause()
            assert self._view(table) == before, "config table view moved"

    async def test_provider_grid_keeps_its_cursor_and_the_detail_pane_follows(self):
        """The grid's cursor drives the detail pane via RowHighlighted.

        Two things must hold: the cursor survives `clear()` (which also resets
        the scroll offset and posts a RowHighlighted for row 0), and the detail
        pane must never flash the first provider as a side effect of a rebuild.
        """
        from textual.widgets import DataTable, Static
        tui, status = await self._app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-overview")
            await pilot.pause()

            grid = tui.query_one("#provider-grid", DataTable)
            detail = tui.query_one("#provider-detail", DataTable)
            title = tui.query_one("#provider-detail-title", Static)

            grid.move_cursor(row=5, column=0)
            await pilot.pause()
            assert tui._detail_provider_key == "senary"
            detail_before = self._view(detail)

            seen_titles = []
            for _ in range(5):
                tui._active_tab = "tab-overview"
                tui._update_provider_grid(status)
                seen_titles.append(str(title.content))
                await pilot.pause()

            assert self._view(grid) == (5, 0, 0, 0), "grid cursor moved"
            assert tui._detail_provider_key == "senary"
            assert set(seen_titles) == {"DeepSeek — ok"}, (
                f"detail pane flickered to another provider during a rebuild: "
                f"{seen_titles}"
            )
            assert self._view(detail) == detail_before

            # A real user move must still drive the pane.
            grid.move_cursor(row=2, column=0)
            await pilot.pause()
            assert tui._detail_provider_key == "tertiary"
            assert "OpenLux" in str(title.content)

    async def test_row_highlighted_handler_respects_the_suppression_guard(self):
        from proxy_tui import ProxyTUI
        tui = _bare_tui()
        called = []
        tui._suppress_detail_follow = True
        tui._grid_row_keys = ["primary", "secondary"]
        tui._last_raw_status = None
        tui._update_provider_detail = lambda status: called.append(status)

        class _Event:
            class _Table:
                id = "provider-grid"

            data_table = _Table()
            cursor_row = 1

        ProxyTUI.on_data_table_row_highlighted(tui, _Event())
        assert called == [], "a suppressed RowHighlighted repainted the pane"
        assert tui._detail_provider_key == "primary"

    async def test_restore_clamps_a_cursor_past_the_end(self):
        """A shrinking table must not leave the cursor or scroll out of range."""
        from textual.widgets import DataTable
        tui, _ = await self._app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-config")
            await pilot.pause()

            table = tui.query_one("#config-table", DataTable)
            big = table.row_count
            assert big > 5

            tui._config_filter = "proxy_port"
            tui._config_dirty = True
            tui._update_config_table_filtered()
            await pilot.pause()
            await pilot.pause()
            assert table.row_count < big
            assert 0 <= table.cursor_coordinate.row < table.row_count
            assert 0 <= table.scroll_offset.y


class TestSessionLogReadOncePerPoll:
    """`#alert-badge` and the failover panel both read today's session log.

    They used to each call `_read_session_log_entries`, so one file cost two
    `os.stat` + `open`/`seek`/`read` passes and two deque rescans per tick.
    The read is now cached per POLL GENERATION (bumped by the poll loop and by
    every tab switch) — never a wall clock, so the cache cannot outlive a tick.
    """

    def test_second_consumer_in_one_generation_reuses_the_read(self):
        tui = _bare_tui()
        reads = []

        def read():
            reads.append(1)
            return [{"request_id": f"r{i}"} for i in range(3)]

        tui._read_session_log_entries = read
        tui._bump_poll_generation()
        first = tui._session_log_snapshot()
        second = tui._session_log_snapshot()
        third = tui._session_log_snapshot()
        assert len(reads) == 1
        assert first is second is third

        tui._bump_poll_generation()
        tui._session_log_snapshot()
        assert len(reads) == 2, "a new poll generation must re-read"

    def test_the_cached_call_is_still_the_only_thing_that_advances_the_offset(
        self, tmp_path, monkeypatch
    ):
        """The incremental / inode-rotation state machine is untouched.

        `_session_log_snapshot` caches only the RETURNED LIST; the offset,
        inode and partial-line carry all stay inside
        `_read_session_log_entries`, so reading once per tick instead of twice
        cannot drop or duplicate a line.
        """
        import json
        from datetime import datetime, timezone
        monkeypatch.chdir(tmp_path)
        log_dir = tmp_path / "session_logs"
        log_dir.mkdir()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = log_dir / f"{today}.jsonl"

        tui = _bare_tui()
        path.write_text(
            json.dumps({"request_id": "a"}) + "\n", encoding="utf-8")
        offset_after_a = None
        tui._bump_poll_generation()
        assert [e["request_id"] for e in tui._session_log_snapshot()] == ["a"]
        offset_after_a = tui._session_log_offset
        assert offset_after_a == path.stat().st_size

        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"request_id": "b"}) + "\n")
        # Same generation: the cached list is returned and the offset is NOT
        # touched, because only a real read advances it.
        assert [e["request_id"] for e in tui._session_log_snapshot()] == ["a"]
        assert tui._session_log_offset == offset_after_a

        tui._bump_poll_generation()
        assert [e["request_id"] for e in tui._session_log_snapshot()] == ["a", "b"]
        assert tui._session_log_offset == path.stat().st_size


class TestChromeCollapsesAndTruncatesHonestly:
    """The two chrome items: the failover heading and the alert badge."""

    async def test_failover_title_collapses_with_its_panel(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone
        from textual.widgets import Static
        tui, _ = _live_tui_app()
        monkeypatch.chdir(tmp_path)
        log_dir = tmp_path / "session_logs"
        log_dir.mkdir()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-metrics")
            await pilot.pause()

            panel = tui.query_one("#failover-events-panel", Static)
            title = tui.query_one("#failover-title", Static)

            # No failover events yet: BOTH must be collapsed, or the heading
            # floats above an empty region.
            assert not panel.display and not title.display
            assert not title.display, "the failover heading survived its panel"

            (log_dir / f"{today}.jsonl").write_text(
                '{"request_id": "abcdef1234", "attempted_providers": ["primary", "secondary"],'
                ' "model": "qwen3-coder-plus", "timestamp_utc": "%s"}\n'
                % datetime.now(timezone.utc).isoformat(),
                encoding="utf-8",
            )
            tui._bump_poll_generation()
            tui._update_failover_panel()
            await pilot.pause()
            assert panel.display and title.display

            (log_dir / f"{today}.jsonl").write_text("", encoding="utf-8")
            tui._session_log_path = None
            tui._bump_poll_generation()
            tui._update_failover_panel()
            await pilot.pause()
            assert not panel.display
            assert not title.display, "the heading outlived the panel"

    async def test_the_title_is_the_only_docked_widget_in_the_metrics_pane(self):
        """Textual gives every `dock: top` sibling the SAME row.

        A second docked widget next to `.panel-title` would paint over it
        rather than stack below it — which is exactly what happened when the
        alert badge and error banner were docked in `#overview-panel`.
        """
        from textual.containers import Container  # noqa: F401  (documents the type)
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for container_id in ("#metrics-tab-content", "#overview-panel"):
                container = tui.query_one(container_id)
                # `styles.dock` defaults to the STRING "none", which is truthy,
                # so `if w.styles.dock` matches every widget in the tree. Only
                # an explicit rule counts.
                docked = [w for w in container.walk_children()
                          if w.styles.get_rule("dock") == "top"]
                assert len(docked) <= 1, (
                    f"{container_id} has {len(docked)} docked widgets "
                    f"({[w.id for w in docked]}) sharing one row"
                )

    def test_ellipsize_marks_the_cut_and_lands_on_a_boundary(self):
        from proxy_tui import _ellipsize
        text = "ALERT: " + " | ".join(f"RPM ({k})" for k in
                                      ["secondary", "tertiary", "quaternary",
                                       "quinary", "octonary", "nonary",
                                       "decenary", "undenary"]) + " critical"
        assert _ellipsize(text, 200) == text, "must not touch text that fits"
        assert _ellipsize(text, 0) == text, "unlaid-out badge must not clip to ''"

        for width in range(8, len(text)):
            trimmed = _ellipsize(text, width)
            assert len(trimmed) <= width, f"width {width} -> {trimmed!r}"
            if len(text) <= width:
                continue
            assert trimmed.endswith("…"), f"width {width} -> {trimmed!r}"
            kept = trimmed[:-1]
            assert text.startswith(kept), (
                f"width {width}: the kept prefix is not a prefix of the source"
            )
            assert " (" not in kept[-8:] or not kept.endswith("("), (
                f"width {width} -> {trimmed!r}: cut landed inside a label"
            )
        assert _ellipsize("abcdefgh", 1) == "…"

    async def test_alert_badge_stays_one_row_and_is_ellipsized(self):
        from textual.widgets import Static
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.switch_to("tab-overview")
            await pilot.pause()

            status = _status_with_totals()
            for key in status:
                if isinstance(status[key], dict) and "rpm_limit" in status[key]:
                    status[key]["rpm_current"] = status[key]["rpm_limit"]
            tui._update_alert_badge(status)
            await pilot.pause()

            badge = tui.query_one("#alert-badge", Static)
            assert badge.size.height == 1, "the badge must not grow the Overview"
            assert badge.display
            row = "".join(seg.text for seg in badge.render_line(0))
            assert len(row) <= badge.size.width
            assert "…" in row, f"truncation is not marked: {row!r}"
            # The visible marker must come from `_ellipsize`, not from a CSS
            # `text-overflow` that Textual 8.2.8 never applies to a Static.
            assert str(badge.content).endswith("…")


# ---------------------------------------------------------------------------
# Item N: `capture_screenshots` must unwrap the /v1/proxy/status ENVELOPE.
# ---------------------------------------------------------------------------

def _http_envelope(latencies=None, top_level_latencies=None):
    """The payload `GET /v1/proxy/status` actually serves.

    `handlers.py` builds `{"rate_limits": rate_limiter.status(), ...}`, i.e.
    the limiter status is nested ONE LEVEL DEEP. The TUI, however, consumes it
    FLAT (`primary` / `secondary` / ... at the top level). Serving the envelope
    to the screenshot harness is therefore the regression: before the unwrap the
    grid came back empty and `capture_screenshots.py` refused to run.
    """
    limiter = _status_with_totals()
    if latencies is not None:
        limiter["recent_latencies"] = list(latencies)
    payload = {
        "rate_limits": limiter,
        "providers": {"primary": {"available": True}},
        "model_overlaps": {"qwen3.6-plus": ["primary"]},
    }
    if top_level_latencies is not None:
        payload["recent_latencies"] = list(top_level_latencies)
    return payload


class _FakeResponse:
    """Minimal `urlopen` context manager returning a canned body."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@contextlib.contextmanager
def _status_endpoint(payload):
    """Serve `payload` from the screenshot harness's `/v1/proxy/status` fetch."""
    import capture_screenshots

    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    original = capture_screenshots.urlopen
    capture_screenshots.urlopen = lambda *args, **kwargs: _FakeResponse(body)
    try:
        yield
    finally:
        capture_screenshots.urlopen = original


@contextlib.contextmanager
def _endpoint_unreachable():
    """Make the harness's status fetch fail at the transport level."""
    import capture_screenshots

    def _boom(*args, **kwargs):
        raise OSError("connection refused")

    original = capture_screenshots.urlopen
    capture_screenshots.urlopen = _boom
    try:
        yield
    finally:
        capture_screenshots.urlopen = original


class TestStatusEnvelopeUnwrap:
    """Lock the envelope→flat unwrap in `capture_screenshots`.

    `capture_screenshots.py` renders the real `ProxyTUI` against a LIVE proxy, so
    its `status()` is the only thing standing between the HTTP response and the
    TUI. It used to hand the whole envelope through verbatim, which fed the
    dashboard a payload one nesting level too deep: `provider_grid_rows` found
    no provider sub-dicts, and the fail-loud guard misdiagnosed its own proxy's
    legitimate response shape as a foreign service squatting the port.

    Three outcomes must stay distinguishable:
      (a) unreachable         → `fetch_failed`, placeholder status, exit 0;
      (b) reachable, unusable → `payload_error`, hard error, NOTHING written;
      (c) reachable, usable   → the flat limiter status.
    """

    def test_envelope_yields_provider_rows(self):
        import capture_screenshots
        from tui_status import PROVIDER_KEYS, provider_grid_rows

        envelope = _http_envelope()
        # Pre-fix behaviour, pinned so the regression cannot come back:
        # the RAW envelope produces no rows at all.
        assert provider_grid_rows(envelope) == []

        harness = capture_screenshots.LiveRateLimiterProxy("http://stub.invalid")
        with _status_endpoint(envelope):
            status = harness.status()

        assert harness.fetch_failed is False
        assert harness.payload_error is None
        rows = provider_grid_rows(status)
        assert len(rows) == len(PROVIDER_KEYS), (
            f"unwrapped envelope lost its providers: {sorted(status)[:6]}"
        )
        assert status["primary"]["total_forwarded"] == 9
        # The TUI validates per-provider status against this key set, so the
        # unwrapped dict must still satisfy it.
        from proxy_tui import ProxyTUI
        assert not (ProxyTUI._REQUIRED_STATUS_KEYS - set(status["primary"]))

    async def test_overview_grid_renders_the_unwrapped_envelope(self):
        """End to end through the real App: the grid fills with 11 rows."""
        import capture_screenshots
        from textual.widgets import DataTable
        from tui_status import PROVIDER_KEYS

        harness = capture_screenshots.LiveRateLimiterProxy("http://stub.invalid")
        tui, _ = _live_tui_app()
        # Keep the stubbed endpoint installed for the whole run so the poller
        # (until it is cancelled) also reads through the harness.
        with _status_endpoint(_http_envelope()):
            tui.rate_limiter = harness
            async with tui.run_test(size=(120, 40)) as pilot:
                tui.workers.cancel_all()
                await pilot.pause()
                tui._update_provider_grid(harness.status())
                await pilot.pause()
                grid = tui.query_one("#provider-grid", DataTable)
                assert grid.row_count == len(PROVIDER_KEYS), (
                    f"grid has {grid.row_count} rows, expected {len(PROVIDER_KEYS)}"
                )

    def test_already_flat_payload_is_passed_through(self):
        import capture_screenshots
        from tui_status import provider_grid_rows

        flat = _status_with_totals()
        harness = capture_screenshots.LiveRateLimiterProxy("http://stub.invalid")
        with _status_endpoint(flat):
            status = harness.status()
        assert harness.payload_error is None
        assert status["primary"] == flat["primary"]
        assert len(provider_grid_rows(status)) == len(
            [k for k, v in flat.items() if isinstance(v, dict) and "rpm_limit" in v]
        )

    @pytest.mark.parametrize("latencies, top_level, expected", [
        # Nested (the limiter's own combined list) survives the unwrap.
        ([1.0, 2.0, 3.0], None, [1.0, 2.0, 3.0]),
        # Top-level survives when the nested one is absent.
        (None, [4.0, 5.0], [4.0, 5.0]),
        # Neither list is dropped for the other: the longer wins.
        ([1.0], [7.0, 8.0, 9.0], [7.0, 8.0, 9.0]),
        ([1.0, 2.0, 3.0], [7.0], [1.0, 2.0, 3.0]),
        # A populated list is never blanked by an empty sibling.
        ([1.0, 2.0], [], [1.0, 2.0]),
        # Neither offered → the last-resort local fallback.
        (None, None, []),
    ])
    def test_recent_latencies_survive_the_unwrap(self, latencies, top_level, expected):
        import capture_screenshots

        harness = capture_screenshots.LiveRateLimiterProxy("http://stub.invalid")
        harness._recent_latencies = []
        with _status_endpoint(_http_envelope(latencies, top_level)):
            status = harness.status()
        assert status["recent_latencies"] == expected

    def test_local_latency_fallback_is_kept(self):
        """The pre-existing `self._recent_latencies` fallback still applies."""
        import capture_screenshots

        harness = capture_screenshots.LiveRateLimiterProxy("http://stub.invalid")
        harness._recent_latencies = [42.0]
        with _status_endpoint({"rate_limits": {"primary": {}, "recent_latencies": []}}):
            status = harness.status()
        assert status["recent_latencies"] == [42.0]

    @pytest.mark.parametrize("bad", ["rate_limits: not an object", ["a"], None, 7])
    def test_malformed_rate_limits_is_reported_not_raised(self, bad):
        """A non-dict `rate_limits` is a SHAPE error, not an AttributeError."""
        import capture_screenshots

        harness = capture_screenshots.LiveRateLimiterProxy("http://stub.invalid")
        with _status_endpoint({"rate_limits": bad, "providers": {}, "model_overlaps": {}}):
            status = harness.status()
        assert harness.fetch_failed is False, "a bad payload is not an unreachable host"
        assert harness.payload_error and "'rate_limits'" in harness.payload_error
        assert status == harness._default_status()

    def test_non_object_json_is_reported_not_raised(self):
        import capture_screenshots

        harness = capture_screenshots.LiveRateLimiterProxy("http://stub.invalid")
        with _status_endpoint([1, 2, 3]):
            status = harness.status()
        assert harness.payload_error and "expected an object" in harness.payload_error
        assert status == harness._default_status()

    def test_unreachable_keeps_the_placeholder_path(self):
        """Case (a): unreachable → placeholder, NOT a hard error."""
        import capture_screenshots

        harness = capture_screenshots.LiveRateLimiterProxy("http://stub.invalid")
        with _endpoint_unreachable():
            status = harness.status()
        assert harness.fetch_failed is True
        assert harness.payload_error is None
        assert status == harness._default_status()

    @pytest.mark.parametrize("payload", [
        {"error": "not found"},                                   # no rate_limits
        {"rate_limits": "not an object"},                         # malformed
    ])
    async def test_main_refuses_unusable_payloads_and_writes_nothing(self, payload):
        """Case (b): reachable but unusable → exit 1, nothing written."""
        import capture_screenshots
        from capture_screenshots import ScreenshotSourceError

        with _status_endpoint(payload):
            with pytest.raises(ScreenshotSourceError) as excinfo:
                await capture_screenshots.main("http://stub.invalid")
        message = str(excinfo.value)
        assert "Nothing was written" in message
        # The message must describe the payload shape, not blame a foreign
        # service: `rate_limits` / `providers` / `model_overlaps` is THIS
        # proxy's own envelope.
        assert "squat" not in message.lower()


# ---------------------------------------------------------------------------
# Sparkline copy, latency-union snapshot, and poll-loop shutdown/logging locks
# ---------------------------------------------------------------------------

class TestSparklineHandsCopy:
    """`_update_sparklines` must hand widgets a COPY, not the shared list.

    `Sparkline.data` is a plain reactive holding the reference, so aliasing
    the history list lets the poll thread `append` to widget-held state.
    """

    async def test_all_four_sparklines_get_copies_not_aliases(self):
        from textual.widgets import Sparkline
        tui, _ = _live_tui_app()
        async with tui.run_test(size=(120, 40)) as pilot:
            tui.workers.cancel_all()
            await pilot.pause()
            tui.history.rpm_samples = [10, 20, 30]
            tui.history.tpm_used_samples = [100, 200, 300]
            tui.history.queue_samples = [1, 2, 3]
            tui.history.upstream_latency_samples = [0.5, 1.5, 2.5]

            tui._update_sparklines()
            await pilot.pause()

            pairs = [
                ("#rpm-sparkline", tui.history.rpm_samples),
                ("#tpm-sparkline", tui.history.tpm_used_samples),
                ("#queue-sparkline", tui.history.queue_samples),
                ("#upstream-latency-sparkline",
                 tui.history.upstream_latency_samples),
            ]
            for selector, backing in pairs:
                widget = tui.query_one(selector, Sparkline)
                assert list(widget.data) == list(backing), (
                    f"{selector} values diverged from history"
                )
                assert widget.data is not backing, (
                    f"{selector} aliases the history list — the poll "
                    f"thread would mutate widget-held state"
                )

            # The actual leak: growing the history must not grow the widget.
            tui.history.rpm_samples.append(40)
            tui.history.tpm_used_samples.append(400)
            tui.history.queue_samples.append(4)
            tui.history.upstream_latency_samples.append(3.5)
            await pilot.pause()

            expected = {
                "#rpm-sparkline": [10, 20, 30],
                "#tpm-sparkline": [100, 200, 300],
                "#queue-sparkline": [1, 2, 3],
                "#upstream-latency-sparkline": [0.5, 1.5, 2.5],
            }
            for selector, values in expected.items():
                widget = tui.query_one(selector, Sparkline)
                assert list(widget.data) == values, (
                    f"{selector} grew when history grew — it holds an alias"
                )


class TestLatencyUnionValuesSnapshot:
    """`_latency_union_values` must tolerate concurrent key insertion.

    The poll thread inserts keys via `setdefault` while a tab switch can be
    reading the union. Iterating the live view raises "dictionary changed
    size during iteration"; iterating `list(...values())` snapshots first.
    """

    def test_union_is_correct_and_fresh(self):
        tui = _bare_tui()
        from proxy_tui import LatencyTracker
        tui._latency_trackers = {
            "primary": LatencyTracker(),
            "secondary": LatencyTracker(),
        }
        tui._latency_trackers["primary"].latencies = [1.0, 2.0]
        tui._latency_trackers["secondary"].latencies = [3.0]

        union = tui._latency_union_values()
        assert union == [1.0, 2.0, 3.0]
        for tracker in tui._latency_trackers.values():
            assert union is not tracker.latencies, (
                "union aliases a tracker list — mutating it corrupts history"
            )
        union.append(99.0)
        assert 99.0 not in tui._latency_trackers["primary"].latencies
        assert 99.0 not in tui._latency_trackers["secondary"].latencies

    def test_insertion_during_iteration_does_not_raise(self):
        """A `setdefault` racing the read must neither raise nor be observed."""
        tui = _bare_tui()
        from proxy_tui import LatencyTracker
        host: dict = {}
        late = LatencyTracker()
        late.latencies = [9.0]

        class _Inserting:
            def __init__(self):
                self._vals = [1.0, 2.0]
                self._inserted = False

            @property
            def latencies(self):
                if not self._inserted:
                    self._inserted = True
                    host["tertiary"] = late
                return self._vals

        second = LatencyTracker()
        second.latencies = [3.0]
        host["primary"] = _Inserting()
        host["secondary"] = second
        tui._latency_trackers = host

        union = tui._latency_union_values()
        assert union == [1.0, 2.0, 3.0], (
            f"snapshot must not observe the concurrent insert: {union!r}"
        )

    def test_iteration_is_snapshot_based(self):
        """Pin the source of the robustness, not just its effect."""
        import inspect
        from proxy_tui import ProxyTUI
        src = inspect.getsource(ProxyTUI._latency_union_values)
        assert "list(self._latency_trackers.values())" in src, (
            "union must iterate a snapshot so setdefault cannot resize it"
        )


class TestPollTickFailureIsLogged:
    """A tick failure must EMIT a log record, never be swallowed."""

    def test_status_failure_emits_warning_with_exc_info(self, monkeypatch):
        import threading
        import proxy_tui as pt
        from proxy_tui import MetricHistory
        tui = _bare_tui()
        tui.history = MetricHistory()
        tui._shutdown_event = threading.Event()
        tui._shutdown_event.set()  # break after the first tick
        tui._poll_error_count = 0
        tui.call_from_thread = lambda *a, **k: None

        class _Boom:
            def status(self):
                raise RuntimeError("boom-status")

        tui.rate_limiter = _Boom()
        monkeypatch.setattr(
            tui._refresh_wake, "wait", lambda timeout=None: True)

        class _Worker:
            is_cancelled = False

        monkeypatch.setattr(pt, "get_current_worker", lambda: _Worker())

        warnings: list = []

        class _Logger:
            def warning(self, *a, **k):
                warnings.append((a, k))

            def info(self, *a, **k):
                pass

        monkeypatch.setattr(pt, "_tui_logger", _Logger())
        tui._poll_loop()

        assert warnings, "poll tick failure was swallowed with no log record"
        assert "poll tick failed" in str(warnings[0][0][0])
        assert warnings[0][1].get("exc_info") is True, (
            "tick failure must carry exc_info so the traceback survives"
        )

    def test_cancelled_dispatch_breaks_loop_without_warning(self, monkeypatch):
        """`CancelledError` is a BaseException: `except Exception` misses it."""
        import asyncio
        import threading
        import proxy_tui as pt
        from proxy_tui import MetricHistory
        tui = _bare_tui()
        tui.history = MetricHistory()
        tui._shutdown_event = threading.Event()
        tui._poll_error_count = 0
        tui.rate_limiter = _MultiProviderLimiter()
        tui.call_from_thread = lambda *a, **k: None

        def _cancelled(_raw):
            raise asyncio.CancelledError("dispatch torn down")

        tui._dispatch_poll_updates = _cancelled
        monkeypatch.setattr(
            tui._refresh_wake, "wait", lambda timeout=None: True)

        class _Worker:
            is_cancelled = False

        monkeypatch.setattr(pt, "get_current_worker", lambda: _Worker())

        infos: list = []
        warnings: list = []

        class _Logger:
            def warning(self, *a, **k):
                warnings.append((a, k))

            def info(self, *a, **k):
                infos.append((a, k))

        monkeypatch.setattr(pt, "_tui_logger", _Logger())
        tui._poll_loop()

        assert not warnings, f"cancel was misreported as a tick failure: {warnings!r}"
        assert any("cancelled" in str(a[0]).lower() for a, _k in infos), (
            "cancel path must log its exit"
        )


class TestPollLoopShutdownGuard:
    """Once shutdown is signalled the loop must stop dispatching."""

    def _loop_tui(self):
        import threading
        from proxy_tui import MetricHistory
        tui = _bare_tui()
        tui.history = MetricHistory()
        tui._shutdown_event = threading.Event()
        tui._poll_error_count = 0
        tui.rate_limiter = _MultiProviderLimiter()
        tui.call_from_thread = lambda *a, **k: None
        return tui

    def test_dispatch_not_reached_after_shutdown(self, monkeypatch):
        import proxy_tui as pt
        tui = self._loop_tui()
        tui._shutdown_event.set()
        monkeypatch.setattr(
            tui._refresh_wake, "wait", lambda timeout=None: True)

        class _Worker:
            is_cancelled = False

        monkeypatch.setattr(pt, "get_current_worker", lambda: _Worker())

        class _Logger:
            def warning(self, *a, **k):
                pass

            def info(self, *a, **k):
                pass

        monkeypatch.setattr(pt, "_tui_logger", _Logger())

        called: list = []
        tui._dispatch_poll_updates = lambda raw: called.append(raw)
        tui._poll_loop()
        assert called == [], "dispatch ran after shutdown was signalled"

    def test_signal_sets_both_events(self):
        import threading
        tui = _bare_tui()
        tui._shutdown_event = threading.Event()
        tui._refresh_wake.clear()
        tui._workers = []
        tui.proxy_app = {}
        tui._signal_proxy_shutdown()
        assert tui._shutdown_event.is_set()
        assert tui._refresh_wake.is_set(), (
            "the poll loop sleeps on `_refresh_wake`, so shutdown must wake it"
        )

    def test_poll_thread_exits_on_shutdown(self, monkeypatch):
        """The thread join is bounded; dispatch is stubbed so no UI is needed."""
        import threading
        import proxy_tui as pt
        tui = self._loop_tui()
        tui._workers = []
        tui.proxy_app = {}

        class _Worker:
            is_cancelled = False

        monkeypatch.setattr(pt, "get_current_worker", lambda: _Worker())

        class _Logger:
            def warning(self, *a, **k):
                pass

            def info(self, *a, **k):
                pass

        monkeypatch.setattr(pt, "_tui_logger", _Logger())

        thread = threading.Thread(target=tui._poll_loop, daemon=True)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive(), "loop exited without shutdown — test proves nothing"

        tui._signal_proxy_shutdown()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "poll thread did not exit after shutdown"

