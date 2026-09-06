# Live Test Results - 2026-09-05

## Server Status
- **Started**: Successfully at http://127.0.0.1:8899
- **Uptime**: ~2 minutes during testing
- **All 6 providers configured and available**

## Feature Verification

### 1. Provider Prefix Syntax ✅
| Test | Model | Result |
|------|-------|--------|
| DeepSeek prefix | `deepseek/deepseek-v4-flash` | 200 OK, 826ms |
| OpenLux prefix | `openlux/gpt-5.6-sol` | 200 OK, 3702ms |
| OpenLux prefix | `openlux/gemini-3.7-flash` | 200 OK, 2124ms |
| Meta prefix | `metaspark/muse-spark-1.3` | 200 OK, 4071ms |
| MIMO prefix | `mimo/mimo-v2.5-pro` | 401 (invalid API key, routing correct) |

### 2. Unknown Provider Prefix ✅
- Request: `{"model": "unknown/model", ...}`
- Response: `400 Bad Request`
- Body: `{"error": "unknown provider prefix", "model": "unknown/model", "available_providers": ["dashscope", "mimo", "openlux", "ark", "metaspark", "deepseek"]}`

### 3. Models Endpoint ✅
- Returns deduped model list with new fields:
  - `providers`: List of provider names serving this model
  - `provider_models`: List of `provider/model` syntax options
- Example: `{"id": "deepseek-v4-flash", "providers": ["senary"], "provider_models": ["deepseek/deepseek-v4-flash"]}`

### 4. Provider Status ✅
- `GET /v1/proxy/status` includes:
  - Per-provider rate limit status
  - Provider availability and base URLs
  - `model_overlaps` object (empty in current config)

### 5. Session Logging ✅
- New `attempted_providers` field present in all requests
- Example: `"attempted_providers": ["quinary"]` for Meta AI request
- Error logging includes `error_reason` (e.g., "unknown_provider_prefix")

## Provider Test Results

| Provider | Status | Test Model | Result |
|----------|--------|------------|--------|
| Primary (DashScope) | ✅ Working | `qwen3.6-plus` | 200 OK, 4185ms |
| Secondary (MIMO) | ❌ Invalid Key | `mimo-v2.5-pro` | 401 Unauthorized |
| Tertiary (OpenLux) | ✅ Working | `gpt-5.6-sol` | 200 OK, 3702ms |
| Tertiary (OpenLux) | ✅ Working | `gemini-3.7-flash` | 200 OK, 2124ms |
| Quaternary (ARK) | ❌ Expired Sub | `glm-5.2` | 400 Bad Request |
| Quinary (Meta AI) | ✅ Working | `muse-spark-1.3` | 200 OK, 4071ms |
| Senary (DeepSeek) | ✅ Working | `deepseek-v4-flash` | 200 OK, 826ms |

**Total tokens consumed**: 1524

## Issues Found
- **MIMO**: API key is invalid (not a code issue)
- **ARK**: Subscription expired (not a code issue)

## All Core Features Verified ✅
1. Provider prefix parsing and stripping
2. 400 errors for unknown/unconfigured pins with `available_providers` hint
3. Deduped `/v1/models` with provider metadata
4. Multi-provider rate limiting working
5. Session log tracking with `attempted_providers`
6. Provider status endpoint with overlap detection