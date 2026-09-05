# TUI Full Provider Support Design

**Date**: 2026-09-06
**Status**: Approved
**Scope**: Ensure TUI fully supports all 6 providers (primary, secondary, tertiary, quaternary, quinary, senary) with dynamic visibility and enhanced metrics.

---

## Problem Statement

The proxy supports 6 providers:
- **Primary**: DashScope
- **Secondary**: MIMO
- **Tertiary**: OpenLux
- **Quaternary**: ARK/BytePlus
- **Quinary**: Meta AI/Muse Spark
- **Senary**: DeepSeek

The TUI currently has hardcoded UI sections for only 5 providers. The **senary (DeepSeek) provider has no UI section**, meaning operators cannot monitor DeepSeek metrics through the TUI.

Additionally, the current implementation has significant code duplication across 5 nearly-identical `_update_X_metrics()` methods (secondary, tertiary, quaternary, quinary - senary is completely missing), making maintenance difficult and adding new providers error-prone.

---

## Goals

1. **Add senary provider support**: DeepSeek metrics must be visible in the TUI when configured
2. **Eliminate code duplication**: Replace 6 duplicated methods with a single provider-agnostic rendering function
3. **Dynamic provider visibility**: Show only configured and active providers, hide unconfigured ones
4. **Enhanced model table**: Show per-provider breakdown for models served by multiple providers
5. **Failover tracking**: Visualize which providers were tried for requests that failed over

---

## Non-Goals

- Moving provider metrics to a separate tab (approach 2 from brainstorming)
- Changing the visual paradigm to a compact grid (approach 3 from brainstorming)
- Adding new provider-specific features beyond metrics display

---

## Design

### 1. Provider Registry Pattern

Replace hardcoded per-provider logic with a registry-based iteration pattern.

**Registry Definition** (in `proxy_tui.py`):

```python
PROVIDER_REGISTRY = [
    {"key": "primary", "label": "DashScope", "slug": "dashscope"},
    {"key": "secondary", "label": "MIMO", "slug": "mimo"},
    {"key": "tertiary", "label": "OpenLux", "slug": "openlux"},
    {"key": "quaternary", "label": "ARK", "slug": "ark"},
    {"key": "quinary", "label": "Meta AI", "slug": "metaspark"},
    {"key": "senary", "label": "DeepSeek", "slug": "deepseek"},
]
```

Each registry entry maps:
- `key`: The provider key used in `MultiProviderRateLimiter.status()["<key>"]`
- `label`: Human-readable display name for the UI
- `slug`: Provider slug used in `<provider>/<model>` pin syntax

---

### 2. UI Component Refactoring

**Before**: 5 duplicated methods + missing senary
```python
def _update_secondary_metrics(self, status: dict) -> None: ...
def _update_tertiary_metrics(self, status: dict) -> None: ...
def _update_quaternary_metrics(self, status: dict) -> None: ...
def _update_quinary_metrics(self, status: dict) -> None: ...
# Note: _update_senary_metrics() is MISSING entirely
```

**After**: Single unified method
```python
def _update_provider_metrics(self, provider_key: str, status: dict) -> None:
    """Update metrics for a single provider.
    
    Args:
        provider_key: Provider key from registry (e.g., "secondary", "senary")
        status: Raw status dict from MultiProviderRateLimiter.status()
    """
    # Get provider info from registry
    provider_info = next(p for p in PROVIDER_REGISTRY if p["key"] == provider_key)
    
    # Get or create UI elements
    container_id = f"{provider_key}-overview"
    # ... render metrics ...
```

**Composition**: Replace hardcoded UI container markup with dynamic creation:

```python
# In compose():
for provider_info in PROVIDER_REGISTRY[1:]:  # Skip primary (rendered separately)
    provider_key = provider_info["key"]
    label = provider_info["label"]
    with Vertical(id=f"{provider_key}-overview", classes="provider-section"):
        yield Static(label, classes="panel-title")
        yield Static(f"{label}: Not configured", id=f"{provider_key}-status-line")
        yield DataTable(id=f"{provider_key}-rl-metrics")
```

---

### 3. Dynamic Visibility Logic

**Show provider section if**:
1. Provider is configured (API key and base URL are set)
2. Provider has served at least one request (`total_forwarded > 0`)

**Hide provider section if**:
1. Provider is not configured, OR
2. Provider is configured but has not yet served any requests

This prevents UI clutter from showing sections for providers that aren't actively being used.

**Implementation**:
```python
def _should_show_provider(self, provider_key: str, provider_status: dict | None) -> bool:
    """Determine if a provider section should be visible."""
    if not provider_status:
        return False
    if provider_status.get("total_forwarded", 0) == 0:
        return False
    return True
```

---

### 4. Enhanced Model Table

**Current behavior**: Model table shows one provider per model.

**New behavior**: Models served by multiple providers show:
- Aggregated totals (requests, tokens, 429s across all providers)
- Provider breakdown with per-provider metrics

**Columns**:
| Column | Description |
|--------|-------------|
| Model | Model name (bare, without provider prefix) |
| Providers | Comma-separated list of providers serving this model |
| Requests | Total requests (with percentage of total) |
| Tokens | Total tokens consumed |
| 429s | Total 429 errors |
| Avg Latency | Weighted average latency across providers |
| p50 | 50th percentile latency |
| p95 | 95th percentile latency |

**For multi-provider models**, add a grouped display:

```
qwen3.8-max              openlux, dashscope    1250 (42%)    1.2M    12    450ms    380ms    890ms
  via openlux                                  800 (64%)     780K    5     420ms    350ms    780ms
  via dashscope                                450 (36%)     420K    7     510ms    450ms    920ms
```

**Note**: Textual DataTable doesn't support true hierarchical indentation. The breakdown will be shown as separate rows with the model name prefixed with "  via " for visual grouping.

---

### 5. Failover Tracking Panel

**Location**: Metrics tab (new section below sparklines)

**Purpose**: Visualize cross-provider failover events.

**Display**:
- Recent failover chains (last 20 events)
- Format: `model → [provider1 → provider2 → provider3]`
- Timestamp and request ID for each event
- Click to view full session log entry

**Data source**: Session log entries include `attempted_providers` list.

**Example**:
```
Failover Events (last 5 min):
  qwen3.8-max → [openlux → dashscope] (req: abc123, 14:32:05)
  gpt-5.6-sol → [openlux → primary] (req: def456, 14:31:58)
```

**Implementation**:
```python
def _update_failover_panel(self, raw_status: dict) -> None:
    """Update failover events panel from recent session logs."""
    # The proxy writes session logs to session_logs/YYYY-MM-DD.jsonl
    # Each entry includes: request_id, model, attempted_providers (list), timestamp
    # Read last 100 entries from today's log file
    # Filter to entries where len(attempted_providers) > 1
    # Render most recent 20 to Static widget
```

---

### 6. Alert Badge Enhancement

**Current**: Shows quota warnings and circuit breaker status.

**New**: Also show active failover events.

```python
if has_recent_failovers:
    warnings.append("Failover active")
```

---

### 7. Base URL Display

Each provider section's status line shows the configured base URL:

```
Status: Active | Target: https://api.deepseek.com
```

This helps operators verify they're pointing to the correct upstream.

---

## Implementation Checklist

- [ ] Add `PROVIDER_REGISTRY` constant to `proxy_tui.py`
- [ ] Refactor `compose()` to create provider sections dynamically
- [ ] Replace 6 `_update_X_metrics()` methods with single `_update_provider_metrics()`
- [ ] Add senary provider UI section
- [ ] Update `_poll_loop()` to iterate over registry instead of hardcoded calls
- [ ] Enhance `_update_model_table()` to show multi-provider breakdown
- [ ] Add `_update_failover_panel()` method
- [ ] Add failover events panel to Metrics tab UI
- [ ] Update alert badge to show failover status
- [ ] Update `_update_alert_badge()` to check all providers
- [ ] Add tests for new functionality (if applicable)

---

## File Changes

| File | Changes |
|------|---------|
| `proxy_tui.py` | Major refactor: provider registry, unified rendering, senary support, enhanced model table, failover panel |
| `proxy_tui.tcss` | Minor: CSS for provider sections (if needed) |

No changes required to:
- `dashscope_proxy_lib/rate_limiter.py` (already supports senary)
- `dashscope_proxy_lib/config.py` (already has senary config)
- `dashscope_proxy_lib/server.py` (already passes senary config to MultiProviderRateLimiter)
- `dashscope_proxy_lib/provider_router.py` (already has senary routing)

---

## Backward Compatibility

- Single-provider users see the same UI (Primary section + aggregated stats)
- All existing functionality preserved
- No breaking changes to CLI or config

---

## Testing Strategy

1. **Unit tests**: Verify provider registry iteration logic
2. **Integration tests**: Test with 0, 1, 2, 3, and 6 providers configured
3. **Manual testing**:
   - Start proxy with only primary configured → verify single section
   - Add secondary provider → verify section appears after first request
   - Add all 6 providers → verify all sections appear correctly
   - Test failover → verify failover panel shows events
   - Test multi-provider model → verify model table shows breakdown

---

## Success Criteria

- [ ] Senary (DeepSeek) provider metrics visible when configured
- [ ] No duplicate code for provider rendering
- [ ] Only configured providers show in UI
- [ ] Model table shows multi-provider breakdown
- [ ] Failover events tracked and displayed
- [ ] All existing tests pass
- [ ] Manual testing confirms correct behavior for all provider combinations