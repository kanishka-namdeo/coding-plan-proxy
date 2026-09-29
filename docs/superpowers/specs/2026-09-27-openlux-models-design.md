# OpenLux Models Support Design

## Overview

Add support for 7 new OpenLux (tertiary provider) models that are currently not recognized by the proxy.

## Current State

The proxy already supports OpenLux as the "tertiary" provider with these models:
- `gpt-5.6-sol`
- `gemini-3.7-flash` (note: different from 3.8)
- `qwen3.8-max`
- `qwen3.8-max-0902`
- `gpt-5.6-luna`
- `gemini-3.8-flash` (already present!)
- `grok-4.6`
- `MiniMax-M3`
- `mimo-v2.5`
- `glm-5.3-flash`
- `gpt-6-astra`

## Missing Models (from user image)

After comparing the user's image with the current `TERTIARY_MODELS` list:

| Model | Status |
|-------|--------|
| `gpt-5.6-sol` | ✅ Already present |
| `gemini-3.8-flash` | ✅ Already present |
| `gpt-5.6-terra` | ❌ **Missing** |
| `gpt-5.6-luna` | ✅ Already present |
| `glm-5.3-flash` | ✅ Already present |
| `MiniMax-M3` | ✅ Already present |
| `mimo-v2.5` | ✅ Already present |
| `gpt-6-sol` | ❌ **Missing** |
| `gpt-6-astra` | ✅ Already present |
| `mimo-v2.6-flash` | ❌ **Missing** |
| `jev-1.13.0` | ❌ **Missing** |

## Models to Add

1. `gpt-5.6-terra`
2. `gpt-6-sol`
3. `mimo-v2.6-flash`
4. `jev-1.13.0`

## Implementation

### Changes Required

**File: `dashscope_proxy_lib/config.py`**

Add the 4 missing model entries to `TERTIARY_MODELS["data"]` list.

### No Other Changes Needed

- Provider slug `openlux` → `tertiary` already exists in `PROVIDER_SLUGS`
- Provider routing logic already handles tertiary models
- Rate limiting config already exists for tertiary provider
- TUI display config already includes OpenLux

## Testing

- Unit tests in `tests/test_units.py` verify provider routing
- Integration tests verify model resolution
- No new test files needed - existing test coverage sufficient

## Verification

After implementation:
1. `/v1/models` endpoint should list the new models
2. Requests with `openlux/<model>` pin should route to tertiary provider
3. Bare model names should resolve to tertiary provider when OpenLux is configured
