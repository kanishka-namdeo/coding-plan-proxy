"""Textual TUI dashboard for the DashScope proxy server."""

import time
import math
import statistics
import threading
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
from dashscope_proxy_lib.config import _load_config
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
]


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
        self._config_snapshot: dict = {}

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

        # Alert tracking
        self._active_alerts: list[dict] = []

        # Cancellable sleep for the poll worker
        self._shutdown_event = threading.Event()

        # Poller state
        self.error_count = 0
        self._poll_error_count = 0

        # Keys that must be present in rate_limiter.status() for metrics updates
        self._REQUIRED_STATUS_KEYS: set[str] = {
            "rps_limit", "rpm_limit", "rpm_current",
            "tpm_limit", "tpm_available", "tpm_reserved",
            "requests_5h", "requests_5h_limit",
            "requests_week", "requests_week_limit",
            "requests_month", "requests_month_limit",
            "total_forwarded", "queue_drops", "total_429s", "total_rejected",
            "total_tokens_consumed", "total_request_bytes", "total_response_bytes",
            "pending_requests", "circuit_open", "circuit_failure_count",
            "model_usage", "recent_latencies", "uptime_seconds",
        }

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="main-tabs", initial="tab-overview"):
            # Tab 1: Overview (primary operational dashboard)
            with TabPane("Overview", id="tab-overview"):
                with Horizontal():
                    with Vertical(id="metrics-panel"):
                        yield Static("Proxy: Checking...", id="status-indicator")
                        yield Static("", id="alert-badge", classes="alert-badge")
                        yield Static("Primary Provider", classes="panel-title")
                        yield Static("Rate Limiter", classes="panel-title")
                        yield DataTable(id="rl-metrics")
                        
                        # Dynamic provider sections (secondary through senary)
                        for provider_info in PROVIDER_REGISTRY[1:]:  # Skip primary
                            provider_key = provider_info["key"]
                            label = provider_info["label"]
                            with Vertical(id=f"{provider_key}-overview"):
                                yield Static(label, classes="panel-title")
                                yield Static(f"{label}: Not configured", id=f"{provider_key}-status-line")
                                yield DataTable(id=f"{provider_key}-rl-metrics")
                        
                        yield Static("", classes="spacer")
                        yield Static("Request Statistics", classes="panel-title")
                        yield DataTable(id="request-stats")
                    with Vertical(id="log-panel"):
                        yield Static("Alerts & Warnings", classes="panel-title")
                        yield Static("", id="error-banner", classes="error-banner")
                        yield Log(id="live-log")

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
                    yield Log(id="live-log-full")

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
        self._config_snapshot = _load_config()

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
            model_table.add_columns("Model", "Provider", "Requests", "Tokens", "429s", "Avg Latency", "p50", "p95")
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

        # Configure dynamic provider metrics tables
        for provider_info in PROVIDER_REGISTRY[1:]:  # Skip primary
            provider_key = provider_info["key"]
            try:
                table = self.query_one(f"#{provider_key}-rl-metrics", DataTable)
                table.add_columns("Metric", "Value")
                table.show_header = False
                table.zebra_stripes = True
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
                # Respect cancellation even during sleep
                if worker.is_cancelled:
                    break
                raw_status = self.rate_limiter.status()

                # Handle multi-provider status structure
                # MultiProviderRateLimiter.status() returns {"primary": {...}, "secondary": {...}}
                # Single RateLimiter.status() returns flat dict with rate limit keys
                if "primary" in raw_status:
                    # Multi-provider mode - use primary for main display
                    status = raw_status["primary"]
                    # Store full status for model table and secondary tab
                    status["_multi_provider"] = raw_status
                else:
                    # Single provider mode
                    status = raw_status

                # Update history buffers for sparklines
                tpm_used = status.get("tpm_limit", 0) - status.get("tpm_available", 0)
                # Compute avg upstream latency from recent latencies
                recent_lats = status.get("recent_latencies", [])
                avg_upstream = statistics.mean(recent_lats) if recent_lats else 0.0
                self.history.append(
                    rpm=status.get("rpm_current", 0),
                    tpm_used=tpm_used,
                    queue_depth=status.get("pending_requests", 0),
                    upstream_latency_ms=avg_upstream,
                )

                # Update latency tracker with fresh data (replace to avoid duplicates)
                self.latency_tracker.latencies = list(status.get("recent_latencies", []))

                # Update all UI components
                self.call_from_thread(self._update_metrics, raw_status)
                self.call_from_thread(self._poll_logs)
                self.call_from_thread(self._update_sparklines)
                self.call_from_thread(self._update_derived_metrics, raw_status)
                self.call_from_thread(self._update_latency_histogram)
                self.call_from_thread(self._update_model_table, raw_status)
                self.call_from_thread(self._update_config_table_filtered)
                self.call_from_thread(self._update_secondary_metrics, raw_status)
                self.call_from_thread(self._update_tertiary_metrics, raw_status)
                self.call_from_thread(self._update_quaternary_metrics, raw_status)
                self.call_from_thread(self._update_quinary_metrics, raw_status)

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

        # Primary provider rate limiter metrics
        rl_table = self.query_one("#rl-metrics", DataTable)
        rl_table.clear()
        rl_table.add_row("RPS Limit", str(primary.get("rps_limit", 0)))

        rpm_current = primary.get("rpm_current", 0)
        rpm_limit = primary.get("rpm_limit", 1)
        rl_table.add_row(
            "RPM",
            _progress_bar(rpm_current, rpm_limit),
        )

        rl_table.add_row(
            "TPM Available",
            _progress_bar(primary.get("tpm_available", 0), primary.get("tpm_limit", 1)),
        )

        rl_table.add_row(
            "5-Hour Quota",
            _progress_bar(primary.get('requests_5h', 0), primary.get('requests_5h_limit', 1)),
        )
        rl_table.add_row(
            "Weekly Quota",
            _progress_bar(primary.get('requests_week', 0), primary.get('requests_week_limit', 1)),
        )
        rl_table.add_row(
            "Monthly Quota",
            _progress_bar(primary.get('requests_month', 0), primary.get('requests_month_limit', 1)),
        )

        # Circuit breaker status (primary)
        if primary.get("circuit_open"):
            rl_table.add_row("Circuit", "OPEN (failures: {})".format(primary.get("circuit_failure_count", 0)))
        elif primary.get("circuit_failure_count", 0) > 0:
            rl_table.add_row("Circuit", "closed ({} failures)".format(primary.get("circuit_failure_count", 0)))

        # Token summary (primary)
        rl_table.add_row(
            "Tokens",
            f"{_fmt_number(primary.get('total_tokens_consumed', 0))} consumed | "
            f"{_fmt_number(primary.get('tpm_reserved', 0))} reserved | "
            f"{_fmt_number(primary.get('tpm_limit', 0))} capacity",
        )

        # Quota warning row (primary only)
        warning = self._quota_warning(primary)
        if warning:
            rl_table.add_row("Warning", warning)

        # Request statistics — use AGGREGATED totals across all providers
        stats_table = self.query_one("#request-stats", DataTable)
        stats_table.clear()
        total_fwd = multi.get("total_forwarded", 0)
        total_rejected = multi.get("total_rejected", 0)
        total_429s = multi.get("total_429s", 0)
        total_attempts = total_fwd + total_rejected + total_429s
        success_rate = (total_fwd / total_attempts * 100) if total_attempts > 0 else 0.0

        total_request_bytes = multi.get("total_request_bytes", 0)
        total_response_bytes = multi.get("total_response_bytes", 0)
        avg_req_size = _fmt_number(total_request_bytes // total_fwd) if total_fwd > 0 else "0"
        avg_resp_size = _fmt_number(total_response_bytes // total_fwd) if total_fwd > 0 else "0"

        # Show per-provider breakdown when secondary/tertiary/quaternary are active
        if "primary" in status:
            pri_fwd = primary.get("total_forwarded", 0)
            total_fwd_all = pri_fwd
            stats_table.add_row("Forwarded (Primary)", _fmt_number(pri_fwd))
            if status.get("secondary"):
                sec = status["secondary"]
                sec_fwd = sec.get("total_forwarded", 0)
                stats_table.add_row("Forwarded (Secondary)", _fmt_number(sec_fwd))
                stats_table.add_row("429s (Secondary)", str(sec.get("total_429s", 0)))
                total_fwd_all += sec_fwd
            if status.get("tertiary"):
                ter = status["tertiary"]
                ter_fwd = ter.get("total_forwarded", 0)
                stats_table.add_row("Forwarded (OpenLux)", _fmt_number(ter_fwd))
                stats_table.add_row("429s (OpenLux)", str(ter.get("total_429s", 0)))
                total_fwd_all += ter_fwd
            if status.get("quaternary"):
                qua = status["quaternary"]
                qua_fwd = qua.get("total_forwarded", 0)
                stats_table.add_row("Forwarded (ARK)", _fmt_number(qua_fwd))
                stats_table.add_row("429s (ARK)", str(qua.get("total_429s", 0)))
                total_fwd_all += qua_fwd
            if status.get("quinary"):
                qui = status["quinary"]
                qui_fwd = qui.get("total_forwarded", 0)
                stats_table.add_row("Forwarded (Meta AI)", _fmt_number(qui_fwd))
                stats_table.add_row("429s (Meta AI)", str(qui.get("total_429s", 0)))
                total_fwd_all += qui_fwd
            # Show aggregate totals when multiple providers are active
            has_fallback = status.get("secondary") or status.get("tertiary") or status.get("quaternary") or status.get("quinary")
            if has_fallback:
                stats_table.add_row("Total 429s", str(total_429s))
                stats_table.add_row("Total Forwarded", _fmt_number(total_fwd))
                stats_table.add_row("429s (Primary)", str(primary.get("total_429s", 0)))
            else:
                stats_table.add_row("Total Forwarded", _fmt_number(total_fwd))
        else:
            stats_table.add_row("Total Forwarded", _fmt_number(total_fwd))

        stats_table.add_row("Queue Drops", str(primary.get("queue_drops", 0)))
        queue_p50 = primary.get("queue_p50_ms", 0)
        queue_p95 = primary.get("queue_p95_ms", 0)
        queue_p99 = primary.get("queue_p99_ms", 0)
        stats_table.add_row("Queue Wait (p50/p95/p99)", f"{queue_p50}ms / {queue_p95}ms / {queue_p99}ms")
        stats_table.add_row(
            "Tokens Consumed",
            _fmt_number(multi.get("total_tokens_consumed", 0)),
        )
        stats_table.add_row("Avg Req Size", avg_req_size)
        stats_table.add_row("Avg Resp Size", avg_resp_size)

        # Update error banner
        error_banner = self.query_one("#error-banner", Static)
        if self.error_count > 0:
            error_banner.update(f"{self.error_count} error(s) in recent logs")
        else:
            error_banner.update("")

    @_safe_update
    def _update_provider_metrics(self, provider_key: str, status: dict) -> None:
        """Update metrics for a single provider.
        
        Args:
            provider_key: Provider key from registry (e.g., "secondary", "senary")
            status: Raw status dict from MultiProviderRateLimiter.status()
        """
        # Query UI elements using provider_key
        try:
            overview = self.query_one(f"#{provider_key}-overview", Vertical)
            status_line = self.query_one(f"#{provider_key}-status-line", Static)
            metrics_table = self.query_one(f"#{provider_key}-rl-metrics", DataTable)
        except NoMatches:
            return

        # Check visibility conditions
        if provider_key not in status:
            overview.set_class(False, "visible")
            return

        provider_status = status.get(provider_key)
        
        if not provider_status:
            overview.set_class(False, "visible")
            return

        # Only show if provider has served at least one request
        if provider_status.get("total_forwarded", 0) == 0:
            overview.set_class(False, "visible")
            return

        # Provider is active - show the section
        overview.set_class(True, "visible")

        # Update status line with base URL from config
        # Find provider info from registry
        provider_info = None
        for entry in PROVIDER_REGISTRY:
            if entry["key"] == provider_key:
                provider_info = entry
                break
        
        if provider_info:
            config_key = provider_info["config_key"]
            # Import the config value dynamically
            from dashscope_proxy_lib import config as proxy_config
            base_url = getattr(proxy_config, config_key, None)
            base_url_display = base_url or "N/A"
            status_line.update(f"Status: Active | Target: {base_url_display}")
            status_line.set_class(True, "status-running")
            status_line.set_class(False, "status-stopped")

        # Populate metrics table
        metrics_table.clear()

        metrics_table.add_row("RPS Limit", str(provider_status.get("rps_limit", 0)))

        rpm_current = provider_status.get("rpm_current", 0)
        rpm_limit = provider_status.get("rpm_limit", 1)
        metrics_table.add_row(
            "RPM",
            _progress_bar(rpm_current, rpm_limit),
        )

        metrics_table.add_row(
            "TPM Available",
            _progress_bar(
                provider_status.get("tpm_available", 0),
                provider_status.get("tpm_limit", 1)
            ),
        )

        metrics_table.add_row(
            "5-Hour Quota",
            _progress_bar(
                provider_status.get("requests_5h", 0),
                provider_status.get("requests_5h_limit", 1)
            ),
        )

        metrics_table.add_row(
            "Weekly Quota",
            _progress_bar(
                provider_status.get("requests_week", 0),
                provider_status.get("requests_week_limit", 1)
            ),
        )

        metrics_table.add_row(
            "Monthly Quota",
            _progress_bar(
                provider_status.get("requests_month", 0),
                provider_status.get("requests_month_limit", 1)
            ),
        )

        # Circuit breaker status
        if provider_status.get("circuit_open"):
            metrics_table.add_row("Circuit", "OPEN (failures: {})".format(
                provider_status.get("circuit_failure_count", 0)))
        elif provider_status.get("circuit_failure_count", 0) > 0:
            metrics_table.add_row("Circuit", "closed ({} failures)".format(
                provider_status.get("circuit_failure_count", 0)))

        # Token summary
        metrics_table.add_row(
            "Tokens",
            f"{_fmt_number(provider_status.get('total_tokens_consumed', 0))} consumed | "
            f"{_fmt_number(provider_status.get('tpm_reserved', 0))} reserved | "
            f"{_fmt_number(provider_status.get('tpm_limit', 0))} capacity",
        )

        # Request stats
        metrics_table.add_row(
            "Forwarded",
            f"{_fmt_number(provider_status.get('total_forwarded', 0))} | "
            f"429s: {provider_status.get('total_429s', 0)} | "
            f"Rejected: {provider_status.get('total_rejected', 0)}",
        )

        # Quota warning
        warning = self._quota_warning(provider_status)
        if warning:
            metrics_table.add_row("Warning", warning)

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
            ("5H", status.get("requests_5h", 0), status.get("requests_5h_limit", 1)),
            ("Week", status.get("requests_week", 0), status.get("requests_week_limit", 1)),
            ("Month", status.get("requests_month", 0), status.get("requests_month_limit", 1)),
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

    def _append_logs_to_widget(self, widget_id: str, apply_filter: bool = False, severity_filter: str | None = None) -> int:
        """Append new log entries to a specific Log widget.

        Args:
            widget_id: The CSS selector for the Log widget.
            apply_filter: If True, apply user-level text/level/time filters (Logs tab).
            severity_filter: If set to "warnings_and_above", only show WARNING and ERROR.
        """
        try:
            log_widget = self.query_one(widget_id, Log)
        except NoMatches:
            return 0

        seq = self._log_seqs.get(widget_id, 0)
        new_entries = self.log_handler.get_logs(limit=50, from_seq=seq)
        written_count = 0
        for entry in new_entries:
            level = entry.get("level", "INFO")
            msg = entry.get("message", "")

            # Apply severity filter for the overview alert feed
            if severity_filter == "warnings_and_above" and level not in ("WARNING", "ERROR"):
                continue

            # Apply user filters for the full log tab
            if apply_filter:
                try:
                    level_filter = self.query_one("#log-level-filter", Select).value
                    if level_filter != "ALL" and level != level_filter:
                        continue
                    text_filter = self.query_one("#log-filter", Input).value
                    if text_filter and text_filter.lower() not in msg.lower():
                        continue
                    time_range = self.query_one("#log-time-range", Select).value
                    if time_range != "ALL":
                        entry_ts = entry.get("timestamp", "")
                        if entry_ts:
                            try:
                                dt = datetime.fromisoformat(entry_ts)
                                cutoff_map = {"5m": 300, "1h": 3600, "24h": 86400}
                                if time_range in cutoff_map:
                                    if time.time() - dt.timestamp() > cutoff_map[time_range]:
                                        continue
                            except Exception:
                                pass
                except NoMatches:
                    pass

            ts = entry.get("timestamp", "")
            if isinstance(ts, str) and " " in ts:
                ts = ts.split(" ")[1] if len(ts.split(" ")) > 1 else ts
            line = f"[{level}] {ts} -- {msg}"
            log_widget.write_line(line)
            written_count += 1
            if level == "ERROR" and not apply_filter:
                self.error_count += 1

        if new_entries:
            self._log_seqs[widget_id] = max(e.get("seq", 0) for e in new_entries) + 1

        return written_count

    def _update_alert_badge(self, status: dict) -> None:
        """Update alert badge based on quota usage and circuit status.

        In multi-provider mode, checks quotas and circuit breakers for ALL
        providers. In single-provider mode, checks the flat status dict.
        """
        try:
            badge = self.query_one("#alert-badge", Static)
            warnings = []

            if "primary" in status:
                # Multi-provider mode: check all providers
                for provider_key in ("primary", "secondary", "tertiary", "quaternary", "quinary"):
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

            if warnings:
                badge.update("ALERT: " + " | ".join(warnings) + " critical")
                badge.add_class("alert-active")
            else:
                badge.update("")
                badge.remove_class("alert-active")
        except NoMatches:
            pass

    def _update_latency_histogram(self) -> None:
        """Update latency histogram visualization in Metrics tab."""
        try:
            panel = self.query_one("#latency-histogram-panel", Static)
        except NoMatches:
            return

        latencies = self.latency_tracker.latencies
        if not latencies:
            panel.update("")
            return

        # Define buckets
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
        
        p50 = self.latency_tracker.p50()
        p95 = self.latency_tracker.p95()
        p99 = self.latency_tracker.p99()
        avg = self.latency_tracker.avg()
        lines.append(f"  avg: {avg}ms | p50: {p50}ms | p95: {p95}ms | p99: {p99}ms")
        
        panel.update("\n".join(lines))

    @_safe_update
    def _update_secondary_metrics(self, status: dict) -> None:
        """Update secondary provider metrics in the Overview tab.

        Receives raw_status from MultiProviderRateLimiter.status() which has
        the shape {"primary": {...}, "secondary": {...}, "shared_limits": bool}.
        Toggles visibility of the secondary-overview section based on whether
        the secondary provider is configured.
        """
        try:
            secondary_overview = self.query_one("#secondary-overview", Vertical)
            secondary_status_line = self.query_one("#secondary-status-line", Static)
            secondary_table = self.query_one("#secondary-rl-metrics", DataTable)
        except NoMatches:
            return

        # MultiProviderRateLimiter.status() always has "primary" key.
        # If "secondary" key is absent, it's a plain RateLimiter (single provider).
        if "secondary" not in status:
            secondary_overview.set_class(False, "visible")
            return

        secondary_status = status.get("secondary")

        if not secondary_status:
            secondary_overview.set_class(False, "visible")
            return

        # Only show if provider has served at least one request
        if secondary_status.get("total_forwarded", 0) == 0:
            secondary_overview.set_class(False, "visible")
            return

        # Secondary provider is active - show the section
        secondary_overview.set_class(True, "visible")

        # Update status line with base URL
        from dashscope_proxy_lib.config import SECONDARY_BASE_URL
        base_url_display = SECONDARY_BASE_URL or "N/A"
        secondary_status_line.update(f"Status: Active | Target: {base_url_display}")
        secondary_status_line.set_class(True, "status-running")
        secondary_status_line.set_class(False, "status-stopped")

        # Populate secondary metrics table
        secondary_table.clear()

        secondary_table.add_row("RPS Limit", str(secondary_status.get("rps_limit", 0)))

        rpm_current = secondary_status.get("rpm_current", 0)
        rpm_limit = secondary_status.get("rpm_limit", 1)
        secondary_table.add_row(
            "RPM",
            _progress_bar(rpm_current, rpm_limit),
        )

        secondary_table.add_row(
            "TPM Available",
            _progress_bar(
                secondary_status.get("tpm_available", 0),
                secondary_status.get("tpm_limit", 1)
            ),
        )

        secondary_table.add_row(
            "5-Hour Quota",
            _progress_bar(
                secondary_status.get("requests_5h", 0),
                secondary_status.get("requests_5h_limit", 1)
            ),
        )

        secondary_table.add_row(
            "Weekly Quota",
            _progress_bar(
                secondary_status.get("requests_week", 0),
                secondary_status.get("requests_week_limit", 1)
            ),
        )

        secondary_table.add_row(
            "Monthly Quota",
            _progress_bar(
                secondary_status.get("requests_month", 0),
                secondary_status.get("requests_month_limit", 1)
            ),
        )

        # Circuit breaker status
        if secondary_status.get("circuit_open"):
            secondary_table.add_row("Circuit", "OPEN (failures: {})".format(
                secondary_status.get("circuit_failure_count", 0)))
        elif secondary_status.get("circuit_failure_count", 0) > 0:
            secondary_table.add_row("Circuit", "closed ({} failures)".format(
                secondary_status.get("circuit_failure_count", 0)))

        # Token summary
        secondary_table.add_row(
            "Tokens",
            f"{_fmt_number(secondary_status.get('total_tokens_consumed', 0))} consumed | "
            f"{_fmt_number(secondary_status.get('tpm_reserved', 0))} reserved | "
            f"{_fmt_number(secondary_status.get('tpm_limit', 0))} capacity",
        )

        # Request stats
        secondary_table.add_row(
            "Forwarded",
            f"{_fmt_number(secondary_status.get('total_forwarded', 0))} | "
            f"429s: {secondary_status.get('total_429s', 0)} | "
            f"Rejected: {secondary_status.get('total_rejected', 0)}",
        )

        # Quota warning
        warning = self._quota_warning(secondary_status)
        if warning:
            secondary_table.add_row("Warning", warning)

    @_safe_update
    def _update_tertiary_metrics(self, status: dict) -> None:
        """Update OpenLux (tertiary) provider metrics in the Overview tab."""
        try:
            tertiary_overview = self.query_one("#tertiary-overview", Vertical)
            tertiary_status_line = self.query_one("#tertiary-status-line", Static)
            tertiary_table = self.query_one("#tertiary-rl-metrics", DataTable)
        except NoMatches:
            return

        if "tertiary" not in status:
            tertiary_overview.set_class(False, "visible")
            return

        tertiary_status = status.get("tertiary")

        if not tertiary_status:
            tertiary_overview.set_class(False, "visible")
            return

        # Only show if provider has served at least one request
        if tertiary_status.get("total_forwarded", 0) == 0:
            tertiary_overview.set_class(False, "visible")
            return

        tertiary_overview.set_class(True, "visible")

        from dashscope_proxy_lib.config import TERTIARY_BASE_URL
        base_url_display = TERTIARY_BASE_URL or "N/A"
        tertiary_status_line.update(f"Status: Active | Target: {base_url_display}")
        tertiary_status_line.set_class(True, "status-running")
        tertiary_status_line.set_class(False, "status-stopped")

        tertiary_table.clear()

        tertiary_table.add_row("RPS Limit", str(tertiary_status.get("rps_limit", 0)))

        rpm_current = tertiary_status.get("rpm_current", 0)
        rpm_limit = tertiary_status.get("rpm_limit", 1)
        tertiary_table.add_row(
            "RPM",
            _progress_bar(rpm_current, rpm_limit),
        )

        tertiary_table.add_row(
            "TPM Available",
            _progress_bar(
                tertiary_status.get("tpm_available", 0),
                tertiary_status.get("tpm_limit", 1)
            ),
        )

        tertiary_table.add_row(
            "5-Hour Quota",
            _progress_bar(
                tertiary_status.get("requests_5h", 0),
                tertiary_status.get("requests_5h_limit", 1)
            ),
        )

        tertiary_table.add_row(
            "Weekly Quota",
            _progress_bar(
                tertiary_status.get("requests_week", 0),
                tertiary_status.get("requests_week_limit", 1)
            ),
        )

        tertiary_table.add_row(
            "Monthly Quota",
            _progress_bar(
                tertiary_status.get("requests_month", 0),
                tertiary_status.get("requests_month_limit", 1)
            ),
        )

        if tertiary_status.get("circuit_open"):
            tertiary_table.add_row("Circuit", "OPEN (failures: {})".format(
                tertiary_status.get("circuit_failure_count", 0)))
        elif tertiary_status.get("circuit_failure_count", 0) > 0:
            tertiary_table.add_row("Circuit", "closed ({} failures)".format(
                tertiary_status.get("circuit_failure_count", 0)))

        tertiary_table.add_row(
            "Tokens",
            f"{_fmt_number(tertiary_status.get('total_tokens_consumed', 0))} consumed | "
            f"{_fmt_number(tertiary_status.get('tpm_reserved', 0))} reserved | "
            f"{_fmt_number(tertiary_status.get('tpm_limit', 0))} capacity",
        )

        tertiary_table.add_row(
            "Forwarded",
            f"{_fmt_number(tertiary_status.get('total_forwarded', 0))} | "
            f"429s: {tertiary_status.get('total_429s', 0)} | "
            f"Rejected: {tertiary_status.get('total_rejected', 0)}",
        )

        warning = self._quota_warning(tertiary_status)
        if warning:
            tertiary_table.add_row("Warning", warning)

    @_safe_update
    def _update_quaternary_metrics(self, status: dict) -> None:
        """Update ARK (quaternary) provider metrics in the Overview tab."""
        try:
            quaternary_overview = self.query_one("#quaternary-overview", Vertical)
            quaternary_status_line = self.query_one("#quaternary-status-line", Static)
            quaternary_table = self.query_one("#quaternary-rl-metrics", DataTable)
        except NoMatches:
            return

        if "quaternary" not in status:
            quaternary_overview.set_class(False, "visible")
            return

        quaternary_status = status.get("quaternary")

        if not quaternary_status:
            quaternary_overview.set_class(False, "visible")
            return

        # Only show if provider has served at least one request
        if quaternary_status.get("total_forwarded", 0) == 0:
            quaternary_overview.set_class(False, "visible")
            return

        quaternary_overview.set_class(True, "visible")

        from dashscope_proxy_lib.config import QUATERNARY_BASE_URL
        base_url_display = QUATERNARY_BASE_URL or "N/A"
        quaternary_status_line.update(f"Status: Active | Target: {base_url_display}")
        quaternary_status_line.set_class(True, "status-running")
        quaternary_status_line.set_class(False, "status-stopped")

        quaternary_table.clear()

        quaternary_table.add_row("RPS Limit", str(quaternary_status.get("rps_limit", 0)))

        rpm_current = quaternary_status.get("rpm_current", 0)
        rpm_limit = quaternary_status.get("rpm_limit", 1)
        quaternary_table.add_row(
            "RPM",
            _progress_bar(rpm_current, rpm_limit),
        )

        quaternary_table.add_row(
            "TPM Available",
            _progress_bar(
                quaternary_status.get("tpm_available", 0),
                quaternary_status.get("tpm_limit", 1)
            ),
        )

        quaternary_table.add_row(
            "5-Hour Quota",
            _progress_bar(
                quaternary_status.get("requests_5h", 0),
                quaternary_status.get("requests_5h_limit", 1)
            ),
        )

        quaternary_table.add_row(
            "Weekly Quota",
            _progress_bar(
                quaternary_status.get("requests_week", 0),
                quaternary_status.get("requests_week_limit", 1)
            ),
        )

        quaternary_table.add_row(
            "Monthly Quota",
            _progress_bar(
                quaternary_status.get("requests_month", 0),
                quaternary_status.get("requests_month_limit", 1)
            ),
        )

        if quaternary_status.get("circuit_open"):
            quaternary_table.add_row("Circuit", "OPEN (failures: {})".format(
                quaternary_status.get("circuit_failure_count", 0)))
        elif quaternary_status.get("circuit_failure_count", 0) > 0:
            quaternary_table.add_row("Circuit", "closed ({} failures)".format(
                quaternary_status.get("circuit_failure_count", 0)))

        quaternary_table.add_row(
            "Tokens",
            f"{_fmt_number(quaternary_status.get('total_tokens_consumed', 0))} consumed | "
            f"{_fmt_number(quaternary_status.get('tpm_reserved', 0))} reserved | "
            f"{_fmt_number(quaternary_status.get('tpm_limit', 0))} capacity",
        )

        quaternary_table.add_row(
            "Forwarded",
            f"{_fmt_number(quaternary_status.get('total_forwarded', 0))} | "
            f"429s: {quaternary_status.get('total_429s', 0)} | "
            f"Rejected: {quaternary_status.get('total_rejected', 0)}",
        )

        warning = self._quota_warning(quaternary_status)
        if warning:
            quaternary_table.add_row("Warning", warning)

    @_safe_update
    def _update_quinary_metrics(self, status: dict) -> None:
        """Update Meta AI (quinary) provider metrics in the Overview tab."""
        try:
            quinary_overview = self.query_one("#quinary-overview", Vertical)
            quinary_status_line = self.query_one("#quinary-status-line", Static)
            quinary_table = self.query_one("#quinary-rl-metrics", DataTable)
        except NoMatches:
            return

        if "quinary" not in status:
            quinary_overview.set_class(False, "visible")
            return

        quinary_status = status.get("quinary")

        if not quinary_status:
            quinary_overview.set_class(False, "visible")
            return

        # Only show if provider has served at least one request
        if quinary_status.get("total_forwarded", 0) == 0:
            quinary_overview.set_class(False, "visible")
            return

        quinary_overview.set_class(True, "visible")

        from dashscope_proxy_lib.config import QUINARY_BASE_URL
        base_url_display = QUINARY_BASE_URL or "N/A"
        quinary_status_line.update(f"Status: Active | Target: {base_url_display}")
        quinary_status_line.set_class(True, "status-running")
        quinary_status_line.set_class(False, "status-stopped")

        quinary_table.clear()

        quinary_table.add_row("RPS Limit", str(quinary_status.get("rps_limit", 0)))

        rpm_current = quinary_status.get("rpm_current", 0)
        rpm_limit = quinary_status.get("rpm_limit", 1)
        quinary_table.add_row(
            "RPM",
            _progress_bar(rpm_current, rpm_limit),
        )

        quinary_table.add_row(
            "TPM Available",
            _progress_bar(
                quinary_status.get("tpm_available", 0),
                quinary_status.get("tpm_limit", 1)
            ),
        )

        quinary_table.add_row(
            "5-Hour Quota",
            _progress_bar(
                quinary_status.get("requests_5h", 0),
                quinary_status.get("requests_5h_limit", 1)
            ),
        )

        quinary_table.add_row(
            "Weekly Quota",
            _progress_bar(
                quinary_status.get("requests_week", 0),
                quinary_status.get("requests_week_limit", 1)
            ),
        )

        quinary_table.add_row(
            "Monthly Quota",
            _progress_bar(
                quinary_status.get("requests_month", 0),
                quinary_status.get("requests_month_limit", 1)
            ),
        )

        if quinary_status.get("circuit_open"):
            quinary_table.add_row("Circuit", "OPEN (failures: {})".format(
                quinary_status.get("circuit_failure_count", 0)))
        elif quinary_status.get("circuit_failure_count", 0) > 0:
            quinary_table.add_row("Circuit", "closed ({} failures)".format(
                quinary_status.get("circuit_failure_count", 0)))

        quinary_table.add_row(
            "Tokens",
            f"{_fmt_number(quinary_status.get('total_tokens_consumed', 0))} consumed | "
            f"{_fmt_number(quinary_status.get('tpm_reserved', 0))} reserved | "
            f"{_fmt_number(quinary_status.get('tpm_limit', 0))} capacity",
        )

        quinary_table.add_row(
            "Forwarded",
            f"{_fmt_number(quinary_status.get('total_forwarded', 0))} | "
            f"429s: {quinary_status.get('total_429s', 0)} | "
            f"Rejected: {quinary_status.get('total_rejected', 0)}",
        )

        warning = self._quota_warning(quinary_status)
        if warning:
            quinary_table.add_row("Warning", warning)

    @_safe_update
    def _update_model_table(self, status: dict) -> None:
        """Update per-model usage DataTable with filtering and sorting.
        
        Receives raw_status from MultiProviderRateLimiter.status() which has
        the shape {"primary": {...}, "secondary": {...}, "shared_limits": bool}.
        """
        table = self.query_one("#model-usage-table", DataTable)
        table.clear()

        # Handle multi-provider status structure
        if "primary" in status:
            primary_usage = status.get("primary", {}).get("model_usage", {})
            secondary_usage = status.get("secondary", {}).get("model_usage", {}) if status.get("secondary") else {}
            tertiary_usage = status.get("tertiary", {}).get("model_usage", {}) if status.get("tertiary") else {}
            quaternary_usage = status.get("quaternary", {}).get("model_usage", {}) if status.get("quaternary") else {}
            quinary_usage = status.get("quinary", {}).get("model_usage", {}) if status.get("quinary") else {}

            model_usage = {}
            for model_name, stats in primary_usage.items():
                model_usage[model_name] = {**stats, "provider": "primary"}
            for model_name, stats in secondary_usage.items():
                model_usage[model_name] = {**stats, "provider": "secondary"}
            for model_name, stats in tertiary_usage.items():
                model_usage[model_name] = {**stats, "provider": "openlux"}
            for model_name, stats in quaternary_usage.items():
                model_usage[model_name] = {**stats, "provider": "ark"}
            for model_name, stats in quinary_usage.items():
                model_usage[model_name] = {**stats, "provider": "meta-ai"}
        else:
            # Single provider mode
            model_usage = {k: {**v, "provider": "primary"} for k, v in status.get("model_usage", {}).items()}
        
        # Apply filter
        if self._model_filter:
            filter_lower = self._model_filter.lower()
            model_usage = {k: v for k, v in model_usage.items() if filter_lower in k.lower()}
        
        # Sort by selected key
        sort_key = self._model_sort_key
        if sort_key == "requests":
            sorted_models = sorted(model_usage.items(), key=lambda x: x[1]["requests"], reverse=True)
        elif sort_key == "tokens":
            sorted_models = sorted(model_usage.items(), key=lambda x: x[1]["tokens"], reverse=True)
        elif sort_key == "latency":
            sorted_models = sorted(model_usage.items(), key=lambda x: x[1]["avg_latency_ms"], reverse=True)
        elif sort_key == "429s":
            sorted_models = sorted(model_usage.items(), key=lambda x: x[1]["errors_429"], reverse=True)
        else:
            sorted_models = sorted(model_usage.items(), key=lambda x: x[1]["requests"], reverse=True)

        if not sorted_models:
            if model_usage:
                table.add_row("--", "No models match filter", "", "", "", "", "")
            else:
                table.add_row("--", "No model data yet", "", "", "", "", "")
        else:
            total_requests = sum(v["requests"] for _, v in sorted_models)
            for model_name, stats in sorted_models:
                pct = f"{(stats['requests'] / total_requests * 100):.0f}%" if total_requests > 0 else "0%"
                provider = stats.get("provider", "primary")
                table.add_row(
                    model_name,
                    provider,
                    f"{stats['requests']} ({pct})",
                    _fmt_number(stats["tokens"]),
                    str(stats["errors_429"]),
                    f"{stats['avg_latency_ms']:.0f}ms",
                    f"{stats.get('p50_latency_ms', 0):.0f}ms",
                    f"{stats.get('p95_latency_ms', 0):.0f}ms",
                )
            
            # Totals row
            total_tokens = sum(v["tokens"] for _, v in sorted_models)
            total_429s = sum(v["errors_429"] for _, v in sorted_models)
            avg_latency = sum(v["avg_latency_ms"] * v["requests"] for _, v in sorted_models) / total_requests if total_requests > 0 else 0
            table.add_row(
                "TOTAL",
                "",  # Provider column (aggregate)
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
        import os
        
        # Group config keys
        config_groups = {
            "Rate Limits": ["rpm_limit", "tpm_limit", "rps_limit", "requests_per_week", "requests_per_month", "requests_per_5h", "safety_factor"],
            "Timeouts": ["upstream_timeout_total", "upstream_timeout_connect"],
            "Connection": ["max_connections", "max_connections_per_host"],
            "Buffering": ["max_body_size", "max_stream_buffer", "max_queue_size", "max_retries", "base_backoff", "deque_max_size"],
            "Logging": ["log_level", "log_buffer_size"],
        }
        
        # Detect env var source for a config key
        def _env_source(key: str) -> str:
            if f"PROXY_{key.upper()}" in os.environ:
                return "env"
            if f"SECONDARY_{key.upper()}" in os.environ:
                return "env"
            if f"MIMO_CODING_PLAN_{key.upper()}" in os.environ:
                return "env"
            if f"OPENLUX_{key.upper()}" in os.environ:
                return "env"
            if f"MODEL_ARK_{key.upper()}" in os.environ:
                return "env"
            if f"QUINARY_{key.upper()}" in os.environ:
                return "env"
            return "default"
        
        ungrouped_keys = []
        for key, value in sorted(self._config_snapshot.items()):
            source = _env_source(key)
            if self._config_filter and self._config_filter.lower() not in key.lower():
                continue
            ungrouped_keys.append((key, str(value), source))
        
        if self._config_grouped:
            # Show grouped view
            shown_keys = set()
            for group_name, group_keys in config_groups.items():
                group_items = [(k, v, s) for k, v, s in ungrouped_keys if k in group_keys]
                if group_items:
                    table.add_row(f"--- {group_name} ---", "", "")
                    for key, value, source in group_items:
                        table.add_row(key, value, source)
                        shown_keys.add(key)
            
            # Show remaining ungrouped keys
            remaining = [(k, v, s) for k, v, s in ungrouped_keys if k not in shown_keys]
            if remaining:
                table.add_row("--- Other ---", "", "")
                for key, value, source in remaining:
                    table.add_row(key, value, source)
        else:
            for key, value, source in ungrouped_keys:
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
        os.makedirs(log_dir, exist_ok=True)
        
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(log_dir, f"tui_export_{timestamp}.log")
        
        # Get all logs from buffer
        all_logs = list(self.log_handler.buffer)
        
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
        
        # Write to file
        with open(filepath, "w", encoding="utf-8") as f:
            for entry in filtered:
                ts = entry.get("timestamp", "")
                level = entry.get("level", "INFO")
                msg = entry.get("message", "")
                f.write(f"[{level}] {ts} -- {msg}\n")
        
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
        elif event.button.id == "config-refresh-btn":
            self._config_snapshot = _load_config()
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
            try:
                status = self.rate_limiter.status()
                self._update_model_table(status)
            except NoMatches:
                pass
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
            try:
                status = self.rate_limiter.status()
                self._update_model_table(status)
            except NoMatches:
                pass


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
