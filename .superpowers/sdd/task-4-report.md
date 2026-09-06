# Task 4 Report: Create Unified _update_provider_metrics() Method

## Status: ✅ COMPLETED

## Implementation

Added a new unified method `_update_provider_metrics(provider_key: str, status: dict)` that can render metrics for ANY provider (secondary, tertiary, quaternary, quinary, senary, etc.).

### Method Location
- **File**: `proxy_tui.py`
- **Line**: 600 (after `_update_metrics`)
- **Lines added**: 126

### Method Signature
```python
@_safe_update
def _update_provider_metrics(self, provider_key: str, status: dict) -> None:
    """Update metrics for a single provider.
    
    Args:
        provider_key: Provider key from registry (e.g., "secondary", "senary")
        status: Raw status dict from MultiProviderRateLimiter.status()
    """
```

### Implementation Details

1. **Dynamic UI element queries**: Uses f-strings with `provider_key` to query:
   - `#{provider_key}-overview` (Vertical container)
   - `#{provider_key}-status-line` (Static widget)
   - `#{provider_key}-rl-metrics` (DataTable)

2. **Visibility conditions**: Checks three conditions before showing:
   - Provider key exists in status dict
   - Provider status is not None
   - Provider has forwarded at least one request (`total_forwarded > 0`)
   - Sets `visible` class if all conditions pass

3. **Status line update**: 
   - Looks up provider info in `PROVIDER_REGISTRY`
   - Gets base URL from config using `provider_info["config_key"]`
   - Formats: `"Status: Active | Target: {base_url}"`

4. **Metrics table population**: Identical to existing per-provider methods:
   - RPS Limit
   - RPM (with progress bar)
   - TPM Available (with progress bar)
   - 5-Hour Quota (with progress bar)
   - Weekly Quota (with progress bar)
   - Monthly Quota (with progress bar)
   - Circuit breaker status (if open or has failures)
   - Token summary (consumed/reserved/capacity)
   - Forwarded/429s/Rejected stats
   - Quota warning (if any)

### Key Features

- **Reusability**: One method replaces all duplicated per-provider methods
- **Future-proof**: Works with any provider defined in `PROVIDER_REGISTRY`
- **Consistent output**: Produces same metrics as existing methods
- **Dynamic config lookup**: Uses registry to get the correct config key for each provider

## Testing

### Syntax Verification
```bash
$ python -m py_compile proxy_tui.py
# Exit code: 0 (success)
```

### Commit
```bash
$ git commit -m "feat(tui): add unified _update_provider_metrics method"
[tui-provider-support 9a03019] feat(tui): add unified _update_provider_metrics method
 1 file changed, 126 insertions(+)
```

## Notes

- Old methods (`_update_secondary_metrics`, `_update_tertiary_metrics`, etc.) are **NOT removed** yet - that's Task 6
- The method uses existing helper functions: `_progress_bar()`, `_fmt_number()`, `_quota_warning()`
- Senary provider is now supported (missing in original implementation)
- The method can be called for any provider in the registry, making it easy to add new providers in the future

## Next Steps

Task 5 will integrate this unified method into the poll loop, and Task 6 will remove the old duplicated methods.