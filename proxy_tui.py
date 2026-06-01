"""Textual TUI dashboard for the DashScope proxy server."""

import time
import math
import statistics
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
import dashscope_proxy


def _fmt_number(n: int) -> str:
    """Format large numbers with suffixes."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _progress_bar(current: int, maximum: int, width: int = 20) -> str:
    """Return a text-based progress bar."""
    if maximum <= 0:
        filled = 0
    else:
        filled = int((current / maximum) * width)
    filled = min(filled, width)
    empty = width - filled
    bar = "[" + "#" * filled + "." * empty + "]"
    return f"{bar} {_fmt_number(current)}/{_fmt_number(maximum)}"


@dataclass
class MetricHistory:
    """Fixed-length history buffer for sparkline rendering."""
    max_length: int = 120
    rpm_samples: list[int] = field(default_factory=list)
    tpm_used_samples: list[int] = field(default_factory=list)
    queue_samples: list[int] = field(default_factory=list)

    def append(self, rpm: int, tpm_used: int, queue_depth: int):
        self.rpm_samples.append(rpm)
        self.tpm_used_samples.append(tpm_used)
        self.queue_samples.append(queue_depth)
        if len(self.rpm_samples) > self.max_length:
            self.rpm_samples = self.rpm_samples[-self.max_length:]
        if len(self.tpm_used_samples) > self.max_length:
            self.tpm_used_samples = self.tpm_used_samples[-self.max_length:]
        if len(self.queue_samples) > self.max_length:
            self.queue_samples = self.queue_samples[-self.max_length:]


@dataclass
class LatencyTracker:
    """Tracks request latencies for percentile computation."""
    max_length: int = 1000
    latencies: list[float] = field(default_factory=list)

    def add(self, ms: float):
        self.latencies.append(ms)
        if len(self.latencies) > self.max_length:
            self.latencies = self.latencies[-self.max_length:]

    def add_batch(self, values: list[float]):
        for v in values:
            self.add(v)

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

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "clear_logs", "Clear Logs"),
        ("1", "switch_to('tab-overview')", "Overview"),
        ("2", "switch_to('tab-metrics')", "Metrics"),
        ("3", "switch_to('tab-logs')", "Logs"),
        ("4", "switch_to('tab-models')", "Models"),
        ("5", "switch_to('tab-config')", "Config"),
    ]

    last_log_seq: int = 0
    error_count: int = 0
    _poll_error_count: int = 0

    _REQUIRED_STATUS_KEYS = {
        "rps_limit", "rpm_limit", "rpm_current", "tpm_limit", "tpm_available",
        "tpm_reserved", "requests_5h", "requests_5h_limit", "requests_week",
        "requests_week_limit", "requests_month", "requests_month_limit",
        "total_forwarded", "total_queued", "total_429s", "total_rejected",
        "total_tokens_consumed", "pending_requests",
    }

    def __init__(
        self,
        rate_limiter: dashscope_proxy.RateLimiter,
        tui_log_handler: dashscope_proxy.TUILogHandler,
        proxy_app,
    ):
        super().__init__()
        self.rate_limiter = rate_limiter
        self.log_handler = tui_log_handler
        self.proxy_app = proxy_app
        self.history = MetricHistory()
        self.latency_tracker = LatencyTracker()
        self._config_snapshot: dict = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="main-tabs", initial="tab-overview"):
            # Tab 1: Overview (enhanced existing dashboard)
            with TabPane("Overview", id="tab-overview"):
                with Horizontal():
                    with Vertical(id="metrics-panel"):
                        yield Static("Proxy: Checking...", id="status-indicator")
                        yield Static("Rate Limiter", classes="panel-title")
                        yield DataTable(id="rl-metrics")
                        yield Static("", classes="spacer")
                        yield Static("Request Statistics", classes="panel-title")
                        yield DataTable(id="request-stats")
                    with Vertical(id="log-panel"):
                        yield Static("Live Logs", classes="panel-title")
                        yield Static("", id="error-banner", classes="error-banner")
                        yield Log(id="live-log")
                        yield Static("", id="token-status", classes="token-bar")

            # Tab 2: Metrics (sparklines + derived metrics + timers)
            with TabPane("Metrics", id="tab-metrics"):
                with Vertical(id="metrics-tab-content"):
                    yield Horizontal(
                        Static("RPM", id="rpm-sparkline-label", classes="sparkline-label"),
                        Sparkline([], id="rpm-sparkline"),
                        Static("TPM Used", id="tpm-sparkline-label", classes="sparkline-label"),
                        Sparkline([], id="tpm-sparkline"),
                        Static("Queue", id="queue-sparkline-label", classes="sparkline-label"),
                        Sparkline([], id="queue-sparkline"),
                        id="sparkline-row",
                    )
                    yield Static("", id="derived-metrics-panel", classes="derived-panel")
                    yield Static("", id="quota-timers-panel", classes="timers-panel")
                    yield Static("", id="poll-error-banner", classes="poll-error")

            # Tab 3: Logs (full log viewer with filter)
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
                        Button("Clear", id="clear-logs-btn", variant="error"),
                        id="filter-bar",
                    )
                    yield Log(id="live-log-full")

            # Tab 4: Models (per-model breakdown)
            with TabPane("Models", id="tab-models"):
                yield DataTable(id="model-usage-table")

            # Tab 5: Config (read-only config viewer)
            with TabPane("Config", id="tab-config"):
                yield DataTable(id="config-table")

        yield Footer()

    async def on_mount(self) -> None:
        """Initialize data tables and start background polling."""
        self._config_snapshot = dashscope_proxy._load_config()

        # Configure rate limiter metrics table
        rl_table = self.query_one("#rl-metrics", DataTable)
        rl_table.add_columns("Metric", "Value")
        rl_table.show_header = False
        rl_table.zebra_stripes = True

        # Configure request statistics table
        stats_table = self.query_one("#request-stats", DataTable)
        stats_table.add_columns("Statistic", "Value")
        stats_table.show_header = False
        stats_table.zebra_stripes = True

        # Configure model usage table
        try:
            model_table = self.query_one("#model-usage-table", DataTable)
            model_table.add_columns("Model", "Requests", "Tokens", "429s", "Avg Latency")
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
        table.clear()
        env_prefix = "PROXY_"
        for key, value in sorted(self._config_snapshot.items()):
            source = "env" if env_prefix + key.upper() in __import__("os").environ else "default"
            table.add_row(key, str(value), source)

    def _poll_loop(self) -> None:
        """Threaded loop that polls rate limiter and log handler with adaptive timing."""
        worker = get_current_worker()
        base_interval = 1.0
        consecutive_errors = 0
        max_consecutive_errors = 5

        while not worker.is_cancelled:
            interval = base_interval
            status = None
            try:
                status = self.rate_limiter.status()

                # Update history buffers for sparklines
                tpm_used = status.get("tpm_limit", 0) - status.get("tpm_available", 0)
                self.history.append(
                    rpm=status.get("rpm_current", 0),
                    tpm_used=tpm_used,
                    queue_depth=status.get("pending_requests", 0),
                )

                # Update latency tracker
                self.latency_tracker.add_batch(status.get("recent_latencies", []))

                # Update all UI components
                self.call_from_thread(self._update_metrics, status)
                self.call_from_thread(self._poll_logs)
                self.call_from_thread(self._update_sparklines)
                self.call_from_thread(self._update_derived_metrics, status)
                self.call_from_thread(self._update_quota_timers, status)
                self.call_from_thread(self._update_model_table, status)

                consecutive_errors = 0

                # Adaptive: poll faster when activity detected
                if status.get("pending_requests", 0) > 0:
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

            time.sleep(interval)

    def _show_poll_error(self) -> None:
        """Display a transient error banner when polling fails."""
        try:
            banner = self.query_one("#poll-error-banner", Static)
            banner.update(f"Poll error ({self._poll_error_count} errors) -- reconnecting...")
        except NoMatches:
            pass

    @_safe_update
    def _update_metrics(self, status: dict) -> None:
        """Refresh the metrics DataTable widgets (Overview tab)."""
        if not self._validate_status(status):
            return

        # Update connection status indicator
        status_widget = self.query_one("#status-indicator", Static)
        session = self.proxy_app.get("client_session")
        is_running = session is not None and not session.closed
        uptime = status.get("uptime_seconds", 0)
        status_text = f"Proxy: Running (uptime: {self._format_uptime(uptime)})" if is_running else "Proxy: Stopped"
        status_widget.update(status_text)
        status_widget.set_class(is_running, "status-running")
        status_widget.set_class(not is_running, "status-stopped")

        rl_table = self.query_one("#rl-metrics", DataTable)
        rl_table.clear()
        rl_table.add_row("RPS Limit", str(status.get("rps_limit", 0)))

        # RPM with reset timer
        rpm_current = status.get("rpm_current", 0)
        rpm_limit = status.get("rpm_limit", 1)
        rpm_reset_in = max(0, 60 - (time.time() % 60))
        rl_table.add_row(
            "RPM",
            f"{_progress_bar(rpm_current, rpm_limit)} | resets in {int(rpm_reset_in)}s",
        )

        rl_table.add_row(
            "TPM Available",
            _progress_bar(status.get("tpm_available", 0), status.get("tpm_limit", 1)),
        )
        rl_table.add_row(
            "5-Hour Quota",
            _progress_bar(status.get("requests_5h", 0), status.get("requests_5h_limit", 1)),
        )
        rl_table.add_row(
            "Weekly Quota",
            _progress_bar(status.get("requests_week", 0), status.get("requests_week_limit", 1)),
        )
        rl_table.add_row(
            "Monthly Quota",
            _progress_bar(status.get("requests_month", 0), status.get("requests_month_limit", 1)),
        )

        # Circuit breaker status
        if status.get("circuit_open"):
            rl_table.add_row("Circuit", "OPEN (failures: {})".format(status.get("circuit_failure_count", 0)))
        elif status.get("circuit_failure_count", 0) > 0:
            rl_table.add_row("Circuit", "closed ({} failures)".format(status.get("circuit_failure_count", 0)))

        # Quota warning row
        warning = self._quota_warning(status)
        if warning:
            rl_table.add_row("Warning", warning)

        # Request statistics with derived success rate
        stats_table = self.query_one("#request-stats", DataTable)
        stats_table.clear()
        total_fwd = status.get("total_forwarded", 0)
        total_rejected = status.get("total_rejected", 0)
        total_429s = status.get("total_429s", 0)
        total_attempts = total_fwd + total_rejected + total_429s
        success_rate = (total_fwd / total_attempts * 100) if total_attempts > 0 else 0.0

        stats_table.add_row("Total Forwarded", _fmt_number(total_fwd))
        stats_table.add_row("Success Rate", f"{success_rate:.1f}%")
        stats_table.add_row("Total Queued", _fmt_number(status.get("total_queued", 0)))
        stats_table.add_row("Total 429s", _fmt_number(total_429s))
        stats_table.add_row("Total Rejected", _fmt_number(total_rejected))
        stats_table.add_row("Pending", str(status.get("pending_requests", 0)))
        stats_table.add_row(
            "Tokens Consumed",
            _fmt_number(status.get("total_tokens_consumed", 0)),
        )

        # Token status bar
        token_bar = self.query_one("#token-status", Static)
        token_bar.update(
            f"TPM: {_fmt_number(status.get('tpm_available', 0))} available | "
            f"{_fmt_number(status.get('tpm_reserved', 0))} reserved | "
            f"{_fmt_number(status.get('tpm_limit', 0))} capacity"
        )

        # Update error banner
        error_banner = self.query_one("#error-banner", Static)
        if self.error_count > 0:
            error_banner.update(f"{self.error_count} error(s) in recent logs")
        else:
            error_banner.update("")

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
            f"  Latency -- avg: {self.latency_tracker.avg()}ms  |  p50: {self.latency_tracker.p50()}ms  |  p95: {self.latency_tracker.p95()}ms  |  p99: {self.latency_tracker.p99()}ms",
            f"  Requests: {total_fwd} fwd  |  {total_429s} 429s  |  {total_rejected} rejected  |  {status.get('pending_requests', 0)} pending",
        ]
        panel.update("\n".join(lines))

    @_safe_update
    def _update_quota_timers(self, status: dict) -> None:
        """Update quota reset timers panel."""
        panel = self.query_one("#quota-timers-panel", Static)

        now = time.time()
        rpm_reset = max(0, 60 - (now % 60))

        # Week/month resets are based on when the window started
        week_elapsed = now - getattr(self.rate_limiter, 'week_start', now)
        week_reset = max(0, 7 * 24 * 3600 - week_elapsed)
        month_elapsed = now - getattr(self.rate_limiter, 'month_start', now)
        month_reset = max(0, 30 * 24 * 3600 - month_elapsed)
        # 5-hour window reset
        hour5_reset = max(0, 5 * 3600 - (now % (5 * 3600)))

        lines = [
            f"  RPM resets in:       {int(rpm_reset)}s",
            f"  5-Hour resets in:    {self._fmt_time_delta(hour5_reset)}",
            f"  Week resets in:      {self._fmt_time_delta(week_reset)}",
            f"  Month resets in:     {self._fmt_time_delta(month_reset)}",
        ]
        panel.update("\n".join(lines))

    @_safe_update
    def _update_model_table(self, status: dict) -> None:
        """Update per-model usage DataTable."""
        table = self.query_one("#model-usage-table", DataTable)
        table.clear()

        model_usage = status.get("model_usage", {})
        # Sort by request count descending
        sorted_models = sorted(model_usage.items(), key=lambda x: x[1]["requests"], reverse=True)

        if not sorted_models:
            table.add_row("--", "No model data yet", "", "", "")
        else:
            for model_name, stats in sorted_models:
                table.add_row(
                    model_name,
                    str(stats["requests"]),
                    _fmt_number(stats["tokens"]),
                    str(stats["errors_429"]),
                    f"{stats['avg_latency_ms']:.0f}ms",
                )

    def _quota_warning(self, status: dict) -> str:
        """Return a warning string if any quota is near its limit."""
        warnings = []
        thresholds = [
            ("RPM", status.get("rpm_current", 0), status.get("rpm_limit", 1)),
            ("TPM", status.get("tpm_available", 0), status.get("tpm_limit", 1)),
            ("5H", status.get("requests_5h", 0), status.get("requests_5h_limit", 1)),
            ("Week", status.get("requests_week", 0), status.get("requests_week_limit", 1)),
            ("Month", status.get("requests_month", 0), status.get("requests_month_limit", 1)),
        ]
        for name, used, limit in thresholds:
            if limit > 0 and used / limit >= 0.9:
                warnings.append(name)
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
        # Try overview tab log
        self._append_logs_to_widget("#live-log")
        # Try full log tab (if filter matches)
        self._append_logs_to_widget("#live-log-full", apply_filter=True)

    def _append_logs_to_widget(self, widget_id: str, apply_filter: bool = False) -> None:
        """Append new log entries to a specific Log widget, optionally applying filters."""
        try:
            log_widget = self.query_one(widget_id, Log)
        except NoMatches:
            return

        new_entries = self.log_handler.get_logs(limit=50, from_seq=self.last_log_seq)
        for entry in new_entries:
            level = entry.get("level", "INFO")
            msg = entry.get("message", "")

            # Apply filters for the full log tab
            if apply_filter:
                try:
                    level_filter = self.query_one("#log-level-filter", Select).value
                    if level_filter != "ALL" and level != level_filter:
                        continue
                    text_filter = self.query_one("#log-filter", Input).value
                    if text_filter and text_filter.lower() not in msg.lower():
                        continue
                except NoMatches:
                    pass

            ts = entry.get("timestamp", "")
            if isinstance(ts, str) and " " in ts:
                ts = ts.split(" ")[1] if len(ts.split(" ")) > 1 else ts
            line = f"[{level}] {ts} -- {msg}"
            log_widget.write(line)
            if level == "ERROR" and not apply_filter:
                self.error_count += 1

        if new_entries:
            self.last_log_seq = max(e.get("seq", 0) for e in new_entries) + 1

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

    def _fmt_time_delta(self, seconds: float) -> str:
        """Format a time delta as human-readable string."""
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
        self.last_log_seq = 0
        self.error_count = 0
        self.log_handler.clear()

    def switch_to(self, tab_id: str) -> None:
        """Switch to the specified tab."""
        try:
            tabs = self.query_one("#main-tabs", TabbedContent)
            tabs.active = tab_id
        except NoMatches:
            pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses (e.g., clear logs button)."""
        if event.button.id == "clear-logs-btn":
            self.action_clear_logs()

    async def on_shutdown(self) -> None:
        """Signal proxy shutdown and cancel background worker when TUI exits."""
        for worker in self.workers:
            worker.cancel()
        shutdown_event = self.proxy_app.get("shutting_down")
        if shutdown_event and not shutdown_event.is_set():
            shutdown_event.set()
