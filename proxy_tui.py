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
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Label, Log, Static, Input, Select, Sparkline, Button, TabbedContent, TabPane
from textual.worker import Worker, get_current_worker
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

        # Models tab state
        self._model_filter: str = ""
        self._model_sort_key: str = "requests"

        # Config tab state
        self._config_filter: str = ""
        self._config_grouped: bool = False

        # Cancellable sleep for the poll worker
        self._shutdown_event = threading.Event()

        # Poller state
        self.error_count = 0
        self._poll_error_count = 0

        # Overview provider grid state (row order -> provider key mapping)
        self._grid_row_keys: list[str] = []
        self._detail_provider_key: str = "primary"
        self._last_raw_status: dict | None = None

        # Incremental session-log tail (offset/inode + last 200 parsed entries)
        self._session_log_offset = 0
        self._session_log_inode = None
        self._session_log_path: str | None = None
        self._session_log_tail: deque[dict] = deque(maxlen=200)
        self._session_log_partial = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="main-tabs", initial="tab-overview"):
            # Tab 1: Overview (primary operational dashboard)
            with TabPane("Overview", id="tab-overview"):
                with Horizontal():
                    with Vertical(id="metrics-panel"):
                        yield Static("Proxy: Checking...", id="status-indicator")
                        yield Static("", id="alert-badge", classes="alert-badge")
                        # Compact provider grid: one row per provider
                        yield DataTable(id="provider-grid")
                        # Detail pane: full breakdown for the provider under the grid cursor
                        yield Static("", id="provider-detail-title", classes="section-title")
                        yield DataTable(id="provider-detail")

                        yield Static("", classes="spacer")
                        yield Static("Request Statistics", classes="section-title")
                        yield DataTable(id="request-stats")
                    with Vertical(id="log-panel"):
                        yield Static("Alerts & Warnings", classes="panel-title")
                        yield Static("", id="error-banner", classes="error-banner")
                        yield Log(id="live-log", max_lines=500)

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

            # Tab 3: Metrics (sparklines + derived metrics + timers) - REORGANIZED layout
            with TabPane("Metrics", id="tab-metrics"):
                with Vertical(id="metrics-tab-content"):
                    yield Static("", id="derived-metrics-panel", classes="derived-panel")
                    yield Horizontal(
                        Static("RPM", id="rpm-sparkline-label", classes="sparkline-label"),
                        Sparkline([], id="rpm-sparkline"),
                        Static("TPM Used", id="tpm-sparkline-label", classes="sparkline-label"),
                        Sparkline([], id="tpm-sparkline"),
                        Static("Queue", id="queue-sparkline-label", classes="sparkline-label"),
                        Sparkline([], id="queue-sparkline"),
                        Static("Upstream Latency", id="upstream-latency-sparkline-label", classes="sparkline-label"),
                        Sparkline([], id="upstream-latency-sparkline"),
                        id="sparkline-row",
                    )
                    yield Static("Failover Events", classes="panel-title")
                    yield Static("", id="failover-events-panel", classes="failover-panel")
                    yield Static("", id="latency-histogram-panel", classes="histogram-panel")
                    yield Static("", id="poll-error-banner", classes="poll-error")

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

        # Configure the compact provider grid
        grid = self.query_one("#provider-grid", DataTable)
        grid.add_column("Provider", width=11)
        grid.add_column("Circuit", width=7)
        grid.add_column("RPM", width=14)
        grid.add_column("TPM", width=14)
        grid.add_column("Fwd", width=7)
        grid.add_column("429", width=4)
        grid.show_header = True
        grid.zebra_stripes = True
        grid.cursor_type = "row"

        # Configure the provider detail pane (full breakdown of selected provider)
        detail_table = self.query_one("#provider-detail", DataTable)
        detail_table.add_column("Metric", width=14)
        detail_table.add_column("Value")
        detail_table.show_header = False
        detail_table.zebra_stripes = True

        # Configure request statistics table
        stats_table = self.query_one("#request-stats", DataTable)
        stats_table.add_columns("Statistic", "Value")
        stats_table.show_header = False
        stats_table.zebra_stripes = True

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

    def _populate_config_table(self, table: DataTable) -> None:
        """Populate the config table with current configuration."""
        self._update_config_table_filtered()

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

                # Update all UI components
                self.call_from_thread(self._update_metrics, raw_status)
                self.call_from_thread(self._poll_logs)
                self.call_from_thread(self._update_sparklines)
                self.call_from_thread(self._update_derived_metrics, raw_status)
                self.call_from_thread(self._update_failover_panel)
                self.call_from_thread(self._update_latency_histogram)
                self.call_from_thread(self._update_model_table, raw_status)
                self.call_from_thread(self._update_config_table_filtered)
                # Compact provider grid + detail pane (all providers, one pass)
                self.call_from_thread(self._update_provider_grid, raw_status)

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

            # Use an event for cancellable sleep (worker.cancel() won't
            # interrupt time.sleep, so we wait on an event instead).
            self._shutdown_event.wait(timeout=interval)
            if self._shutdown_event.is_set():
                break

    def _show_poll_error(self) -> None:
        """Display a transient error banner when polling fails."""
        try:
            banner = self.query_one("#poll-error-banner", Static)
            banner.update(f"Poll error ({self._poll_error_count} errors) -- reconnecting...")
        except NoMatches:
            pass

    def _clear_poll_error(self) -> None:
        """Clear the poll-error banner once polling recovers.

        The tcss rule `.poll-error:empty { display: none }` hides the widget
        when its text is empty, so resetting the text to "" is enough.
        """
        try:
            banner = self.query_one("#poll-error-banner", Static)
            banner.update("")
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

        if not self._validate_status(primary):
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

        # Request statistics — use AGGREGATED totals across all providers.
        # Per-provider forwarded/429 counts live in the compact provider grid.
        from tui_status import overview_request_stats, success_rate_display
        stats = overview_request_stats(status)
        stats_table = self.query_one("#request-stats", DataTable)
        stats_table.clear()
        total_fwd = stats["total_forwarded"]
        total_429s = stats["total_429s"]

        total_request_bytes = multi.get("total_request_bytes", 0)
        total_response_bytes = multi.get("total_response_bytes", 0)
        avg_req_size = _fmt_number(total_request_bytes // total_fwd) if total_fwd > 0 else "0"
        avg_resp_size = _fmt_number(total_response_bytes // total_fwd) if total_fwd > 0 else "0"

        stats_table.add_row("Total Forwarded", _fmt_number(total_fwd))
        stats_table.add_row("Total 429s", str(total_429s))
        stats_table.add_row("Rejected", str(stats["total_rejected"]))
        stats_table.add_row("Pending", f"{stats['pending_requests']}/{stats['max_queue_size']}")
        # Display-side guard: "n/a" when there are no attempts yet, so an idle
        # proxy does not read as a 0.0% success rate.
        stats_table.add_row(
            "Success Rate",
            success_rate_display(total_fwd, total_429s, stats["total_rejected"]),
        )
        stats_table.add_row("Queue Drops", str(primary.get("queue_drops", 0)))
        queue_p50 = primary.get("queue_p50_ms", 0)
        queue_p95 = primary.get("queue_p95_ms", 0)
        queue_p99 = primary.get("queue_p99_ms", 0)
        stats_table.add_row("Queue Wait (p50/p95/p99)", f"{queue_p50}ms / {queue_p95}ms / {queue_p99}ms")
        stats_table.add_row(
            "Tokens Consumed",
            _fmt_number(multi.get("total_tokens_consumed", 0)),
        )
        stats_table.add_row("Avg Req/Resp Size", f"{avg_req_size} / {avg_resp_size}")

        # Update error banner
        error_banner = self.query_one("#error-banner", Static)
        if self.error_count > 0:
            error_banner.update(f"{self.error_count} error(s) in recent logs")
        else:
            error_banner.update("")

    @_safe_update
    def _update_provider_grid(self, status: dict) -> None:
        """Populate the compact provider grid (one row per provider).

        Uses the pure `provider_grid_rows` helper from tui_status, then keeps
        a row-index -> provider-key mapping for the detail pane cursor follow.
        The currently-highlighted provider key is remembered before `clear()`
        (which resets the cursor to row 0) and restored afterwards so the
        cursor-following detail pane does not snap back to the first provider
        on every poll.
        """
        from tui_status import provider_grid_rows

        self._last_raw_status = status
        rows = provider_grid_rows(status)
        if not rows and "rpm_limit" in status:
            # Single-provider (flat) status: synthesize the primary row.
            rows = provider_grid_rows({"primary": status})

        # Remember the provider the detail pane is currently showing.
        remembered_key = self._detail_provider_key

        grid = self.query_one("#provider-grid", DataTable)
        grid.clear()
        self._grid_row_keys = []
        for row in rows:
            grid.add_row(
                row["label"],
                row["circuit"],
                row["rpm"],
                row["tpm"],
                row["forwarded"],
                row["count_429s"],
            )
            self._grid_row_keys.append(row["key"])

        if rows and remembered_key not in self._grid_row_keys:
            # The provider the user was viewing is gone this poll; fall back
            # to the first provider.
            remembered_key = rows[0]["key"]
        self._detail_provider_key = remembered_key
        self._update_provider_detail(status)

        # Restore the cursor to the remembered provider so the detail pane
        # stays on the user's provider. `clear()` above reset the cursor to
        # row 0; move it back to the remembered row (column 0).
        if self._grid_row_keys:
            try:
                restore_index = (
                    self._grid_row_keys.index(remembered_key)
                    if remembered_key in self._grid_row_keys
                    else 0
                )
                grid.move_cursor(row=restore_index, column=0)
            except (ValueError, NoMatches):
                pass

    @_safe_update
    def _update_provider_detail(self, status: dict) -> None:
        """Render the detail pane for the provider under the grid cursor."""
        key = self._detail_provider_key
        title = self.query_one("#provider-detail-title", Static)
        detail_table = self.query_one("#provider-detail", DataTable)
        detail_table.clear()

        provider_status = status.get(key) if isinstance(status, dict) else None
        if not isinstance(provider_status, dict):
            title.update("Provider Detail")
            return

        label = next((p["label"] for p in PROVIDER_REGISTRY if p["key"] == key), key)
        circuit = "OPEN" if provider_status.get("circuit_open") else (
            "half" if provider_status.get("circuit_failure_count", 0) > 0 else "ok"
        )
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

        # Circuit breaker status (conditional, like the old per-provider tables)
        if provider_status.get("circuit_open"):
            detail_table.add_row(
                "Circuit",
                "OPEN (failures: {})".format(provider_status.get("circuit_failure_count", 0)),
            )
        elif provider_status.get("circuit_failure_count", 0) > 0:
            detail_table.add_row(
                "Circuit",
                "closed ({} failures)".format(provider_status.get("circuit_failure_count", 0)),
            )

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

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Follow the provider-grid cursor: re-render the detail pane."""
        if event.data_table.id != "provider-grid":
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

    @_safe_update
    def _update_derived_metrics(self, status: dict) -> None:
        """Update derived metrics panel (success rate, latency percentiles)."""
        panel = self.query_one("#derived-metrics-panel", Static)

        total_fwd = status.get("total_forwarded", 0)
        total_rejected = status.get("total_rejected", 0)
        total_429s = status.get("total_429s", 0)
        total_attempts = total_fwd + total_rejected + total_429s

        success_rate = (total_fwd / total_attempts * 100) if total_attempts > 0 else 100.0
        error_rate = ((total_rejected + total_429s) / total_attempts * 100) if total_attempts > 0 else 0.0

        lines = [
            f"  Success Rate: {success_rate:.1f}%  |  Error Rate: {error_rate:.1f}%",
            f"  Requests: {total_fwd} fwd  |  {total_429s} 429s  |  {total_rejected} rejected  |  {status.get('pending_requests', 0)} pending",
        ]
        panel.update("\n".join(lines))

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

    def _validate_status(self, status: dict) -> bool:
        """Check that the status dict has all required keys."""
        missing = self._REQUIRED_STATUS_KEYS - status.keys()
        if missing:
            return False
        return True

    def _poll_logs(self) -> None:
        """Append new log entries to the Log widgets."""
        # Overview tab: errors and warnings only (alert feed)
        self._append_logs_to_widget("#live-log", severity_filter="warnings_and_above")
        # Full log tab: all entries with user filters (if not paused)
        if not self._logs_paused:
            written = self._append_logs_to_widget("#live-log-full", apply_filter=True)
            self._displayed_log_count += written
            self._update_log_entry_count()

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

    def _write_log_entry(self, log_widget, entry: dict, apply_filter: bool) -> None:
        """Format and write a single log entry line to the widget."""
        level = entry.get("level", "INFO")
        ts = entry.get("timestamp", "")
        if isinstance(ts, str) and " " in ts:
            ts = ts.split(" ")[1] if len(ts.split(" ")) > 1 else ts
        log_widget.write_line(f"[{level}] {ts} -- {entry.get('message', '')}")
        if level == "ERROR" and not apply_filter:
            self.error_count += 1

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

    def _append_logs_to_widget(self, widget_id: str, apply_filter: bool = False, severity_filter: str | None = None) -> int:
        """Drain new log entries into a specific Log widget.

        Args:
            widget_id: The CSS selector for the Log widget.
            apply_filter: If True, apply user-level text/level/time filters (Logs tab).
            severity_filter: If set to "warnings_and_above", only show WARNING and ERROR.

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
            # Overview alert feed: only WARNING/ERROR.
            if severity_filter == "warnings_and_above" and level not in ("WARNING", "ERROR"):
                return False
            # Full log tab: apply user filters.
            if apply_filter and not self._log_entry_passes_user_filters(entry, level, msg):
                return False
            self._write_log_entry(log_widget, entry, apply_filter)
            return True

        new_seq, written = self._drain_log_buffer(start_seq, cap, write_entry)
        self._log_seqs[widget_id] = new_seq
        return written

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
            for entry in self._read_session_log_entries()[-10:]:
                if len(entry.get("attempted_providers", [])) > 1:
                    if is_failover_recent(entry.get("timestamp_utc")):
                        recent_failovers = True
                        break
            if failover_alert_should_show(warnings, recent_failovers):
                if "Failover" not in warnings:
                    warnings.append("Failover")

            if warnings:
                badge.update("ALERT: " + " | ".join(warnings) + " critical")
                badge.add_class("alert-active")
            else:
                badge.update("")
                badge.remove_class("alert-active")
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
            panel.update("")
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
        except NoMatches:
            return

        failovers = []
        for entry in self._read_session_log_entries():
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
        else:
            panel.update("  No recent failover events")

    @_safe_update
    def _update_model_table(self, status: dict) -> None:
        """Update per-model usage DataTable with multi-provider breakdown.
        
        Receives raw_status from MultiProviderRateLimiter.status() which has
        the shape {"primary": {...}, "secondary": {...}, "shared_limits": bool}.
        
        In multi-provider mode, aggregates stats across providers and shows
        breakdown rows for models served by multiple providers.
        """
        table = self.query_one("#model-usage-table", DataTable)
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

    def _update_config_table_filtered(self) -> None:
        """Update config table with current filter and grouping settings."""
        try:
            table = self.query_one("#config-table", DataTable)
        except NoMatches:
            return

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
        for widget_id in ["#live-log", "#live-log-full"]:
            try:
                log_widget = self.query_one(widget_id, Log)
                log_widget.clear()
            except NoMatches:
                pass
        self._log_seqs.clear()
        self.error_count = 0
        self._displayed_log_count = 0
        self.log_handler.clear()

    def switch_to(self, tab_id: str) -> None:
        """Switch to the specified tab."""
        try:
            tabs = self.query_one("#main-tabs", TabbedContent)
            tabs.active = tab_id
        except NoMatches:
            pass

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
            self._update_config_table_filtered()
        elif event.button.id == "config-grouped-btn":
            self._config_grouped = not self._config_grouped
            event.button.variant = "primary" if self._config_grouped else "default"
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
                self._poll_logs()
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
                self._poll_logs()
            except NoMatches:
                pass
        elif event.select.id == "log-time-range":
            # Clear log widget and re-poll from the start to apply time filter
            try:
                log_widget = self.query_one("#live-log-full", Log)
                log_widget.clear()
                self._log_seqs["#live-log-full"] = 0
                self._displayed_log_count = 0
                self._poll_logs()
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

    async def on_shutdown(self) -> None:
        """Signal proxy shutdown and cancel background worker when TUI exits."""
        self._signal_proxy_shutdown()
