# TUI Provider Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor TUI to support all 6 providers dynamically with unified rendering, enhanced model table, and failover tracking.

**Architecture:** Provider registry pattern replaces hardcoded per-provider UI sections. Single `_update_provider_metrics()` method renders any provider. Model table enhanced with multi-provider breakdown. New failover panel shows cross-provider failover events.

**Tech Stack:** Python, Textual TUI framework, existing rate limiter infrastructure

## Global Constraints

- Must maintain backward compatibility with single-provider setups
- Only show provider sections when configured AND have served requests
- All provider sections must use identical rendering logic
- No breaking changes to CLI, config, or existing proxy functionality
- Follow existing TUI patterns (DataTable, Static, progress bars, sparklines)

---

## File Structure

| File | Purpose | Changes |
|------|---------|---------|
| `proxy_tui.py` | Main TUI implementation | Major refactor: registry pattern, unified rendering, enhanced model table, failover panel |
| `proxy_tui.tcss` | TUI styling | Minor additions for provider sections if needed |
| `dashscope_proxy_lib/config.py` | Configuration | No changes (already has senary) |
| `dashscope_proxy_lib/rate_limiter.py` | Rate limiting | No changes (already supports senary) |

---

### Task 1: Add Provider Registry Constant

**Files:**
- Modify: `proxy_tui.py` (top of file, after imports)

**Interfaces:**
- Produces: `PROVIDER_REGISTRY` list constant used by all later tasks

- [ ] **Step 1: Add PROVIDER_REGISTRY constant**

Add after the imports section (around line 25, after `from dashscope_proxy_lib.logging_config import TUILogHandler`):

```python
# Provider registry for dynamic UI rendering
PROVIDER_REGISTRY = [
    {"key": "primary", "label": "DashScope", "slug": "dashscope", "config_key": "TARGET_BASE"},
    {"key": "secondary", "label": "MIMO", "slug": "mimo", "config_key": "SECONDARY_BASE_URL"},
    {"key": "tertiary", "label": "OpenLux", "slug": "openlux", "config_key": "TERTIARY_BASE_URL"},
    {"key": "quaternary", "label": "ARK", "slug": "ark", "config_key": "QUATERNARY_BASE_URL"},
    {"key": "quinary", "label": "Meta AI", "slug": "metaspark", "config_key": "QUINARY_BASE_URL"},
    {"key": "senary", "label": "DeepSeek", "slug": "deepseek", "config_key": "SENARY_BASE_URL"},
]
```

- [ ] **Step 2: Verify no syntax errors**

Run: `python -m py_compile proxy_tui.py`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
git add proxy_tui.py
git commit -m "feat(tui): add provider registry constant for dynamic rendering"
```

---

### Task 2: Refactor compose() for Dynamic Provider Sections

**Files:**
- Modify: `proxy_tui.py:190-230` (compose method)

**Interfaces:**
- Consumes: `PROVIDER_REGISTRY` from Task 1
- Produces: Dynamic provider section widgets with IDs `{key}-overview`, `{key}-status-line`, `{key}-rl-metrics`

- [ ] **Step 1: Replace hardcoded secondary/tertiary/quaternary/quinary sections with loop**

Find the compose method around line 190. Replace the hardcoded provider sections (secondary, tertiary, quaternary, quinary) with a loop over `PROVIDER_REGISTRY[1:]` (skip primary, it's rendered separately).

Replace lines 205-228 (the secondary/tertiary/quaternary/quinary `with Vertical` blocks) with:

```python
                        # Dynamic provider sections (secondary through senary)
                        for provider_info in PROVIDER_REGISTRY[1:]:  # Skip primary
                            provider_key = provider_info["key"]
                            label = provider_info["label"]
                            with Vertical(id=f"{provider_key}-overview"):
                                yield Static(label, classes="panel-title")
                                yield Static(f"{label}: Not configured", id=f"{provider_key}-status-line")
                                yield DataTable(id=f"{provider_key}-rl-metrics")
```

- [ ] **Step 2: Verify syntax and imports**

Run: `python -m py_compile proxy_tui.py`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
git add proxy_tui.py
git commit -m "refactor(tui): dynamic provider sections via registry loop"
```

---

### Task 3: Update on_mount() for Dynamic Table Configuration

**Files:**
- Modify: `proxy_tui.py:310-380` (on_mount method)

**Interfaces:**
- Consumes: `PROVIDER_REGISTRY` from Task 1
- Produces: Configured DataTable widgets for all providers

- [ ] **Step 1: Replace hardcoded table configuration with loop**

Find the `on_mount` method. Replace the hardcoded secondary/tertiary/quaternary/quinary table configuration (lines 348-377) with a loop:

```python
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
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile proxy_tui.py`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
git add proxy_tui.py
git commit -m "refactor(tui): dynamic table configuration via registry loop"
```

---

### Task 4: Create Unified _update_provider_metrics() Method

**Files:**
- Modify: `proxy_tui.py` (add new method after `_update_metrics`)

**Interfaces:**
- Consumes: `PROVIDER_REGISTRY` from Task 1, status dict from `MultiProviderRateLimiter.status()`
- Produces: Rendered provider metrics in DataTable

- [ ] **Step 1: Add the unified provider metrics method**

Add after `_update_metrics` method (around line 560):

```python
    @_safe_update
    def _update_provider_metrics(self, provider_key: str, status: dict) -> None:
        """Update metrics for a single provider.
        
        Args:
            provider_key: Provider key from registry (e.g., "secondary", "senary")
            status: Raw status dict from MultiProviderRateLimiter.status()
        """
        try:
            overview = self.query_one(f"#{provider_key}-overview", Vertical)
            status_line = self.query_one(f"#{provider_key}-status-line", Static)
            table = self.query_one(f"#{provider_key}-rl-metrics", DataTable)
        except NoMatches:
            return

        # Check if this provider exists in the status dict
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

        # Get base URL from config
        provider_info = next((p for p in PROVIDER_REGISTRY if p["key"] == provider_key), None)
        if provider_info:
            from dashscope_proxy_lib import config as _cfg
            base_url = getattr(_cfg, provider_info["config_key"], "N/A")
            status_line.update(f"Status: Active | Target: {base_url or 'N/A'}")
        status_line.set_class(True, "status-running")
        status_line.set_class(False, "status-stopped")

        # Populate metrics table
        table.clear()

        table.add_row("RPS Limit", str(provider_status.get("rps_limit", 0)))

        rpm_current = provider_status.get("rpm_current", 0)
        rpm_limit = provider_status.get("rpm_limit", 1)
        table.add_row(
            "RPM",
            _progress_bar(rpm_current, rpm_limit),
        )

        table.add_row(
            "TPM Available",
            _progress_bar(
                provider_status.get("tpm_available", 0),
                provider_status.get("tpm_limit", 1)
            ),
        )

        table.add_row(
            "5-Hour Quota",
            _progress_bar(
                provider_status.get("requests_5h", 0),
                provider_status.get("requests_5h_limit", 1)
            ),
        )

        table.add_row(
            "Weekly Quota",
            _progress_bar(
                provider_status.get("requests_week", 0),
                provider_status.get("requests_week_limit", 1)
            ),
        )

        table.add_row(
            "Monthly Quota",
            _progress_bar(
                provider_status.get("requests_month", 0),
                provider_status.get("requests_month_limit", 1)
            ),
        )

        # Circuit breaker status
        if provider_status.get("circuit_open"):
            table.add_row("Circuit", "OPEN (failures: {})".format(
                provider_status.get("circuit_failure_count", 0)))
        elif provider_status.get("circuit_failure_count", 0) > 0:
            table.add_row("Circuit", "closed ({} failures)".format(
                provider_status.get("circuit_failure_count", 0)))

        # Token summary
        table.add_row(
            "Tokens",
            f"{_fmt_number(provider_status.get('total_tokens_consumed', 0))} consumed | "
            f"{_fmt_number(provider_status.get('tpm_reserved', 0))} reserved | "
            f"{_fmt_number(provider_status.get('tpm_limit', 0))} capacity",
        )

        # Request stats
        table.add_row(
            "Forwarded",
            f"{_fmt_number(provider_status.get('total_forwarded', 0))} | "
            f"429s: {provider_status.get('total_429s', 0)} | "
            f"Rejected: {provider_status.get('total_rejected', 0)}",
        )

        # Quota warning
        warning = self._quota_warning(provider_status)
        if warning:
            table.add_row("Warning", warning)
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile proxy_tui.py`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
git add proxy_tui.py
git commit -m "feat(tui): add unified _update_provider_metrics method"
```

---

### Task 5: Update _poll_loop() for Registry-Based Updates

**Files:**
- Modify: `proxy_tui.py:440-449` (poll_loop method, metrics update calls)

**Interfaces:**
- Consumes: `PROVIDER_REGISTRY` from Task 1, `_update_provider_metrics` from Task 4
- Produces: Provider metrics updates for all configured providers

- [ ] **Step 1: Replace hardcoded provider update calls with loop**

Find the `_poll_loop` method. Replace the hardcoded provider update calls (lines 444-449) with a loop:

Replace:
```python
                self.call_from_thread(self._update_secondary_metrics, raw_status)
                self.call_from_thread(self._update_tertiary_metrics, raw_status)
                self.call_from_thread(self._update_quaternary_metrics, raw_status)
                self.call_from_thread(self._update_quinary_metrics, raw_status)
```

With:
```python
                # Update all non-primary providers via registry
                for provider_info in PROVIDER_REGISTRY[1:]:
                    self.call_from_thread(self._update_provider_metrics, provider_info["key"], raw_status)
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile proxy_tui.py`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
git add proxy_tui.py
git commit -m "refactor(tui): registry-based provider updates in poll_loop"
```

---

### Task 6: Remove Old Provider-Specific Update Methods

**Files:**
- Modify: `proxy_tui.py` (remove methods)

**Interfaces:**
- Removes: `_update_secondary_metrics`, `_update_tertiary_metrics`, `_update_quaternary_metrics`, `_update_quinary_metrics`

- [ ] **Step 1: Delete old provider-specific update methods**

Remove the following methods entirely:
- `_update_secondary_metrics` (lines ~870-980)
- `_update_tertiary_metrics` (lines ~985-1080)
- `_update_quaternary_metrics` (lines ~1085-1180)
- `_update_quinary_metrics` (lines ~1185-1280)

These are now replaced by the unified `_update_provider_metrics` method.

- [ ] **Step 2: Verify TUI still runs**

Run: `python -m py_compile proxy_tui.py`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
git add proxy_tui.py
git commit -m "refactor(tui): remove duplicated provider-specific update methods"
```

---

### Task 7: Update _update_alert_badge() for All Providers

**Files:**
- Modify: `proxy_tui.py:785-815` (_update_alert_badge method)

**Interfaces:**
- Consumes: `PROVIDER_REGISTRY` from Task 1
- Produces: Alert badge showing warnings from all configured providers

- [ ] **Step 1: Refactor _update_alert_badge to use registry**

Replace the hardcoded provider loop in `_update_alert_badge` with a registry-based iteration:

```python
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

            if warnings:
                badge.update("ALERT: " + " | ".join(warnings) + " critical")
                badge.add_class("alert-active")
            else:
                badge.update("")
                badge.remove_class("alert-active")
        except NoMatches:
            pass
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile proxy_tui.py`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
git add proxy_tui.py
git commit -m "refactor(tui): registry-based alert badge for all providers"
```

---

### Task 8: Update _update_metrics() Request Statistics for All Providers

**Files:**
- Modify: `proxy_tui.py:575-620` (_update_metrics method, request stats section)

**Interfaces:**
- Consumes: `PROVIDER_REGISTRY` from Task 1
- Produces: Request statistics showing all provider breakdowns

- [ ] **Step 1: Refactor request statistics to use registry**

Replace the hardcoded provider breakdown in `_update_metrics` (lines 575-610) with a registry-based loop:

Replace:
```python
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
```

With:
```python
        # Show per-provider breakdown when multiple providers are active
        if "primary" in status:
            total_fwd_all = 0
            provider_count = 0
            
            for provider_info in PROVIDER_REGISTRY:
                provider_key = provider_info["key"]
                provider_label = provider_info["label"]
                provider_status = status.get(provider_key)
                
                if not provider_status:
                    continue
                    
                provider_fwd = provider_status.get("total_forwarded", 0)
                if provider_fwd == 0:
                    continue
                    
                provider_count += 1
                total_fwd_all += provider_fwd
                
                label_prefix = "Primary" if provider_key == "primary" else provider_label
                stats_table.add_row(f"Forwarded ({label_prefix})", _fmt_number(provider_fwd))
                stats_table.add_row(f"429s ({label_prefix})", str(provider_status.get("total_429s", 0)))
            
            # Show aggregate totals when multiple providers are active
            if provider_count > 1:
                stats_table.add_row("Total 429s", str(total_429s))
                stats_table.add_row("Total Forwarded", _fmt_number(total_fwd))
            elif provider_count == 1:
                stats_table.add_row("Total Forwarded", _fmt_number(total_fwd))
        else:
            stats_table.add_row("Total Forwarded", _fmt_number(total_fwd))
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile proxy_tui.py`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
git add proxy_tui.py
git commit -m "refactor(tui): registry-based request statistics for all providers"
```

---

### Task 9: Enhance Model Table for Multi-Provider Breakdown

**Files:**
- Modify: `proxy_tui.py:1290-1370` (_update_model_table method)

**Interfaces:**
- Consumes: `PROVIDER_REGISTRY` from Task 1, model_usage from all providers
- Produces: Model table with providers column and per-provider breakdown rows

- [ ] **Step 1: Add providers column to model table**

Find the model table configuration in `on_mount`. Update the column definitions to include a "Providers" column:

Replace:
```python
            model_table.add_columns("Model", "Provider", "Requests", "Tokens", "429s", "Avg Latency", "p50", "p95")
```

With:
```python
            model_table.add_columns("Model", "Providers", "Requests", "Tokens", "429s", "Avg Latency", "p50", "p95")
```

- [ ] **Step 2: Refactor _update_model_table to show multi-provider breakdown**

Replace the entire `_update_model_table` method with the enhanced version:

```python
    @_safe_update
    def _update_model_table(self, status: dict) -> None:
        """Update per-model usage DataTable with filtering and sorting.
        
        Receives raw_status from MultiProviderRateLimiter.status() which has
        the shape {"primary": {...}, "secondary": {...}, "shared_limits": bool}.
        """
        table = self.query_one("#model-usage-table", DataTable)
        table.clear()

        # Collect model usage from all providers
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
            # Single provider mode
            for model_name, stats in status.get("model_usage", {}).items():
                model_data[model_name] = {"primary": stats}
        
        # Aggregate stats across providers and track provider list
        aggregated = {}
        for model_name, provider_stats in model_data.items():
            total_requests = sum(s.get("requests", 0) for s in provider_stats.values())
            total_tokens = sum(s.get("tokens", 0) for s in provider_stats.values())
            total_429s = sum(s.get("errors_429", 0) for s in provider_stats.values())
            
            # Weighted average latency
            total_latency = sum(s.get("avg_latency_ms", 0) * s.get("requests", 0) for s in provider_stats.values())
            avg_latency = total_latency / total_requests if total_requests > 0 else 0
            
            # For percentiles, use the provider with the most requests
            primary_provider_stats = max(provider_stats.values(), key=lambda s: s.get("requests", 0))
            
            aggregated[model_name] = {
                "providers": list(provider_stats.keys()),
                "provider_stats": provider_stats,
                "total_requests": total_requests,
                "total_tokens": total_tokens,
                "total_429s": total_429s,
                "avg_latency_ms": avg_latency,
                "p50_latency_ms": primary_provider_stats.get("p50_latency_ms", 0),
                "p95_latency_ms": primary_provider_stats.get("p95_latency_ms", 0),
            }
        
        # Apply filter
        if self._model_filter:
            filter_lower = self._model_filter.lower()
            aggregated = {k: v for k, v in aggregated.items() if filter_lower in k.lower()}
        
        # Sort by selected key
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
            if aggregated:
                table.add_row("--", "No models match filter", "", "", "", "", "", "")
            else:
                table.add_row("--", "No model data yet", "", "", "", "", "", "")
        else:
            total_all_requests = sum(v["total_requests"] for _, v in sorted_models)
            
            for model_name, data in sorted_models:
                providers_list = ", ".join(data["providers"])
                pct = f"{(data['total_requests'] / total_all_requests * 100):.0f}%" if total_all_requests > 0 else "0%"
                
                # Main row with aggregated stats
                table.add_row(
                    model_name,
                    providers_list,
                    f"{data['total_requests']} ({pct})",
                    _fmt_number(data["total_tokens"]),
                    str(data["total_429s"]),
                    f"{data['avg_latency_ms']:.0f}ms",
                    f"{data.get('p50_latency_ms', 0):.0f}ms",
                    f"{data.get('p95_latency_ms', 0):.0f}ms",
                )
                
                # Add breakdown rows for multi-provider models
                if len(data["providers"]) > 1:
                    for provider_key, provider_stats in data["provider_stats"].items():
                        provider_label = next(
                            (p["label"] for p in PROVIDER_REGISTRY if p["key"] == provider_key),
                            provider_key
                        )
                        provider_pct = f"{(provider_stats['requests'] / data['total_requests'] * 100):.0f}%" if data['total_requests'] > 0 else "0%"
                        
                        table.add_row(
                            f"  via {provider_label}",
                            "",  # Providers column (empty for breakdown rows)
                            f"{provider_stats['requests']} ({provider_pct})",
                            _fmt_number(provider_stats.get("tokens", 0)),
                            str(provider_stats.get("errors_429", 0)),
                            f"{provider_stats.get('avg_latency_ms', 0):.0f}ms",
                            f"{provider_stats.get('p50_latency_ms', 0):.0f}ms",
                            f"{provider_stats.get('p95_latency_ms', 0):.0f}ms",
                        )
            
            # Totals row
            total_tokens = sum(v["total_tokens"] for _, v in sorted_models)
            total_429s = sum(v["total_429s"] for _, v in sorted_models)
            avg_latency = sum(v["avg_latency_ms"] * v["total_requests"] for _, v in sorted_models) / total_all_requests if total_all_requests > 0 else 0
            
            table.add_row(
                "TOTAL",
                "",  # Providers column
                str(total_all_requests),
                _fmt_number(total_tokens),
                str(total_429s),
                f"{avg_latency:.0f}ms",
                "",
                "",
            )
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile proxy_tui.py`
Expected: No output (success)

- [ ] **Step 4: Commit**

```bash
git add proxy_tui.py
git commit -m "feat(tui): enhanced model table with multi-provider breakdown"
```

---

### Task 10: Add Failover Tracking Panel to Metrics Tab

**Files:**
- Modify: `proxy_tui.py:280` (compose method, metrics tab)
- Modify: `proxy_tui.py` (add new method)

**Interfaces:**
- Consumes: Session logs from `session_logs/YYYY-MM-DD.jsonl`
- Produces: Failover events panel showing recent failover chains

- [ ] **Step 1: Add failover panel to Metrics tab composition**

In the `compose` method, in the Metrics TabPane, add the failover panel after the sparkline row (around line 280):

```python
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
```

- [ ] **Step 2: Add _update_failover_panel method**

Add after `_update_latency_histogram` method:

```python
    def _update_failover_panel(self) -> None:
        """Update failover events panel from recent session logs."""
        try:
            panel = self.query_one("#failover-events-panel", Static)
        except NoMatches:
            return

        # Read recent session logs
        import json
        from datetime import datetime, timezone, timedelta
        
        failovers = []
        log_dir = "session_logs"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = f"{log_dir}/{today}.jsonl"
        
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                # Read last 200 lines
                lines = f.readlines()[-200:]
                for line in lines:
                    try:
                        entry = json.loads(line.strip())
                        attempted = entry.get("attempted_providers", [])
                        if len(attempted) > 1:
                            model = entry.get("model", "unknown")
                            request_id = entry.get("request_id", "")[:8]
                            timestamp = entry.get("timestamp", "")
                            if timestamp:
                                try:
                                    dt = datetime.fromisoformat(timestamp)
                                    time_str = dt.strftime("%H:%M:%S")
                                except:
                                    time_str = timestamp
                            else:
                                time_str = "unknown"
                            
                            failovers.append({
                                "model": model,
                                "chain": attempted,
                                "request_id": request_id,
                                "time": time_str,
                            })
                    except (json.JSONDecodeError, KeyError):
                        continue
        except FileNotFoundError:
            pass
        except Exception:
            pass

        # Show last 10 failover events
        if failovers:
            failovers = failovers[-10:]
            lines = []
            for event in failovers:
                chain_str = " → ".join(event["chain"])
                lines.append(f"  {event['model']} → [{chain_str}] (req: {event['request_id']}, {event['time']})")
            panel.update("\n".join(lines))
        else:
            panel.update("  No recent failover events")
```

- [ ] **Step 3: Add failover panel update to poll loop**

In `_poll_loop`, add the failover panel update call after the sparkline update:

```python
                self.call_from_thread(self._update_failover_panel)
```

- [ ] **Step 4: Verify syntax**

Run: `python -m py_compile proxy_tui.py`
Expected: No output (success)

- [ ] **Step 5: Commit**

```bash
git add proxy_tui.py
git commit -m "feat(tui): add failover tracking panel to Metrics tab"
```

---

### Task 11: Run Full Test Suite

**Files:**
- Test: `tests/`

**Interfaces:**
- Verifies: All existing tests pass after refactoring

- [ ] **Step 1: Run unit tests**

Run: `python -m pytest tests/test_units.py -v`
Expected: All tests pass

- [ ] **Step 2: Run integration tests**

Run: `python -m pytest tests/test_integration.py -v`
Expected: All tests pass

- [ ] **Step 3: Fix any failures**

If any tests fail, fix the issues in the relevant files. The most likely areas:
- Mock structure changes in test fixtures
- Provider key names in assertions

- [ ] **Step 4: Commit any fixes**

```bash
git add tests/
git commit -m "fix: update tests for provider registry refactoring"
```

---

### Task 12: Manual Testing and Documentation

**Files:**
- None (manual testing)

- [ ] **Step 1: Start proxy with single provider**

Run: `python dashscope_proxy.py` (with only DASHSCOPE_API_KEY set)
Verify: Only Primary section shows, no other provider sections

- [ ] **Step 2: Start proxy with multiple providers**

Set environment variables for secondary and senary providers:
- `MIMO_CODING_PLAN_API_KEY` and `MIMO_CODING_PLAN_TARGET_BASE`
- `DEEPSEEK_API_KEY` and `DEEPSEEK_TARGET_BASE`

Run: `python dashscope_proxy.py`
Make requests to models from each provider
Verify: Secondary and Senary sections appear after serving requests

- [ ] **Step 3: Test failover**

Cause a 429 on one provider
Verify: Failover events panel shows the failover chain

- [ ] **Step 4: Test multi-provider model**

Make requests to a model that exists in multiple provider lists
Verify: Model table shows breakdown rows with "via Provider" prefix

- [ ] **Step 5: Verify commit history is clean**

```bash
git log --oneline -10
```

Ensure all tasks have clear commit messages

---

## Self-Review Checklist

After completing all tasks:

- [ ] **Spec coverage**: Each requirement from the spec has a corresponding task
  - ✅ Provider registry pattern (Tasks 1-2)
  - ✅ Dynamic UI sections (Tasks 2-3)
  - ✅ Unified _update_provider_metrics (Task 4)
  - ✅ Senary provider support (Tasks 1-8, automatic via registry)
  - ✅ Enhanced model table (Task 9)
  - ✅ Failover tracking (Task 10)
  - ✅ Testing (Task 11-12)

- [ ] **No placeholders**: All steps contain actual code

- [ ] **Type consistency**: All method signatures match across tasks