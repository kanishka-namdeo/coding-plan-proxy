"""Textual TUI dashboard for the DashScope proxy server."""

import json
import math
import os
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Footer, Header, Log, Static, Input, Select, Sparkline, Button, TabbedContent, TabPane
from textual.worker import get_current_worker
from textual.css.query import NoMatches

# Import proxy components for shared state
from dashscope_proxy_lib.config import LOG_BUFFER_SIZE, _load_display_config
from dashscope_proxy_lib.rate_limiter import RateLimiter
from dashscope_proxy_lib.logging_config import TUILogHandler

# Provider registry for dynamic UI rendering
PROVIDER_REGISTRY = [
    {"key": "primary", "label": "DashScope", "slug": "dashscope", "config_key": "TARGET_BASE"},
    {"key": "secondary", "label": "MIMO", "slug": "mimo", "config_key": "SECONDARY_BASE_URL"},
    {"key": "tertiary", "label": "OpenLux", "slug": "openlux", "config_key": "TERTIARY_BASE_URL"},
    {"key": "quaternary", "label": "ARK", "slug": "ark", "config_key": "QUATERNARY_BASE_URL"},
    {"key": "quinary", "label": "Meta AI", "slug": "metaspark", "config_key": "QUINARY_BASE_URL"},
    {"key": "senary", "label": "DeepSeek", "slug": "deepseek", "config_key": "SENARY_BASE_URL"},
    {"key": "septenary", "label": "GLM", "slug": "glm", "config_key": "SEPTENARY_BASE_URL"},
    {"key": "octonary", "label": "Agnes Text", "slug": "agnes", "config_key": "OCTONARY_BASE_URL"},
    {"key": "nonary", "label": "Agnes Image", "slug": "agnes-image", "config_key": "NONARY_BASE_URL"},
    {"key": "decenary", "label": "Agnes Video", "slug": "agnes-video", "config_key": "DECENARY_BASE_URL"},
    {"key": "undenary", "label": "OpenCode", "slug": "opencode", "config_key": "UNDENARY_BASE_URL"},
]

# Page size for the incremental log drain loop. The TUI log handler only returns
# the LAST `limit` matching entries per call, so a backlog larger than one page
# must be drained repeatedly; each poll advances past a full page.
LOG_DRAIN_PAGE_SIZE = 500
# Prevent a corrupted or incomplete session-log record from retaining arbitrary data.
SESSION_LOG_PARTIAL_MAX_BYTES = 1_048_576


def _fmt_number(n: int) -> str:
    """Format large numbers with suffixes."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# Separator between stats on the Overview strip. Wide enough that the eye can
# find the next stat without reading every character.
_STATS_SEP = "   "


def _format_stat_strip(stats: dict, multi: dict, primary: dict) -> str:
    """Render the Overview aggregate strip as two lines.

    Replaces the old 9-row `#request-stats` DataTable: same nine values, but
    grouped instead of stacked, so the strip costs 2 rows instead of 10.

    Line 1 carries throughput/outcome, line 2 carries queue and latency:
      line 1 -- Forwarded, 429s, Rejected, Success Rate, Tokens Consumed
      line 2 -- Pending, Queue Drops, Queue Wait p50/p95/p99, Avg Req/Resp Size

    `success_rate_display` is the only source for the rate, so an idle proxy
    reads "n/a" rather than a misleading 0.0% (see the `overview_request_stats`
    note: that helper still returns a raw 0.0 float for API stability).
    """
    from tui_status import success_rate_display

    total_fwd = stats["total_forwarded"]
    total_429s = stats["total_429s"]
    rejected = stats["total_rejected"]

    avg_req = _fmt_number(multi.get("total_request_bytes", 0) // total_fwd) if total_fwd > 0 else "0"
    avg_resp = _fmt_number(multi.get("total_response_bytes", 0) // total_fwd) if total_fwd > 0 else "0"

    line1 = _STATS_SEP.join([
        f"Forwarded {_fmt_number(total_fwd)}",
        f"429s {total_429s}",
        f"Rejected {rejected}",
        f"Success {success_rate_display(total_fwd, total_429s, rejected)}",
        f"Tokens {_fmt_number(multi.get('total_tokens_consumed', 0))}",
    ])
    line2 = _STATS_SEP.join([
        f"Pending {stats['pending_requests']}/{stats['max_queue_size']}",
        f"Drops {primary.get('queue_drops', 0)}",
        "Queue p50/p95/p99 "
        f"{primary.get('queue_p50_ms', 0)}/{primary.get('queue_p95_ms', 0)}/"
        f"{primary.get('queue_p99_ms', 0)}ms",
        f"Avg Req/Resp {avg_req}B/{avg_resp}B",
    ])
    return f"{line1}\n{line2}"


def _ellipsize(text: str, width: int) -> str:
    """Clip `text` to `width` cells, ending in a real ellipsis marker.

    The alert badge is one row tall, so a long alert (many providers tripping
    quotas at once) used to be cut mid-token — `... | TPM (quinary) |` — with
    no sign that anything had been dropped. Textual's `text-overflow:
    ellipsis` is not an option: verified against 8.2.8, it never reaches a
    Static's render path, so `height: 1` + `overflow: hidden` clips identically
    with or without it. The marker therefore has to be produced here, against
    the widget's real content width.

    `width <= 0` means "not laid out yet" (the Overview tab is hidden, or this
    is the first frame) — the text is returned untouched rather than clipped to
    nothing, and the next poll trims it against a real width.

    Length is measured in characters, which is exact for the badge's content:
    provider labels and quota names are ASCII.
    """
    if width <= 0 or len(text) <= width:
        return text
    if width == 1:
        return "…"
    clipped = text[: width - 1].rstrip()
    # Land on a token boundary. An ellipsis after a half-written label is no
    # more honest than the mid-word clip it replaces — "| RPM (d…" still has
    # the reader guessing. Prefer the list separator (" | "), which is what
    # joins the alert items; fall back to a space only if there is none.
    boundary = clipped.rfind(" | ")
    if boundary <= 0:
        boundary = clipped.rfind(" ")
    if boundary > 0:
        clipped = clipped[:boundary].rstrip()
    return clipped + "…"


def _progress_bar(current: int, maximum: int, width: int = 20) -> str:
    """Return a text-based progress bar.

    Uses block characters — bracketed bars like '[####....]' are eaten by
    Rich console markup when DataTable cells render.
    """
    if maximum <= 0:
        filled = 0
    else:
        filled = int((current / maximum) * width)
    filled = min(filled, width)
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"{bar} {_fmt_number(current)}/{_fmt_number(maximum)}"


@dataclass
class MetricHistory:
    """Fixed-length history buffer for sparkline rendering."""
    max_length: int = 120
    rpm_samples: list[int] = field(default_factory=list)
    tpm_used_samples: list[int] = field(default_factory=list)
    queue_samples: list[int] = field(default_factory=list)
    upstream_latency_samples: list[float] = field(default_factory=list)

    def append(self, rpm: int, tpm_used: int, queue_depth: int, upstream_latency_ms: float = 0.0):
        self.rpm_samples.append(rpm)
        self.tpm_used_samples.append(tpm_used)
        self.queue_samples.append(queue_depth)
        self.upstream_latency_samples.append(upstream_latency_ms)
        if len(self.rpm_samples) > self.max_length:
            self.rpm_samples = self.rpm_samples[-self.max_length:]
        if len(self.tpm_used_samples) > self.max_length:
            self.tpm_used_samples = self.tpm_used_samples[-self.max_length:]
        if len(self.queue_samples) > self.max_length:
            self.queue_samples = self.queue_samples[-self.max_length:]
        if len(self.upstream_latency_samples) > self.max_length:
            self.upstream_latency_samples = self.upstream_latency_samples[-self.max_length:]


@dataclass
class LatencyTracker:
    """Tracks request latencies for percentile computation."""
    max_length: int = 1000
    latencies: list[float] = field(default_factory=list)
    _last_snapshot: list[float] = field(default_factory=list)

    def add(self, ms: float):
        self.latencies.append(ms)
        if len(self.latencies) > self.max_length:
            self.latencies = self.latencies[-self.max_length:]

    def merge_snapshot(self, snapshot: list[float]) -> None:
        """Count-based merge of a limiter status snapshot.

        status() publishes only the most recent samples, so consecutive
        snapshots overlap. Append just the samples beyond the overlap so the
        tracker accumulates a rolling window across polls instead of being
        replaced with a single last-100 slice.
        """
        prev = self._last_snapshot
        self._last_snapshot = list(snapshot)
        overlap = 0
        for m in range(min(len(prev), len(snapshot)), 0, -1):
            if prev[-m:] == snapshot[:m]:
                overlap = m
                break
        for value in snapshot[overlap:]:
            self.add(value)

    def _percentile(self, p: float) -> float:
        if not self.latencies:
            return 0.0
        sorted_vals = sorted(self.latencies)
        k = (len(sorted_vals) - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_vals[int(k)]
        return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)

    def p50(self) -> float:
        return round(self._percentile(50), 1)

    def p95(self) -> float:
        return round(self._percentile(95), 1)

    def p99(self) -> float:
        return round(self._percentile(99), 1)

    def avg(self) -> float:
        if not self.latencies:
            return 0.0
        return round(statistics.mean(self.latencies), 1)

    @staticmethod
    def _stats(values: list[float]) -> dict:
        """Percentile/avg stats over an arbitrary list of latency samples."""
        def pct(p: float) -> float:
            if not values:
                return 0.0
            sv = sorted(values)
            k = (len(sv) - 1) * (p / 100.0)
            f = math.floor(k)
            c = math.ceil(k)
            if f == c:
                return round(sv[int(k)], 1)
            return round(sv[f] * (c - k) + sv[c] * (k - f), 1)

        return {
            "count": len(values),
            "avg": round(statistics.mean(values), 1) if values else 0.0,
            "p50": pct(50),
            "p95": pct(95),
            "p99": pct(99),
        }


def _safe_update(fn: Callable) -> Callable:
    """Decorator that catches widget update errors without crashing the poller."""
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            fn(self, *args, **kwargs)
        except NoMatches:
            pass  # Widget not yet mounted or being destroyed
        except Exception:
            self._poll_error_count += 1
    return wrapper


class ProxyTUI(App):
    """Live dashboard for the DashScope API proxy."""

    CSS_PATH = "proxy_tui.tcss"

    # Keys that must be present in the per-provider status entry for metrics
    # updates. In multi-provider mode `_validate_status` is run against
    # `status["primary"]` (the flat RateLimiter.status() sub-dict); in single
    # mode it is run against the flat dict directly. So this set must exist in
    # BOTH the flat RateLimiter.status() shape and the multi-provider primary
    # entry (identical key sets). Defined at class level so it is testable
    # without instantiating the Textual App.
    _REQUIRED_STATUS_KEYS: frozenset[str] = frozenset({
        "rps_limit", "rpm_limit", "rpm_current",
        "tpm_limit", "tpm_available", "tpm_reserved",
        "total_forwarded", "queue_drops", "total_429s", "total_rejected",
        "total_tokens_consumed", "total_request_bytes", "total_response_bytes",
        "pending_requests", "circuit_open", "circuit_failure_count",
        "model_usage", "recent_latencies", "uptime_seconds",
    })

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "clear_logs", "Clear Logs"),
        ("1", "switch_to('tab-overview')", "Overview"),
        ("2", "switch_to('tab-logs')", "Logs"),
        ("3", "switch_to('tab-metrics')", "Metrics"),
        ("4", "switch_to('tab-models')", "Models"),
        ("5", "switch_to('tab-config')", "Config"),
    ]

    # Tab pane ids, declared so the gating table below can be checked against
    # the tabs that are actually composed rather than a second hand-kept list.
    EXPECTED_TAB_IDS: frozenset[str] = frozenset({
        "tab-overview", "tab-logs", "tab-metrics", "tab-models", "tab-config",
    })

    # Per-tick UI work, in dispatch order: (method name, required tab id,
    # takes the raw status). A target is dispatched only while its tab is the
    # ACTIVE one — this is the fix for "a log line jumps / the model table
    # snaps back": before tab gating, every tick rebuilt every table on every
    # tab eight times over, whether or not the user could see it.
    #
    # `None` means "always dispatch". `_poll_logs` is the one such target: it
    # is the only source of `#error-banner`'s `error_count`, so while the Logs
    # tab is hidden it still runs — as a count-only pass over the log stream
    # that never touches the widget. See `_scan_log_errors`.
    _POLL_UPDATERS: tuple[tuple[str, str | None, bool], ...] = (
        # First, so `#update_metrics` renders the banner from the same tick's
        # error count rather than the previous one.
        ("_poll_logs", None, False),
        ("_update_metrics", "tab-overview", True),
        ("_update_provider_grid", "tab-overview", True),
        ("_update_sparklines", "tab-metrics", False),
        ("_update_failover_panel", "tab-metrics", False),
        ("_update_latency_histogram", "tab-metrics", False),
        ("_update_model_table", "tab-models", True),
        ("_update_config_table_filtered", "tab-config", False),
    )

    def __init__(
        self,
        rate_limiter: RateLimiter,
        tui_log_handler: TUILogHandler,
        proxy_app,
    ):
        super().__init__()
        self.rate_limiter = rate_limiter
        self.log_handler = tui_log_handler
        self.proxy_app = proxy_app
        self.history = MetricHistory()
        self.latency_tracker = LatencyTracker()
        # Per-provider latency trackers (multi-provider mode). In multi mode the
        # polled top-level `recent_latencies` is a CONCATENATION of per-provider
        # time-ordered windows, so a single merge_snapshot over it double-counts.
        # Instead each provider's own time-ordered window feeds its own tracker;
        # display math is computed over the UNION of all trackers' values. In
        # single-provider mode the dict holds one "primary" entry, so the code
        # paths are uniform.
        self._latency_trackers: dict[str, LatencyTracker] = {"primary": self.latency_tracker}
        self._config_rows: list[tuple[str, str, str, str]] = []

        # Logs tab state
        self._logs_paused: bool = False
        self._autoscroll_enabled: bool = True
        self._log_seqs: dict[str, int] = {}
        self._displayed_log_count: int = 0
        # How far the COUNT-ONLY pass has walked the log stream while the Logs
        # tab is hidden. It runs its own cursor precisely so it can keep
        # `error_count` truthful WITHOUT advancing the widget cursor
        # `_log_seqs`, which is what would otherwise consume the backlog the
        # user is about to come back and read. Always >= the widget cursor.
        self._error_scanned_seq: int = 0

        # Models tab state
        self._model_filter: str = ""
        self._model_sort_key: str = "requests"

        # Config tab state
        self._config_filter: str = ""
        self._config_grouped: bool = False
        # `_config_rows` only changes when the user presses Refresh, so the
        # per-poll rebuild of ~92 rows (109 grouped) is skipped while clean.
        self._config_dirty: bool = True

        # Cancellable sleep for the poll worker
        self._shutdown_event = threading.Event()
        # Set by a tab switch to cut the poller's sleep short; the adaptive
        # interval backs off to 16 s, which is far too long to stare at stale
        # data after switching tabs.
        self._refresh_wake = threading.Event()

        # Poller state
        self.error_count = 0
        self._poll_error_count = 0

        # Active tab (`TabbedContent.TabActivated`) plus a ready flag: the
        # initial TabActivated can arrive before the DataTable columns are
        # added in `on_mount`, so no immediate repaint happens until then.
        # NOT named `_ready`: that is an existing `async App._ready()` hook and
        # shadowing it with a bool breaks Textual's startup.
        self._active_tab: str = "tab-overview"
        self._ui_ready: bool = False

        # Overview provider grid state (row order -> provider key mapping)
        self._grid_row_keys: list[str] = []
        self._detail_provider_key: str = "primary"
        # Set across the grid's clear()/rebuild so the spurious RowHighlighted
        # that `clear()` posts for row 0 cannot repaint the detail pane with
        # the wrong provider before the cursor is restored.
        self._suppress_detail_follow: bool = False
        self._last_raw_status: dict | None = None

        # Incremental session-log tail (offset/inode + last 200 parsed entries)
        self._session_log_offset = 0
        self._session_log_inode = None
        self._session_log_path: str | None = None
        self._session_log_tail: deque[dict] = deque(maxlen=200)
        self._session_log_partial = ""
        # One read per poll, shared by `#alert-badge` (Overview) and the
        # failover panel (Metrics). Keyed on the poll generation rather than
        # a wall clock, so "once per poll" is exact.
        self._poll_generation: int = 0
        self._generation_lock = threading.Lock()
        self._session_log_gen: int = -1
        self._session_log_cache: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="main-tabs", initial="tab-overview"):
            # Tab 1: Overview (primary operational dashboard)
            # A single full-width column: the provider grid needs the whole
            # terminal width, which is what removed the old 56-cell budget.
            # The WARNING/ERROR feed that used to live here (`#live-log`) is
            # now on the Logs tab, which already shows every level with
            # level/text/time filters; `#alert-badge` and `#error-banner`
            # keep the at-a-glance alerts on Overview.
            with TabPane("Overview", id="tab-overview"):
                with Vertical(id="overview-panel"):
                    yield Static("Proxy: Checking...", id="status-indicator")
                    yield Static("", id="alert-badge", classes="alert-badge")
                    yield Static("", id="error-banner", classes="error-banner")
                    yield DataTable(id="provider-grid")
                    # Detail pane: full breakdown for the provider under the grid cursor
                    yield Static("", id="provider-detail-title", classes="section-title")
                    yield DataTable(id="provider-detail")

                    yield Static("", classes="spacer")
                    # No "Request Statistics" title above this: the strip is
                    # self-describing (every value carries its own label), so
                    # the heading was a row of pure chrome.
                    yield Static("", id="request-stats", classes="stats-strip")

            # Tab 2: Logs (moved from position 3 - critical for debugging)
            with TabPane("Logs", id="tab-logs"):
                with Vertical(id="logs-tab-content"):
                    yield Horizontal(
                        Input(placeholder="Filter logs by text...", id="log-filter"),
                        Select(
                            [("All Levels", "ALL"), ("INFO", "INFO"), ("WARNING", "WARNING"), ("ERROR", "ERROR")],
                            value="ALL",
                            id="log-level-filter",
                            allow_blank=False,
                        ),
                        Select(
                            [("All Time", "ALL"), ("Last 5 min", "5m"), ("Last 1 hour", "1h"), ("Last 24 hours", "24h")],
                            value="ALL",
                            id="log-time-range",
                            allow_blank=False,
                        ),
                        Button("Clear", id="clear-logs-btn", variant="error"),
                        id="filter-bar",
                    )
                    yield Horizontal(
                        Button("Pause", id="pause-logs-btn", variant="default"),
                        Button("Export", id="export-logs-btn", variant="default"),
                        Button("Auto-scroll: ON", id="autoscroll-btn", variant="default"),
                        Static("", id="log-entry-count", classes="entry-count"),
                        id="log-controls-bar",
                    )
                    yield Log(id="live-log-full", max_lines=LOG_BUFFER_SIZE)

            # Tab 3: Metrics (sparklines + latency percentiles)
            with TabPane("Metrics", id="tab-metrics"):
                with Vertical(id="metrics-tab-content"):
                    yield Horizontal(
                        Static("RPM", id="rpm-sparkline-label", classes="sparkline-label"),
                        Sparkline([], id="rpm-sparkline"),
                        Static("TPM Used", id="tpm-sparkline-label", classes="sparkline-label"),
                        Sparkline([], id="tpm-sparkline"),
                        Static("Queue", id="queue-sparkline-label", classes="sparkline-label"),
                        Sparkline([], id="queue-sparkline"),
                        # Kept short enough to fit .sparkline-label's width on
                        # one row; "Upstream Latency" wrapped to two rows.
                        Static("Latency", id="upstream-latency-sparkline-label", classes="sparkline-label"),
                        Sparkline([], id="upstream-latency-sparkline"),
                        id="sparkline-row",
                    )
                    # `hidden` at compose time, and toggled together with
                    # `#failover-events-panel`: the title used to float above an
                    # empty region on the Metrics tab whenever nothing had
                    # failed over. `.panel-title` keeps `dock: top` and is the
                    # ONLY docked widget in this container — a second docked
                    # sibling would land in the SAME row and paint over it.
                    yield Static("Failover Events", id="failover-title",
                                 classes="panel-title failover-title hidden")
                    # These three start `hidden`: they are empty at compose
                    # time, and only their updaters add the content that should
                    # reveal them. Without this they render as empty boxes
                    # until the first poll touches them.
                    yield Static("", id="failover-events-panel",
                                 classes="failover-panel hidden")
                    yield Static("", id="latency-histogram-panel",
                                 classes="histogram-panel hidden")
                    yield Static("", id="poll-error-banner",
                                 classes="poll-error hidden")

            # Tab 4: Models (per-model breakdown with sort/filter)
            with TabPane("Models", id="tab-models"):
                with Vertical(id="models-tab-content"):
                    yield Horizontal(
                        Input(placeholder="Filter models by name...", id="model-filter"),
                        Select(
                            [("Sort: Requests", "requests"), ("Sort: Tokens", "tokens"), ("Sort: Latency", "latency"), ("Sort: 429s", "429s")],
                            value="requests",
                            id="model-sort",
                            allow_blank=False,
                        ),
                        id="model-controls",
                    )
                    yield DataTable(id="model-usage-table")

            # Tab 5: Config (config viewer with filter and grouped view)
            with TabPane("Config", id="tab-config"):
                with Vertical(id="config-tab-content"):
                    yield Horizontal(
                        Input(placeholder="Filter config keys...", id="config-filter"),
                        Button("Grouped", id="config-grouped-btn", variant="default"),
                        Button("Refresh", id="config-refresh-btn", variant="default"),
                        id="config-controls",
                    )
                    yield DataTable(id="config-table")

        yield Footer()

    async def on_mount(self) -> None:
        """Initialize data tables and start background polling."""
        self._config_rows = _load_display_config()

        # A typo in a `_POLL_UPDATERS` tab id would silently disable that
        # updater for the life of the app, which is invisible — the panel just
        # never updates. Fail loudly instead.
        composed = {
            str(pane.id) for pane in self.query(TabPane) if pane.id
        }
        missing = sorted(self.EXPECTED_TAB_IDS - composed)
        if missing:
            raise ValueError(f"composed tab panes missing: {missing}")
        for method_name, tab_id, _takes_status in self._POLL_UPDATERS:
            if tab_id is not None and tab_id not in composed:
                raise ValueError(
                    f"_POLL_UPDATERS entry {method_name!r} names tab "
                    f"{tab_id!r}, which is not composed"
                )

        # Configure the compact provider grid.
        #
        # `#overview-panel` is now full width (`width: 1fr`), so there is no
        # longer a 56-cell budget to squeeze against — at 120 columns the grid
        # has 116 cells and the 56 used here fit with ~60 to spare. The columns
        # are deliberately NOT grown to fill that space: widening the bars is
        # a `GRID_BAR_WIDTH` change, and that constant is out of scope here.
        #
        # `cell_padding = 0` is kept, so there is still NO implicit gap between
        # columns and every width must be sized to the WORST case of the
        # strings tui_status emits PLUS one cell of trailing slack — without
        # that slack adjacent cells run together and read as one token
        # ("Agnes Imageok", "111.1K63"):
        #   Provider  12  longest PROVIDER_LABELS entry is "Agnes Image"/"Video"
        #                      (11 chars); +1 so the circuit word never runs on
        #   Circuit    8  header "Circuit" (7); state words are <= 4 chars
        #   RPM/TPM   12  tui_status.GRID_BAR_CELL_MAX (11) + 1; worst case is
        #                      the over-quota mark + full bar + " " + "999" + "%"
        #   Fwd/429    7  _compact_number, e.g. "999.9K" / "999.9M" (6 chars)
        grid = self.query_one("#provider-grid", DataTable)
        grid.cell_padding = 0
        grid.add_column("Provider", width=12)
        grid.add_column("Circuit", width=8)
        grid.add_column("RPM", width=12)
        grid.add_column("TPM", width=12)
        grid.add_column("Fwd", width=7)
        grid.add_column("429", width=7)
        grid.show_header = True
        grid.zebra_stripes = True
        grid.cursor_type = "row"

        # Configure the provider detail pane (full breakdown of selected provider)
        detail_table = self.query_one("#provider-detail", DataTable)
        detail_table.add_column("Metric", width=14)
        detail_table.add_column("Value")
        detail_table.show_header = False
        detail_table.zebra_stripes = True

        # Configure model usage table
        try:
            model_table = self.query_one("#model-usage-table", DataTable)
            model_table.add_columns("Model", "Providers", "Requests", "Tokens", "429s", "Avg Latency", "p50", "p95")
            model_table.show_header = True
            model_table.zebra_stripes = True
        except NoMatches:
            pass

        # Configure config table
        try:
            config_table = self.query_one("#config-table", DataTable)
            config_table.add_columns("Key", "Value", "Source")
            config_table.show_header = True
            config_table.zebra_stripes = True
            self._populate_config_table(config_table)
        except NoMatches:
            pass

        # Start background polling in a thread
        self.run_worker(self._poll_loop, exclusive=True, thread=True, description="metrics poller")
        # Tables are configured now, so a tab switch may repaint immediately.
        self._ui_ready = True

    def _populate_config_table(self, table: DataTable) -> None:
        """Populate the config table with current configuration."""
        self._config_dirty = True
        self._update_config_table_filtered()

    def _dispatch_poll_updates(self, raw_status: dict) -> int:
        """Schedule this tick's UI updates, skipping tabs the user cannot see.

        Returns how many updates were scheduled. That count IS the churn the
        poller causes, and it is the number that has to come down: eight widget
        rebuilds per tick used to run regardless of which tab was active, so a
        hidden table's `clear()`/`add_row()` cycle could still yank the visible
        one's layout around.
        """
        scheduled = 0
        for method_name, tab_id, takes_status in self._POLL_UPDATERS:
            if tab_id is not None and tab_id != self._active_tab:
                continue
            method = getattr(self, method_name)
            if takes_status:
                self.call_from_thread(method, raw_status)
            else:
                self.call_from_thread(method)
            scheduled += 1
        return scheduled

    def _bump_poll_generation(self) -> int:
        """Open a new poll generation, invalidating the session-log snapshot."""
        with self._generation_lock:
            self._poll_generation += 1
            return self._poll_generation

    def _refresh_visible_tab(self) -> None:
        """Repaint the active tab from the newest published status.

        Same updater list as a poll tick, restricted to the active tab, so a
        tab switch never lands on a half-rendered panel. Status-driven
        updaters are skipped until the poller has published a payload —
        `_last_raw_status` is None before the first tick, and handing None to
        `_update_metrics` would trip the poll-error banner.
        """
        status = self._last_raw_status
        self._bump_poll_generation()
        try:
            for method_name, tab_id, takes_status in self._POLL_UPDATERS:
                if tab_id is not None and tab_id != self._active_tab:
                    continue
                if takes_status and status is None:
                    continue
                method = getattr(self, method_name)
                method(status) if takes_status else method()
        except NoMatches:
            pass
        except Exception:
            self._poll_error_count += 1

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Track the active tab and repaint it without waiting for a poll.

        The adaptive interval backs off to 16 s (and to 5 s after five
        consecutive errors), so a tab switch cannot be left waiting for the
        next tick. Two things happen here: `_refresh_wake` cuts the poller's
        sleep short so fresh data is fetched now, and the newly-visible tab's
        updaters run immediately from the newest published status so the panel
        is never blank or stale in the meantime.
        """
        self._active_tab = event.pane.id or "tab-overview"
        self._refresh_wake.set()
        if self._ui_ready:
            self._refresh_visible_tab()

    def _poll_loop(self) -> None:
        """Threaded loop that polls rate limiter and log handler with adaptive timing."""
        worker = get_current_worker()
        base_interval = 1.0
        consecutive_errors = 0
        max_consecutive_errors = 5

        while not worker.is_cancelled:
            interval = base_interval
            try:
                # Respect cancellation even during sleep
                if worker.is_cancelled:
                    break
                raw_status = self.rate_limiter.status()
                from tui_status import series_from_status
                series = series_from_status(raw_status)
                avg_upstream = statistics.mean(series["recent_latencies"]) if series["recent_latencies"] else 0.0
                self.history.append(
                    rpm=series["rpm"],
                    tpm_used=series["tpm_used"],
                    queue_depth=series["queue_depth"],
                    upstream_latency_ms=avg_upstream,
                )
                self._feed_latency_trackers(raw_status)
                # Cache the raw status here, on the poll thread, rather than
                # inside `_update_provider_grid`: the Models tab's filter/sort
                # handlers reuse it, and they must keep working while the
                # Overview tab (the grid) is not being repainted.
                self._last_raw_status = raw_status

                # Refresh only what the user can actually see.
                self._bump_poll_generation()
                self._dispatch_poll_updates(raw_status)

                consecutive_errors = 0
                # Polling recovered: clear any stale poll-error banner so the
                # tcss `.poll-error:empty` hide rule applies again.
                if self._poll_error_count > 0:
                    self._poll_error_count = 0
                    try:
                        self.call_from_thread(self._clear_poll_error)
                    except Exception:
                        pass

                # Adaptive: poll faster when activity detected
                if series["queue_depth"] > 0:
                    interval = 0.5

            except Exception:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    try:
                        self.call_from_thread(self._show_poll_error)
                    except Exception:
                        pass
                    interval = base_interval * 5
                else:
                    interval = base_interval * (2 ** consecutive_errors)

            # Cancellable sleep (worker.cancel() won't interrupt time.sleep, so
            # we wait on an event instead). `_refresh_wake` is set by a tab
            # switch so the wait ends early instead of stranding the
            # newly-visible tab on stale data for up to a full interval.
            self._refresh_wake.wait(timeout=interval)
            self._refresh_wake.clear()
            if self._shutdown_event.is_set():
                break

    def _show_poll_error(self) -> None:
        """Display a transient error banner when polling fails."""
        try:
            banner = self.query_one("#poll-error-banner", Static)
            banner.update(f"Poll error ({self._poll_error_count} errors) -- reconnecting...")
            banner.set_class(False, "hidden")
        except NoMatches:
            pass

    def _clear_poll_error(self) -> None:
        """Clear the poll-error banner once polling recovers.

        Visibility is an explicit `.hidden` class, NOT the tcss `:empty`
        pseudo-class: in Textual `:empty` means "has no child nodes", which is
        permanently true for a Static, so a `:empty` rule hid this banner
        FOREVER and made the whole show/hide path dead code.
        """
        try:
            banner = self.query_one("#poll-error-banner", Static)
            banner.update("")
            banner.set_class(True, "hidden")
        except NoMatches:
            pass

    def _feed_latency_trackers(self, raw_status: dict) -> None:
        """Feed the per-provider latency trackers from the polled status.

        In multi-provider mode `raw_status` maps each provider key to its own
        time-ordered `recent_latencies` window, so each window feeds a
        dedicated tracker (merge_snapshot is correct on a single time-ordered
        window). In single-provider (flat) mode the dict holds one "primary"
        tracker fed from the flat `recent_latencies`.
        """
        from tui_status import is_multi_provider, PROVIDER_KEYS

        if is_multi_provider(raw_status):
            for key in PROVIDER_KEYS:
                p = raw_status.get(key)
                if not isinstance(p, dict):
                    continue
                tracker = self._latency_trackers.setdefault(key, LatencyTracker())
                tracker.merge_snapshot(p.get("recent_latencies") or [])
        else:
            tracker = self._latency_trackers.setdefault("primary", self.latency_tracker)
            tracker.merge_snapshot(raw_status.get("recent_latencies") or [])

    def _latency_union_values(self) -> list[float]:
        """Union of all per-provider tracker values (fresh, display-only).

        No persistent concatenated deque is kept — the union is computed on
        demand for the histogram/percentile display math.
        """
        union: list[float] = []
        for tracker in self._latency_trackers.values():
            union.extend(tracker.latencies)
        return union

    def _latency_union_stats(self) -> dict:
        """Display percentiles/avg/histogram over the union of all trackers."""
        return LatencyTracker._stats(self._latency_union_values())

    @_safe_update
    def _update_metrics(self, status: dict) -> None:
        """Refresh the metrics DataTable widgets (Overview tab).

        In multi-provider mode, `status` is raw_status from
        MultiProviderRateLimiter.status() which has {"primary": {...},
        "secondary": {...}, ...} plus aggregated totals at the top level.
        In single-provider mode, `status` is a flat RateLimiter status dict.
        """
        # Detect multi-provider mode
        if "primary" in status:
            primary = status["primary"]
            multi = status  # aggregated totals live at top level
        else:
            primary = status
            multi = status

        missing = self._missing_status_keys(primary)
        if missing:
            # Never leave the strip silently blank: the grid still populates
            # from the provider sub-dicts, so an unvalidated payload produced a
            # half-rendered Overview with no explanation.
            self.query_one("#status-indicator", Static).update("Proxy: status unavailable")
            strip = self.query_one("#request-stats", Static)
            strip.set_class(True, "stats-error")
            strip.update("Incomplete proxy status — missing: " + ", ".join(sorted(missing)))
            return

        # Update connection status indicator
        status_widget = self.query_one("#status-indicator", Static)
        session = self.proxy_app.get("client_session")
        is_running = session is not None and not session.closed
        uptime = primary.get("uptime_seconds", 0)
        status_text = f"Proxy: Running (uptime: {self._format_uptime(uptime)})" if is_running else "Proxy: Stopped"
        status_widget.update(status_text)
        status_widget.set_class(is_running, "status-running")
        status_widget.set_class(not is_running, "status-stopped")

        # Update alert badge (checks all providers in multi-provider mode)
        self._update_alert_badge(status)

        # Aggregate totals across all providers, as a compact 2-line strip.
        # Per-provider forwarded/429 counts live in the compact provider grid.
        from tui_status import overview_request_stats
        stats = overview_request_stats(status)
        strip = self.query_one("#request-stats", Static)
        strip.set_class(False, "stats-error")
        strip.update(_format_stat_strip(stats, multi, primary))

        # Update error banner. Visibility is a toggled class, not `:empty`:
        # Textual's `:empty` is always true for a Static (it has no child
        # nodes), so a `:empty` rule would hide the banner permanently.
        error_banner = self.query_one("#error-banner", Static)
        if self.error_count > 0:
            error_banner.update(f"{self.error_count} error(s) in recent logs")
            error_banner.set_class(False, "hidden")
        else:
            error_banner.update("")
            error_banner.set_class(True, "hidden")

    def _snapshot_table_view(self, table: DataTable) -> tuple[int, int, int, int]:
        """Capture a table's cursor coordinate and scroll offset.

        `DataTable.clear()` resets BOTH: it assigns `cursor_coordinate` to
        (0, 0) and `scroll_x`/`scroll_y` to 0. Every table in this app is
        rebuilt with `clear()` + `add_row()` on each poll, which is exactly the
        "I'm scrolling the model table and it snaps back" symptom — nothing
        about the rebuild was wrong, the user's position was simply thrown away.
        """
        try:
            coordinate = table.cursor_coordinate
        except Exception:
            coordinate = Coordinate(0, 0)
        try:
            offset = table.scroll_offset
            x, y = offset.x, offset.y
        except Exception:
            x = y = 0
        return (int(coordinate.row), int(coordinate.column), int(x), int(y))

    def _restore_table_view(
        self,
        table: DataTable,
        snapshot: tuple[int, int, int, int],
        *,
        row: int | None = None,
    ) -> None:
        """Restore a `_snapshot_table_view` snapshot after a rebuild.

        `row` overrides the snapshot's row — the provider grid uses it to pin
        the cursor to whichever provider the detail pane is showing, which is
        keyed by provider rather than by position. Coordinates are clamped to
        the rebuilt table so a shrinking table cannot leave the cursor or the
        scroll past the end.

        `move_cursor(scroll=False)` is deliberate: its own scroll-into-view
        would fight the restore, and while `_require_update_dimensions` is set
        it defers that scroll to `call_after_refresh`, which would then land
        AFTER this method and undo it.

        `set_scroll` is used rather than `scroll_to` because it performs no
        validation: right after a rebuild `virtual_size` is still the OLD
        value (`clear()` sets `_require_update_dimensions` and defers
        `_update_dimensions` to `on_idle`), so a validating scroll would clamp
        against stale dimensions and drop the restore.
        """
        snap_row, snap_column, x, y = snapshot
        if table.row_count:
            target_row = snap_row if row is None else row
            target_row = max(0, min(target_row, table.row_count - 1))
            columns = len(table.columns)
            target_column = 0 if columns <= 0 else max(0, min(snap_column, columns - 1))
            table.move_cursor(row=target_row, column=target_column, scroll=False)
        table.set_scroll(x=x, y=y)
        # Re-apply from a callback scheduled LAST. `clear()` assigns
        # `cursor_coordinate` to (0, 0) and the watcher posts a DEFERRED
        # `_scroll_cursor_into_view`, which lands after this method and yanks
        # the table straight back down to the cursor row — so a synchronous
        # restore alone loses the fight, and `move_cursor(scroll=False)` only
        # suppresses its own half of it. `call_after_refresh` is FIFO, so
        # scheduling from here puts the restore behind every pending
        # scroll-into-view.
        table.call_after_refresh(
            lambda: self._restore_table_scroll(table, x, y)
        )

    @staticmethod
    def _restore_table_scroll(table: DataTable, x: int, y: int) -> None:
        """Re-apply a scroll offset after the deferred cursor scrolls.

        By the time this runs the rebuilt rows have been measured, so the
        offset can also be clamped to what the table actually holds — a table
        that shrank must not be left scrolled past its end, rendering blank.
        """
        limit_y = max(0, table.virtual_size.height - table.container_size.height)
        limit_x = max(0, table.virtual_size.width - table.container_size.width)
        table.set_scroll(x=min(x, limit_x), y=min(y, limit_y))

    def _palette(self) -> dict[str, str]:
        """Resolved CSS variables, cached once per app.

        `get_css_variables()` rebuilds the theme's whole colour system on every
        call, so it must not run per cell (the grid renders ~11 rows x 6
        columns on every poll).
        """
        cached = getattr(self, "_palette_cache", None)
        if cached is None:
            cached = self._palette_cache = self.get_css_variables()
        return cached

    def _load_style(self, level: str) -> str:
        """Style string for a load level, built from the app palette.

        `tui_status` reports the level as plain data and this module owns all
        styling — that split is what keeps `tui_status` free of Rich/Textual
        imports and therefore unit-testable.
        """
        palette = self._palette()
        if level == "over":
            return f"bold {palette['load-over']}"
        if level == "high":
            return f"bold {palette['load-high']}"
        if level == "warm":
            return palette["load-warm"]
        return ""

    def _bar_cell(self, bar: str, level: str) -> Content:
        """Render a usage bar into a styled DataTable cell.

        `Content` is Textual's own cell primitive — importing `rich.text.Text`
        directly would break the repo's "rich is never imported directly"
        contract. CSS classes are not an option instead: a DataTable renders
        its cells itself, so there is no element to hang a class on.
        """
        style = self._load_style(level)
        return Content(bar).stylize(style) if style else Content(bar)

    def _grid_cell(self, text: str, dim: bool = False) -> Content:
        """Render a plain grid cell, dimming it when its row is idle.

        `provider_grid_rows` already computes and returns `idle`; it used to be
        discarded, which made an idle provider look exactly like an active one
        sitting at 0%.
        """
        if not dim:
            return Content(text)
        return Content(text).stylize(f"dim {self._palette()['load-idle']}")

    # CSS variables are resolved once here and referenced from the tcss, so the
    # palette lives in one place instead of as scattered literals.
    #
    # Two Textual facts this depends on, both verified against 8.2.8:
    #   - `get_css_variables` REPLACES the built-in set, so the base must be
    #     merged via super() or `App.DEFAULT_CSS` fails on `$background`.
    #   - keys in that mapping have NO leading `$`.
    #   - a variable's value may not itself be a `$reference`, so these alias
    #     the already-RESOLVED theme colours. That keeps them theme-aware (a
    #     light theme gets light-theme severities) while still centralising them.
    def get_css_variables(self) -> dict[str, str]:
        base = super().get_css_variables()
        base.update({
            # Load bars in the provider grid (keys match tui_status.LOAD_*).
            "load-ok": base["success"],
            "load-warm": base["warning"],
            "load-high": base["warning"],
            "load-over": base["error"],
            # An idle provider: present, configured, but carrying no traffic.
            "load-idle": base["text-muted"],
            # Panel borders. Colour carries meaning only: warning = "events in
            # here worth reading", error = "something is broken".
            "panel-border-warning": base["warning"],
            "panel-border-error": base["error"],
        })
        return base

    @_safe_update
    def _update_provider_grid(self, status: dict) -> None:
        """Populate the compact provider grid (one row per provider).

        Uses the pure `provider_grid_rows` helper from tui_status, then keeps
        a row-index -> provider-key mapping for the detail pane cursor follow.

        The cursor coordinate and scroll offset are snapshotted before
        `clear()` and restored after the rebuild, because `clear()` resets
        both. The restore is anchored on the PROVIDER KEY rather than the row
        index, so the detail pane stays on the provider the user is looking at
        even if the row order shifts.

        `clear()` also assigns `cursor_coordinate` to (0, 0), and Textual
        posts `RowHighlighted` whenever that value actually changes. Without
        the `_suppress_detail_follow` guard, a cursor parked anywhere but row 0
        made the detail pane flash the FIRST provider on every single poll.
        """
        from tui_status import provider_grid_rows

        self._last_raw_status = status
        rows = provider_grid_rows(status)
        if not rows and "rpm_limit" in status:
            # Single-provider (flat) status: synthesize the primary row.
            rows = provider_grid_rows({"primary": status})

        grid = self.query_one("#provider-grid", DataTable)
        view = self._snapshot_table_view(grid)
        # The provider the detail pane is currently showing.
        focused_key = self._detail_provider_key

        self._suppress_detail_follow = True
        try:
            grid.clear()
            self._grid_row_keys = []
            for row in rows:
                # Every cell of an idle row dims together — leaving the circuit
                # column bright made a dimmed row look half-rendered.
                idle = row["idle"]
                grid.add_row(
                    self._grid_cell(row["label"], dim=idle),
                    self._grid_cell(row["circuit"], dim=idle),
                    self._bar_cell(row["rpm"], row["rpm_level"]),
                    self._bar_cell(row["tpm"], row["tpm_level"]),
                    self._grid_cell(row["forwarded"], dim=idle),
                    self._grid_cell(row["count_429s"], dim=idle),
                )
                self._grid_row_keys.append(row["key"])

            if not self._grid_row_keys:
                return

            if focused_key in self._grid_row_keys:
                target_row = self._grid_row_keys.index(focused_key)
            else:
                # The provider the user was viewing is gone this poll; stay on
                # the row they were on (clamped) rather than jumping to row 0.
                target_row = view[0]
            self._restore_table_view(grid, view, row=target_row)
            # Restore made the cursor move, which Textual would normally turn
            # into a RowHighlighted -> `_update_provider_detail`. Do that work
            # once, here, with the provider already resolved.
            self._detail_provider_key = self._grid_row_keys[target_row]
        finally:
            self._suppress_detail_follow = False

        self._update_provider_detail(status)

    @_safe_update
    def _update_provider_detail(self, status: dict) -> None:
        """Render the detail pane for the provider under the grid cursor."""
        from tui_status import CIRCUIT_OK, CIRCUIT_OPEN, circuit_glyph

        key = self._detail_provider_key
        title = self.query_one("#provider-detail-title", Static)
        detail_table = self.query_one("#provider-detail", DataTable)
        view = self._snapshot_table_view(detail_table)
        detail_table.clear()

        provider_status = status.get(key) if isinstance(status, dict) else None
        if not isinstance(provider_status, dict):
            title.update("Provider Detail")
            self._restore_table_view(detail_table, view)
            return

        label = next((p["label"] for p in PROVIDER_REGISTRY if p["key"] == key), key)
        # Single source of truth: the grid cell and this pane must never
        # disagree, so both go through `circuit_glyph` rather than each
        # re-branching on the raw status keys.
        circuit = circuit_glyph(provider_status)
        title.update(f"{label} — {circuit}")

        detail_table.add_row("RPS Limit", str(provider_status.get("rps_limit", 0)))
        detail_table.add_row(
            "RPM",
            _progress_bar(provider_status.get("rpm_current", 0), provider_status.get("rpm_limit", 1)),
        )
        detail_table.add_row(
            "TPM Available",
            _progress_bar(
                provider_status.get("tpm_available", 0),
                provider_status.get("tpm_limit", 1),
            ),
        )

        # Circuit breaker status (conditional, like the old per-provider tables).
        # Same word as the grid cell, plus the failure count, which is the only
        # place the raw count is surfaced. Omitted entirely when healthy.
        failures = provider_status.get("circuit_failure_count", 0)
        if circuit == CIRCUIT_OPEN:
            detail_table.add_row("Circuit", f"{circuit} (failures: {failures})")
        elif circuit != CIRCUIT_OK:
            detail_table.add_row("Circuit", f"{circuit} ({failures} failures)")

        detail_table.add_row(
            "Tokens",
            f"{_fmt_number(provider_status.get('total_tokens_consumed', 0))} consumed | "
            f"{_fmt_number(provider_status.get('tpm_reserved', 0))} reserved | "
            f"{_fmt_number(provider_status.get('tpm_limit', 0))} capacity",
        )
        detail_table.add_row(
            "Forwarded",
            f"{_fmt_number(provider_status.get('total_forwarded', 0))} | "
            f"429s: {provider_status.get('total_429s', 0)} | "
            f"Rejected: {provider_status.get('total_rejected', 0)}",
        )

        warning = self._quota_warning(provider_status)
        if warning:
            detail_table.add_row("Warning", warning)

        self._restore_table_view(detail_table, view)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Follow the provider-grid cursor: re-render the detail pane."""
        if event.data_table.id != "provider-grid":
            return
        if self._suppress_detail_follow:
            # Raised by `clear()`/cursor restore inside `_update_provider_grid`,
            # which renders the pane itself from the restored row. Acting on it
            # here would paint the pane twice per poll and, mid-rebuild, with
            # the previous row's provider.
            return
        row = event.cursor_row
        if 0 <= row < len(self._grid_row_keys):
            self._detail_provider_key = self._grid_row_keys[row]
            status = self._last_raw_status
            if status:
                self._update_provider_detail(status)

    @_safe_update
    def _update_sparklines(self) -> None:
        """Update sparkline widgets in Metrics tab."""
        try:
            rpm_sparkline = self.query_one("#rpm-sparkline", Sparkline)
            rpm_sparkline.data = self.history.rpm_samples
        except NoMatches:
            pass

        try:
            tpm_sparkline = self.query_one("#tpm-sparkline", Sparkline)
            tpm_sparkline.data = self.history.tpm_used_samples
        except NoMatches:
            pass

        try:
            queue_sparkline = self.query_one("#queue-sparkline", Sparkline)
            queue_sparkline.data = self.history.queue_samples
        except NoMatches:
            pass

        try:
            upstream_sparkline = self.query_one("#upstream-latency-sparkline", Sparkline)
            upstream_sparkline.data = self.history.upstream_latency_samples
        except NoMatches:
            pass

    def _check_quota_thresholds(self, status: dict) -> list[str]:
        """Return list of quota names at >= 90% usage."""
        warnings = []
        thresholds = [
            ("RPM", status.get("rpm_current", 0), status.get("rpm_limit", 1)),
            ("TPM", status.get("tpm_limit", 0) - status.get("tpm_available", 0) - status.get("tpm_reserved", 0), status.get("tpm_limit", 1) - status.get("tpm_reserved", 0)),
        ]
        for name, used, limit in thresholds:
            if limit > 0 and used / limit >= 0.9:
                warnings.append(name)
        return warnings

    def _quota_warning(self, status: dict) -> str:
        """Return a warning string if any quota is near its limit."""
        warnings = self._check_quota_thresholds(status)
        if warnings:
            return "WARNING: " + ", ".join(warnings) + " near limit"
        return ""

    def _missing_status_keys(self, status: dict) -> set[str]:
        """Return the `_REQUIRED_STATUS_KEYS` absent from `status` (empty = ok).

        Split out from `_validate_status` so callers can NAME the missing keys
        in the UI instead of just returning and leaving a stale screen behind.
        """
        if not isinstance(status, dict):
            return set(self._REQUIRED_STATUS_KEYS)
        return self._REQUIRED_STATUS_KEYS - status.keys()

    def _validate_status(self, status: dict) -> bool:
        """Check that the status dict has all required keys."""
        return not self._missing_status_keys(status)

    def _poll_logs(self, *, visible: bool | None = None) -> None:
        """Append new log entries to the Logs tab's Log widget.

        This is the ONLY log feed: Overview's WARNING/ERROR-only `#live-log`
        was removed when the Overview became a single full-width column. The
        Logs tab shows every level and can be filtered down to WARNING/ERROR,
        so nothing is lost — and this feed is also what counts errors for
        `#error-banner`.

        Dispatched on every tick (it is the banner's only `error_count`
        source), but it does almost nothing while the Logs tab is hidden:
        `_scan_log_errors` counts ERRORs without writing or advancing the
        widget's sequence cursor, so the backlog is still complete and in
        order when the user comes back. `visible` is an override for the
        filter handlers, which re-drain from seq 0 and must reach the widget.
        """
        if self._logs_paused:
            return
        if visible is None:
            visible = self._logs_tab_visible
        if visible:
            written = self._append_logs_to_widget("#live-log-full", apply_filter=True, count_errors=True)
            self._displayed_log_count += written
            self._update_log_entry_count()
        else:
            self._scan_log_errors()

    @property
    def _logs_tab_visible(self) -> bool:
        """True when the Logs tab is the active tab."""
        return self._active_tab == "tab-logs"

    def _scan_log_errors(self) -> None:
        """Count ERROR entries without touching the Logs widget.

        `#error-banner` reports `error_count`, which used to be accumulated as a
        side effect of writing log lines into `#live-log-full`. Once the drain
        is gated on the Logs tab being visible, that side effect stops while
        the tab is hidden and the banner would sit frozen and then jump — so
        the count gets its own pass.

        It walks its OWN cursor, `_error_scanned_seq`, and deliberately never
        advances `_log_seqs`: that cursor is what the widget drain resumes
        from, and consuming it here is exactly how the hidden backlog would be
        lost. `_error_scanned_seq` stays at or ahead of the widget cursor, and
        the visible drain skips entries below it, so no ERROR is ever counted
        twice.

        Bounded to one page per poll: the work is O(page), not O(backlog), and
        a backlog still catches up at LOG_DRAIN_PAGE_SIZE entries per tick.
        """
        start_seq = max(self._error_scanned_seq, self._log_seqs.get("#live-log-full", 0))

        def count_entry(entry: dict) -> bool:
            if entry.get("level", "INFO") == "ERROR":
                self.error_count += 1
            return True

        new_seq, _ = self._drain_log_buffer(start_seq, LOG_DRAIN_PAGE_SIZE, count_entry)
        if new_seq > self._error_scanned_seq:
            self._error_scanned_seq = new_seq

    def _log_entry_passes_user_filters(self, entry: dict, level: str, msg: str) -> bool:
        """Return True if the entry passes the Logs-tab user filters."""
        try:
            level_filter = self.query_one("#log-level-filter", Select).value
            if level_filter != "ALL" and level != level_filter:
                return False
            text_filter = self.query_one("#log-filter", Input).value
            if text_filter and text_filter.lower() not in msg.lower():
                return False
            time_range = self.query_one("#log-time-range", Select).value
            if time_range != "ALL":
                entry_ts = entry.get("timestamp", "")
                if entry_ts:
                    try:
                        dt = datetime.fromisoformat(entry_ts)
                        cutoff_map = {"5m": 300, "1h": 3600, "24h": 86400}
                        if time_range in cutoff_map:
                            if time.time() - dt.timestamp() > cutoff_map[time_range]:
                                return False
                    except Exception:
                        pass
        except NoMatches:
            pass
        return True

    def _write_log_entry(self, log_widget, entry: dict) -> None:
        """Format and write a single log entry line to the widget.

        Error counting lives in `_append_logs_to_widget`, not here: it must
        count every ERROR the proxy emitted, including ones the user's own
        level filter hid from this widget.
        """
        level = entry.get("level", "INFO")
        ts = entry.get("timestamp", "")
        if isinstance(ts, str) and " " in ts:
            ts = ts.split(" ")[1] if len(ts.split(" ")) > 1 else ts
        log_widget.write_line(f"[{level}] {ts} -- {entry.get('message', '')}")

    def _drain_log_buffer(self, start_seq: int, cap: int, write_entry) -> tuple[int, int]:
        """Drain new log entries from the TUI handler into the target widget.

        Repeatedly calls `self.log_handler.get_logs(limit=LOG_DRAIN_PAGE_SIZE,
        from_seq=seq)` until a short page (fewer than a full page -> reached the
        newest) or an empty page is returned, or the display `cap` of newly
        written entries has been hit. `write_entry(entry)` is called for each
        candidate entry and returns True if it was displayed.

        Returns `(new_seq, written)` where `new_seq` is one past the highest
        entry consumed this drain (the value to store in `_log_seqs` for the
        next incremental poll) and `written` is how many entries were actually
        displayed. This bounds the fetch to one page per poll and never skips
        entries older than the current page.
        """
        seq = start_seq
        written = 0
        while True:
            page = self.log_handler.get_logs(limit=LOG_DRAIN_PAGE_SIZE, from_seq=seq)
            if not page:
                break
            last_consumed = 0
            cap_hit = False
            for entry in page:
                last_consumed = max(last_consumed, entry.get("seq", 0))
                if write_entry(entry):
                    written += 1
                    if cap and written >= cap:
                        cap_hit = True
                        break
            seq = last_consumed + 1
            if cap_hit or len(page) < LOG_DRAIN_PAGE_SIZE:
                break
        return seq, written

    def _log_display_cap(self, log_widget) -> int:
        """Return the widget's line cap (0 => unbounded) for a re-population drain."""
        cap = getattr(log_widget, "max_lines", None)
        return cap if isinstance(cap, int) else 0

    def _append_logs_to_widget(self, widget_id: str, apply_filter: bool = False, count_errors: bool = False) -> int:
        """Drain new log entries into a specific Log widget.

        Args:
            widget_id: The CSS selector for the Log widget.
            apply_filter: If True, apply user-level text/level/time filters (Logs tab).
            count_errors: If True, count every ERROR drained into `error_count`
                (which feeds `#error-banner`) regardless of whether the user's
                filter actually displayed the line.

        Uses a paged drain loop (see `_drain_log_buffer`) so a backlog larger
        than one page is not silently dropped, and caps new appends at the
        widget's max_lines so bounded Log widgets stay bounded on re-population.
        """
        try:
            log_widget = self.query_one(widget_id, Log)
        except NoMatches:
            return 0

        if widget_id == "#live-log-full":
            log_widget.auto_scroll = self._autoscroll_enabled

        start_seq = self._log_seqs.get(widget_id, 0)
        cap = self._log_display_cap(log_widget)

        def write_entry(entry: dict) -> bool:
            level = entry.get("level", "INFO")
            msg = entry.get("message", "")
            # Count before filtering: `#error-banner` reports errors the proxy
            # actually emitted, not only the ones the user's filter kept.
            # Entries below `_error_scanned_seq` were already counted by the
            # hidden-tab `_scan_log_errors` pass — this widget cursor can be
            # behind it, because re-filtering rewinds the cursor to 0.
            if (count_errors and level == "ERROR"
                    and entry.get("seq", 0) >= self._error_scanned_seq):
                self.error_count += 1
            # Full log tab: apply user filters.
            if apply_filter and not self._log_entry_passes_user_filters(entry, level, msg):
                return False
            self._write_log_entry(log_widget, entry)
            return True

        new_seq, written = self._drain_log_buffer(start_seq, cap, write_entry)
        self._log_seqs[widget_id] = new_seq
        if count_errors:
            # This drain just walked the whole counted range, so the count-only
            # cursor can move up to meet it. Without this, a later hidden pass
            # would re-count errors the visible drain already added.
            self._error_scanned_seq = max(self._error_scanned_seq, new_seq)
        return written

    def _alert_badge_width(self) -> int:
        """Cells available to `#alert-badge`'s text, from a sibling that never hides.

        The badge cannot measure itself: it is `display: none` whenever its
        `.hidden` class is set, which is exactly the moment it is about to
        receive its first — and usually longest — alert, so a self-measurement
        returns 0 and the row clips mid-token until the next poll trims it.
        `#status-indicator` is a sibling in the same `#overview-panel`, carries
        the same `padding: 0 1`, and is never hidden, so its content width IS
        the badge's text area. That coupling is why both rules live in the same
        tcss block.
        """
        try:
            return self.query_one("#status-indicator", Static).content_size.width
        except NoMatches:
            return 0

    def _update_alert_badge(self, status: dict) -> None:
        """Update alert badge based on quota usage and circuit status.

        In multi-provider mode, checks quotas and circuit breakers for ALL
        providers. In single-provider mode, checks the flat status dict.
        """
        try:
            badge = self.query_one("#alert-badge", Static)
            warnings = []

            if "primary" in status:
                # Multi-provider mode: check all providers via registry
                for provider_info in PROVIDER_REGISTRY:
                    provider_key = provider_info["key"]
                    provider_status = status.get(provider_key)
                    if not provider_status:
                        continue
                    provider_warnings = self._check_quota_thresholds(provider_status)
                    for name in provider_warnings:
                        label = f"{name} ({provider_key})" if provider_key != "primary" else name
                        if label not in warnings:
                            warnings.append(label)
                    if provider_status.get("circuit_open") and "Circuit" not in warnings:
                        warnings.append(f"Circuit ({provider_key})" if provider_key != "primary" else "Circuit")
            else:
                # Single-provider mode
                warnings = self._check_quota_thresholds(status)
                if status.get("circuit_open"):
                    warnings.append("Circuit")

            # Check for recent failover events (independent of other warnings).
            # Time-gate on the entry's timestamp_utc (last 5 minutes) so a
            # single old failover does not linger on the badge until 200 newer
            # requests scroll it out.
            from tui_status import failover_alert_should_show, is_failover_recent
            recent_failovers = False
            for entry in self._session_log_snapshot()[-10:]:
                if len(entry.get("attempted_providers", [])) > 1:
                    if is_failover_recent(entry.get("timestamp_utc")):
                        recent_failovers = True
                        break
            if failover_alert_should_show(warnings, recent_failovers):
                if "Failover" not in warnings:
                    warnings.append("Failover")

            if warnings:
                # One row, and honest about it: trim to the badge's real width
                # with a visible ellipsis rather than letting the row clip
                # mid-token.
                badge.update(_ellipsize(
                    "ALERT: " + " | ".join(warnings) + " critical",
                    self._alert_badge_width(),
                ))
                badge.add_class("alert-active")
                badge.set_class(False, "hidden")
            else:
                # Collapse like #error-banner. The badge used to keep a blank
                # row reserved whenever there were no alerts (previously masked
                # by the dock bug that made it paint over the status line).
                badge.update("")
                badge.remove_class("alert-active")
                badge.set_class(True, "hidden")
        except NoMatches:
            pass

    def _update_latency_histogram(self) -> None:
        """Update latency histogram visualization in Metrics tab.

        Computes over the UNION of all per-provider tracker values so the
        Metrics tab reflects every provider's latencies (not just "primary").
        """
        try:
            panel = self.query_one("#latency-histogram-panel", Static)
        except NoMatches:
            return

        stats = self._latency_union_stats()
        latencies = self._latency_union_values()
        if not latencies:
            # Collapse via the `.hidden` class, not `:empty` — see the note in
            # `_clear_poll_error`. With `:empty` this panel was permanently
            # invisible, histogram included.
            panel.update("")
            panel.set_class(True, "hidden")
            return

        buckets = [
            ("0-100ms", 0, 100),
            ("100-250ms", 100, 250),
            ("250-500ms", 250, 500),
            ("500-1s", 500, 1000),
            ("1s+", 1000, float("inf")),
        ]

        counts = []
        for _, low, high in buckets:
            count = sum(1 for l in latencies if low <= l < high)
            counts.append(count)

        max_count = max(counts) if counts else 1
        max_bar_width = 40

        lines = []
        for i, (label, _, _) in enumerate(buckets):
            if max_count > 0:
                bar_len = int((counts[i] / max_count) * max_bar_width)
            else:
                bar_len = 0
            bar = "#" * bar_len
            lines.append(f"  {label:>10} | {bar} {counts[i]}")

        lines.append(
            f"  avg: {stats['avg']}ms | p50: {stats['p50']}ms | "
            f"p95: {stats['p95']}ms | p99: {stats['p99']}ms"
        )

        panel.update("\n".join(lines))
        panel.set_class(False, "hidden")

    def _session_log_snapshot(self) -> list[dict]:
        """Today's session-log entries, read at most ONCE per poll.

        `#alert-badge` (Overview) and the failover panel (Metrics) both consume
        this file, and each used to call `_read_session_log_entries` itself —
        two `os.stat` + `open`/`seek`/`read` passes and two rescans of the
        200-entry deque for one file, per tick. Tab gating already means only
        one of the two is even dispatched on a given tick, so this cache is the
        guarantee rather than the saving: "at most one session-log read per
        tick" is now a property of the code, not of which tab happens to be
        active.

        The cache is keyed on the poll GENERATION (bumped by the poll loop and
        by every tab switch), not on a wall clock, so it cannot outlive a tick.
        It deliberately caches only the returned list: `_read_session_log_entries`
        keeps owning the offset / inode-rotation / partial-line state machine
        and is still the only thing that advances the offset, so reading once
        instead of twice cannot drop or duplicate an entry.
        """
        generation = self._poll_generation
        if generation == self._session_log_gen:
            return self._session_log_cache
        entries = self._read_session_log_entries()
        self._session_log_gen = generation
        self._session_log_cache = entries
        return entries

    def _read_session_log_entries(self) -> list[dict]:
        """Incrementally read today's session log; return last 200 parsed entries."""
        log_dir = "session_logs"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = f"{log_dir}/{today}.jsonl"

        if path != self._session_log_path:
            self._session_log_path = path
            self._session_log_offset = 0
            self._session_log_inode = None
            self._session_log_tail.clear()
            self._session_log_partial = ""

        try:
            st = os.stat(path)
        except FileNotFoundError:
            return list(self._session_log_tail)

        inode = getattr(st, "st_ino", None) or st.st_mtime_ns
        if inode != self._session_log_inode or st.st_size < self._session_log_offset:
            self._session_log_offset = 0
            self._session_log_tail.clear()
            self._session_log_partial = ""
            self._session_log_inode = inode

        try:
            # errors="replace" so a single undecodable byte can never raise and
            # freeze the reader (offset would stop advancing and the same bytes
            # would be re-read every poll). Malformed JSON lines are already
            # skipped by the JSON-parse step below.
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._session_log_offset)
                data = f.read()
                self._session_log_offset = f.tell()
        except OSError:
            return list(self._session_log_tail)

        # Carry incomplete trailing line across polls so mid-chunk JSON is not lost.
        chunk = self._session_log_partial + data
        if chunk.endswith("\n") or chunk.endswith("\r"):
            lines, self._session_log_partial = chunk.splitlines(), ""
        else:
            parts = chunk.splitlines()
            if data == "" and not parts:
                return list(self._session_log_tail)
            self._session_log_partial = parts[-1] if parts else ""
            lines = parts[:-1] if parts else []
            if len(self._session_log_partial.encode("utf-8", errors="replace")) > SESSION_LOG_PARTIAL_MAX_BYTES:
                self._session_log_partial = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                self._session_log_tail.append(entry)

        return list(self._session_log_tail)

    def _update_failover_panel(self) -> None:
        """Update failover events panel from recent session logs."""
        try:
            panel = self.query_one("#failover-events-panel", Static)
            title = self.query_one("#failover-title", Static)
        except NoMatches:
            return

        failovers = []
        for entry in self._session_log_snapshot():
            attempted = entry.get("attempted_providers", [])
            if len(attempted) <= 1:
                continue
            model = entry.get("model", "unknown")
            request_id = entry.get("request_id", "")[:8]
            timestamp = entry.get("timestamp_utc", "")
            if timestamp:
                from tui_status import parse_timestamp_utc
                dt = parse_timestamp_utc(timestamp)
                time_str = dt.strftime("%H:%M:%S") if dt else timestamp
            else:
                time_str = "unknown"

            failovers.append({
                "model": model,
                "chain": attempted,
                "request_id": request_id,
                "time": time_str,
            })

        if failovers:
            failovers = failovers[-10:]
            lines = []
            for event in failovers:
                chain_str = " → ".join(event["chain"])
                lines.append(f"  {event['model']} → [{chain_str}] (req: {event['request_id']}, {event['time']})")
            panel.update("\n".join(lines))
            panel.set_class(False, "hidden")
            title.set_class(False, "hidden")
        else:
            # Collapse when there is nothing to report. This used to render the
            # literal text "No recent failover events" while being permanently
            # invisible anyway, because of the `:empty` trap.
            panel.update("")
            panel.set_class(True, "hidden")
            # The heading collapses WITH the panel. Once the panel hides
            # correctly, leaving "Failover Events" docked at the top of the
            # Metrics tab left a lone title above nothing.
            title.set_class(True, "hidden")

    @_safe_update
    def _update_model_table(self, status: dict) -> None:
        """Update per-model usage DataTable with multi-provider breakdown.
        
        Receives raw_status from MultiProviderRateLimiter.status() which has
        the shape {"primary": {...}, "secondary": {...}, "shared_limits": bool}.
        
        In multi-provider mode, aggregates stats across providers and shows
        breakdown rows for models served by multiple providers.
        """
        table = self.query_one("#model-usage-table", DataTable)
        view = self._snapshot_table_view(table)
        table.clear()

        # Step 1: Collect model usage from all providers
        model_data = {}  # {model_name: {provider_key: stats}}
        
        if "primary" in status:
            # Multi-provider mode
            for provider_info in PROVIDER_REGISTRY:
                provider_key = provider_info["key"]
                provider_status = status.get(provider_key)
                if not provider_status:
                    continue
                
                for model_name, stats in provider_status.get("model_usage", {}).items():
                    if model_name not in model_data:
                        model_data[model_name] = {}
                    model_data[model_name][provider_key] = stats
        else:
            # Single provider mode - treat as primary
            for model_name, stats in status.get("model_usage", {}).items():
                model_data[model_name] = {"primary": stats}
        
        # Step 2: Aggregate stats across providers for each model
        aggregated = {}  # {model_name: {aggregated_fields, providers, provider_stats}}
        
        for model_name, provider_stats in model_data.items():
            total_requests = sum(s.get("requests", 0) for s in provider_stats.values())
            total_tokens = sum(s.get("tokens", 0) for s in provider_stats.values())
            total_429s = sum(s.get("errors_429", 0) for s in provider_stats.values())
            
            # Weighted average latency (weighted by requests)
            if total_requests > 0:
                avg_latency = sum(
                    s.get("avg_latency_ms", 0) * s.get("requests", 0)
                    for s in provider_stats.values()
                ) / total_requests
            else:
                avg_latency = 0
            
            # Collect percentiles (use primary provider's values if available, else first available)
            p50_latency = 0
            p95_latency = 0
            for provider_key, s in provider_stats.items():
                if provider_key == "primary" or p50_latency == 0:
                    p50_latency = s.get("p50_latency_ms", 0)
                    p95_latency = s.get("p95_latency_ms", 0)
                    if provider_key == "primary":
                        break
            
            aggregated[model_name] = {
                "providers": list(provider_stats.keys()),
                "provider_stats": provider_stats,
                "total_requests": total_requests,
                "total_tokens": total_tokens,
                "total_429s": total_429s,
                "avg_latency_ms": avg_latency,
                "p50_latency_ms": p50_latency,
                "p95_latency_ms": p95_latency,
            }
        
        # Step 3: Apply filter
        if self._model_filter:
            filter_lower = self._model_filter.lower()
            aggregated = {k: v for k, v in aggregated.items() if filter_lower in k.lower()}
        
        # Step 4: Sort by selected key
        sort_key = self._model_sort_key
        if sort_key == "requests":
            sorted_models = sorted(aggregated.items(), key=lambda x: x[1]["total_requests"], reverse=True)
        elif sort_key == "tokens":
            sorted_models = sorted(aggregated.items(), key=lambda x: x[1]["total_tokens"], reverse=True)
        elif sort_key == "latency":
            sorted_models = sorted(aggregated.items(), key=lambda x: x[1]["avg_latency_ms"], reverse=True)
        elif sort_key == "429s":
            sorted_models = sorted(aggregated.items(), key=lambda x: x[1]["total_429s"], reverse=True)
        else:
            sorted_models = sorted(aggregated.items(), key=lambda x: x[1]["total_requests"], reverse=True)

        if not sorted_models:
            table.add_row("--", "No model data yet", "", "", "", "", "", "")
        else:
            total_requests = sum(v["total_requests"] for _, v in sorted_models)
            
            # Step 5: Render rows
            for model_name, data in sorted_models:
                # Format provider list (comma-separated)
                providers_list = ", ".join(data["providers"])
                
                # Main row with aggregated stats
                pct = f"{(data['total_requests'] / total_requests * 100):.0f}%" if total_requests > 0 else "0%"
                table.add_row(
                    model_name,
                    providers_list,
                    f"{data['total_requests']} ({pct})",
                    _fmt_number(data["total_tokens"]),
                    str(data["total_429s"]),
                    f"{data['avg_latency_ms']:.0f}ms",
                    f"{data['p50_latency_ms']:.0f}ms",
                    f"{data['p95_latency_ms']:.0f}ms",
                )
                
                # Breakdown rows for multi-provider models
                if len(data["providers"]) > 1:
                    for provider_key, provider_stats in data["provider_stats"].items():
                        # Find provider label from registry
                        provider_label = next(
                            (p["label"] for p in PROVIDER_REGISTRY if p["key"] == provider_key),
                            provider_key
                        )
                        
                        # Calculate percentage for this provider
                        provider_pct = f"{(provider_stats.get('requests', 0) / data['total_requests'] * 100):.0f}%" if data['total_requests'] > 0 else "0%"
                        
                        table.add_row(
                            f"  via {provider_label}",
                            "",  # Providers column (empty for breakdown)
                            f"{provider_stats.get('requests', 0)} ({provider_pct})",
                            _fmt_number(provider_stats.get("tokens", 0)),
                            str(provider_stats.get("errors_429", 0)),
                            f"{provider_stats.get('avg_latency_ms', 0):.0f}ms",
                            f"{provider_stats.get('p50_latency_ms', 0):.0f}ms",
                            f"{provider_stats.get('p95_latency_ms', 0):.0f}ms",
                        )
            
            # Totals row
            total_tokens = sum(v["total_tokens"] for _, v in sorted_models)
            total_429s = sum(v["total_429s"] for _, v in sorted_models)
            avg_latency = sum(v["avg_latency_ms"] * v["total_requests"] for _, v in sorted_models) / total_requests if total_requests > 0 else 0
            table.add_row(
                "TOTAL",
                "",
                str(total_requests),
                _fmt_number(total_tokens),
                str(total_429s),
                f"{avg_latency:.0f}ms",
                "",
                "",
            )

        self._restore_table_view(table, view)

    def _update_config_table_filtered(self, *, force: bool = False) -> None:
        """Update config table with current filter and grouping settings.

        The poller used to call this every tick, clearing and re-adding ~92
        rows (109 when grouped) once a second to redraw data that only changes
        when the user presses Refresh. `_config_dirty` is the gate: it is set by
        the Refresh button, the Grouped toggle, the filter input and mount, and
        cleared here once the rebuild has actually happened. The poll-loop call
        therefore becomes a no-op, and the Config tab costs nothing per tick.

        `force=True` re-renders regardless — the one case where the data is
        unchanged but the widget still needs redrawing.
        """
        if not (self._config_dirty or force):
            return
        try:
            table = self.query_one("#config-table", DataTable)
        except NoMatches:
            return

        view = self._snapshot_table_view(table)
        # Cleared only after the query succeeds, so a table that is not mounted
        # yet stays dirty and is rendered by the next attempt.
        self._config_dirty = False
        table.clear()
        filtered = [
            row for row in self._config_rows
            if not self._config_filter or self._config_filter.lower() in row[1].lower()
        ]

        if self._config_grouped:
            current_group = None
            for group, key, value, source in filtered:
                if group != current_group:
                    table.add_row(f"--- {group} ---", "", "")
                    current_group = group
                table.add_row(key, value, source)
        else:
            for group, key, value, source in sorted(filtered, key=lambda r: r[1]):
                table.add_row(key, value, source)

        self._restore_table_view(table, view)

    def _update_log_entry_count(self) -> None:
        """Update the log entry count indicator."""
        try:
            count_widget = self.query_one("#log-entry-count", Static)
            count_widget.update(f"{self._displayed_log_count} entries")
        except NoMatches:
            pass

    def _export_logs(self) -> None:
        """Export filtered logs to a file in session_logs directory."""
        import os
        import json
        
        log_dir = "session_logs"
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError as exc:
            try:
                count_widget = self.query_one("#log-entry-count", Static)
                count_widget.update(f"Export failed: {exc}")
            except NoMatches:
                pass
            return

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(log_dir, f"tui_export_{timestamp}.log")
        
        # Snapshot under the handler lock so background emitters cannot mutate it.
        all_logs = self.log_handler.snapshot()
        
        # Apply current filters
        try:
            level_filter = self.query_one("#log-level-filter", Select).value
            text_filter = self.query_one("#log-filter", Input).value
            time_range = self.query_one("#log-time-range", Select).value
        except NoMatches:
            level_filter = "ALL"
            text_filter = ""
            time_range = "ALL"
        
        # Apply time range filter
        now = time.time()
        if time_range == "5m":
            cutoff = now - 300
        elif time_range == "1h":
            cutoff = now - 3600
        elif time_range == "24h":
            cutoff = now - 86400
        else:
            cutoff = 0
        
        filtered = []
        for entry in all_logs:
            # Time filter
            try:
                entry_ts = entry.get("timestamp", "")
                if cutoff > 0 and entry_ts:
                    # Parse timestamp and compare
                    dt = datetime.fromisoformat(entry_ts)
                    if dt.timestamp() < cutoff:
                        continue
            except Exception:
                pass
            
            # Level filter
            if level_filter != "ALL" and entry.get("level", "INFO") != level_filter:
                continue
            
            # Text filter
            if text_filter and text_filter.lower() not in entry.get("message", "").lower():
                continue
            
            filtered.append(entry)
        
        # Write to file. Blocking I/O on the UI thread — guard against OSError
        # (disk full, permissions, read-only FS, ...) so a failed export
        # surfaces a message instead of a traceback.
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                for entry in filtered:
                    ts = entry.get("timestamp", "")
                    level = entry.get("level", "INFO")
                    msg = entry.get("message", "")
                    f.write(f"[{level}] {ts} -- {msg}\n")
        except OSError as exc:
            try:
                count_widget = self.query_one("#log-entry-count", Static)
                count_widget.update(f"Export failed: {exc}")
            except NoMatches:
                pass
            return

        # Show confirmation in entry count
        try:
            count_widget = self.query_one("#log-entry-count", Static)
            count_widget.update(f"Exported {len(filtered)} entries to {os.path.basename(filepath)}")
        except NoMatches:
            pass

    def _format_uptime(self, seconds: float) -> str:
        """Format uptime as human-readable string."""
        if seconds < 60:
            return f"{int(seconds)}s"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        if minutes < 60:
            return f"{minutes}m {secs}s"
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins}m"

    def action_clear_logs(self) -> None:
        """Clear all log feeds."""
        try:
            self.query_one("#live-log-full", Log).clear()
        except NoMatches:
            pass
        self._log_seqs.clear()
        self.error_count = 0
        self._displayed_log_count = 0
        # `TUILogHandler.clear()` restarts its seq counter at 0, so the
        # count-only cursor has to rewind with the widget cursor or it would
        # skip every entry written after the clear.
        self._error_scanned_seq = 0
        self.log_handler.clear()

    def switch_to(self, tab_id: str) -> None:
        """Switch to the specified tab."""
        try:
            tabs = self.query_one("#main-tabs", TabbedContent)
            tabs.active = tab_id
        except NoMatches:
            pass

    def action_switch_to(self, tab_id: str) -> None:
        """Switch to the specified tab (key binding entry point)."""
        self.switch_to(tab_id)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "clear-logs-btn":
            self.action_clear_logs()
        elif event.button.id == "pause-logs-btn":
            self._logs_paused = not self._logs_paused
            event.button.label = "Resume" if self._logs_paused else "Pause"
            event.button.variant = "warning" if self._logs_paused else "default"
        elif event.button.id == "export-logs-btn":
            self._export_logs()
        elif event.button.id == "autoscroll-btn":
            self._autoscroll_enabled = not self._autoscroll_enabled
            event.button.label = "Auto-scroll: ON" if self._autoscroll_enabled else "Auto-scroll: OFF"
            event.button.variant = "primary" if self._autoscroll_enabled else "default"
            try:
                self.query_one("#live-log-full", Log).auto_scroll = self._autoscroll_enabled
            except NoMatches:
                pass
        elif event.button.id == "config-refresh-btn":
            self._config_rows = _load_display_config()
            self._config_dirty = True
            self._update_config_table_filtered()
        elif event.button.id == "config-grouped-btn":
            self._config_grouped = not self._config_grouped
            event.button.variant = "primary" if self._config_grouped else "default"
            self._config_dirty = True
            self._update_config_table_filtered()

    async def on_input_changed(self, event: Input.Changed) -> None:
        """Handle input field changes for filters."""
        if event.input.id == "log-filter":
            # Clear log widget and re-poll from the start to apply filter to all entries
            try:
                log_widget = self.query_one("#live-log-full", Log)
                log_widget.clear()
                self._log_seqs["#live-log-full"] = 0
                self._displayed_log_count = 0
                # `visible=True`: rewinding to seq 0 only makes sense for the
                # widget, so bypass the tab gate (these handlers only fire from
                # the Logs tab, but the drain must not silently degrade to the
                # count-only pass if they ever fire from elsewhere).
                self._poll_logs(visible=True)
            except NoMatches:
                pass
        elif event.input.id == "model-filter":
            self._model_filter = event.value
            # Reuse the raw status already cached by the poller (a fresh
            # status() per keystroke builds the dict 7x under the limiter
            # lock in multi-provider mode) rather than re-querying.
            status = self._last_raw_status
            if status:
                self._update_model_table(status)
        elif event.input.id == "config-filter":
            self._config_filter = event.value
            self._config_dirty = True
            self._update_config_table_filtered()

    async def on_select_changed(self, event: Select.Changed) -> None:
        """Handle select widget changes."""
        if event.select.id == "log-level-filter":
            # Clear log widget and re-poll from the start to apply filter to all entries
            try:
                log_widget = self.query_one("#live-log-full", Log)
                log_widget.clear()
                self._log_seqs["#live-log-full"] = 0
                self._displayed_log_count = 0
                self._poll_logs(visible=True)
            except NoMatches:
                pass
        elif event.select.id == "log-time-range":
            # Clear log widget and re-poll from the start to apply time filter
            try:
                log_widget = self.query_one("#live-log-full", Log)
                log_widget.clear()
                self._log_seqs["#live-log-full"] = 0
                self._displayed_log_count = 0
                self._poll_logs(visible=True)
            except NoMatches:
                pass
        elif event.select.id == "model-sort":
            self._model_sort_key = event.value
            # Reuse the poller's cached status rather than a fresh status() call.
            status = self._last_raw_status
            if status:
                self._update_model_table(status)


    def _signal_proxy_shutdown(self) -> None:
        """Signal the proxy server to shut down (cross-thread safe)."""
        self._shutdown_event.set()
        # The poll loop now sleeps on `_refresh_wake`, so the shutdown flag has
        # to wake it too or it would sit out the rest of the interval (up to
        # 16 s) before noticing.
        self._refresh_wake.set()

        for worker in self.workers:
            worker.cancel()

        shutdown_event = self.proxy_app.get("shutting_down")
        event_loop = self.proxy_app.get("event_loop")
        if shutdown_event and not shutdown_event.is_set():
            if event_loop and event_loop.is_running():
                event_loop.call_soon_threadsafe(shutdown_event.set)
            else:
                shutdown_event.set()

    def action_quit(self) -> None:
        """Override quit to signal proxy shutdown before exiting the TUI."""
        self._signal_proxy_shutdown()
        self.exit()

    def on_unmount(self) -> None:
        """Signal proxy shutdown and cancel background worker when the TUI unmounts."""
        self._signal_proxy_shutdown()
