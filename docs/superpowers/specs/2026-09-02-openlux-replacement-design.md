# OpenLux Provider Replacement Design

**Date:** 2026-09-02  
**Status:** Approved for implementation

## Overview

Replace the StreamLake tertiary provider with OpenLux, an OpenAI-compatible API gateway that provides access to multiple model families including OpenAI GPT, Anthropic Claude, Google Gemini, and Alibaba Qwen models.

## Motivation

StreamLake currently serves as the tertiary provider with a single model (`kat-coder-pro-v2.5`). OpenLux offers a broader model selection with better pricing and flexibility, supporting multiple model families through a unified OpenAI-compatible interface at `https://api.openlux.ai/v1`.

## Design Approach

**Approach 1: Direct Replacement (Minimal Changes)**

Keep the "tertiary" provider slot and swap the configuration. This minimizes code changes and preserves the existing architecture.

## Configuration Changes

### 1. Environment Variables

Rename environment variables to reflect the new provider:

- `STREAMLAKE_API_KEY` → `OPENLUX_API_KEY`
- `STREAMLAKE_TARGET_BASE` → `OPENLUX_TARGET_BASE`

Default base URL: `https://api.openlux.ai/v1`

### 2. Model Configuration

Update `TERTIARY_MODELS` in `dashscope_proxy_lib/config.py`:

```python
TERTIARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "gemini-3.7-flash", "object": "model"},
        {"id": "gpt-5.6-terra", "object": "model"},
        {"id": "gpt-5.6-sol", "object": "model"},
        {"id": "qwen3.8-max", "object": "model"},
    ]
}
```

### 3. Rate Limits

Keep existing tertiary rate limit defaults:
- RPM: 40
- TPM: 6,000,000
- Safety factor: 0.8
- Requests per 5h: 3,000
- Requests per week: 20,000
- Requests per month: 50,000
- Max queue size: 200
- Max retries: 20

Environment variable overrides remain unchanged (`TERTIARY_RPM_LIMIT`, `TERTIARY_TPM_LIMIT`, etc.).

## Code Changes

### Files to Update

1. **`dashscope_proxy_lib/config.py`**
   - Rename env var reads: `STREAMLAKE_API_KEY` → `OPENLUX_API_KEY`, `STREAMLAKE_TARGET_BASE` → `OPENLUX_TARGET_BASE`
   - Update default base URL to `https://api.openlux.ai/v1`
   - Update `TERTIARY_MODELS` with new model list
   - Update comments referencing "StreamLake" to "OpenLux"

2. **`dashscope_proxy_lib/provider_router.py`**
   - Update comment in `_build_tertiary_model_ids()` from "StreamLake" to "OpenLux"

3. **`dashscope_proxy_lib/server.py`**
   - Update comments referencing "StreamLake" to "OpenLux"

4. **`proxy_tui.py`**
   - Change TUI display label from "StreamLake" to "OpenLux"
   - Update status line text: "StreamLake: Not configured" → "OpenLux: Not configured"

5. **`.env.example`**
   - Rename `STREAMLAKE_API_KEY` → `OPENLUX_API_KEY`
   - Rename `STREAMLAKE_TARGET_BASE` → `OPENLUX_TARGET_BASE`
   - Update default URL value and comments

6. **`tests/test_units.py` and `tests/test_integration.py`**
   - Update any comments or test data referencing "StreamLake" to "OpenLux"

7. **`AGENTS.md` and `README_SCRIPT.md`**
   - Update documentation references from "StreamLake" to "OpenLux"

### What Stays the Same

- All internal constant names (`TERTIARY_*`)
- Provider routing logic (still uses "tertiary" as the provider name internally)
- Rate limiter structure
- Provider router architecture

## Migration Path

1. Update `.env` file with new environment variable names
2. Obtain OpenLux API key from `https://api.openlux.ai`
3. Set `OPENLUX_API_KEY` and `OPENLUX_TARGET_BASE=https://api.openlux.ai/v1`
4. Restart proxy service
5. Verify new models appear in `/v1/models` endpoint
6. Test routing with new model names

## Testing

- Verify all 4 new models appear in `/v1/models` response
- Test request routing for each model
- Confirm rate limiting works correctly
- Verify TUI displays "OpenLux" label correctly
- Ensure backward compatibility (no breaking changes to API)

## Future Considerations

- If OpenAI and Claude models need separate rate limits, consider splitting into separate provider slots
- Monitor OpenLux pricing page for model availability changes
- Consider adding model-specific routing rules if needed
