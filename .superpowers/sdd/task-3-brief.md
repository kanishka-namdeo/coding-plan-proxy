### Task 3: Prefix-pin routing + 400 on bad pin

**Files:**
- Modify: `dashscope_proxy_lib/provider_router.py` (`get_provider_for_model()`)
- Modify: `dashscope_proxy_lib/handlers.py` (parse prefix at model extraction, strip before upstream, 400 on unknown/unconfigured pin)
- Test: `tests/test_units.py` + `tests/test_integration.py` (append tests)

**Interfaces:**
- Consumes: `split_provider_prefix()` from Task 1 (returns `(provider_or_None, bare_model)` with tail already alias-normalized).
- Produces: `get_provider_for_model()` honors pins (no signature change); handler sends 400 for bad pins before queue/TPM.

- [ ] **Step 1: Write the failing tests**

Unit (`tests/test_units.py`):

```python
class TestPinnedRouting:
    def test_pinned_model_routes_to_pinned_provider(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_API_KEY", "k")
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_BASE_URL", "https://t.example.com/v1")
        router = dashscope_module.ProviderRouter()
        assert router.get_provider_for_model("openlux/gemini-3.7-flash").name == "tertiary"

    def test_pin_to_unconfigured_provider_falls_back(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_API_KEY", "")
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_BASE_URL", "")
        router = dashscope_module.ProviderRouter()
        assert router.get_provider_for_model("openlux/gemini-3.7-flash").name == "primary"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_units.py::TestPinnedRouting -v`
Expected: FAIL (pinned name not found in model sets → routes primary; first test fails)

- [ ] **Step 3: Write minimal implementation**

1. In `provider_router.py::get_provider_for_model`, at the top after `normalize_model_name`:

```python
from dashscope_proxy_lib.request_transform import split_provider_prefix
pinned, bare = split_provider_prefix(model_name)
if pinned is not None:
    provider = getattr(self, pinned)
    if provider.is_available:
        return provider
    model_name = bare  # fall through to normal resolution on bare name
else:
    model_name = bare
```

If a pin targets an unconfigured provider, fall through to normal resolution
on the bare name (layered semantics, pre-flight decision: router falls back so
`get_provider_for_model` stays total; the HTTP handler below returns 400
for the same request, so users never see a silent reroute).

2. In `handlers.py::handle_request`, after `model_name`/`is_stream` extraction (around line 234), insert pin validation before router selection:

```python
from dashscope_proxy_lib.request_transform import split_provider_prefix
pinned_name, bare_name = split_provider_prefix(model_name or "")
if "/" in (model_name or "") and pinned_name is None:
    error_reason = "unknown_provider_prefix"
    status_code = 400
    return _make_error_response(
        400,
        json.dumps({"error": "unknown provider prefix", "model": model_name}).encode(),
        request_id,
    )
if pinned_name is not None:
    provider_check = getattr(router, pinned_name)
    if not provider_check.is_available:
        error_reason = "provider_not_configured"
        status_code = 400
        return _make_error_response(
            400,
            json.dumps({"error": f"provider '{pinned_name}' not configured", "model": model_name}).encode(),
            request_id,
        )
    model_name = bare_name
    body["model"] = bare_name
    body_bytes = json.dumps(body).encode()
```

Note: `router = get_provider_router()` already exists at line 238 — place the validation after it. The stripped `body_bytes` ensures upstream never sees the prefix (LiteLLM convention).

CAUTION (early-exit session logging): the function's early 400-exits before the main try/finally write session entries via `_maybe_flush_session_log(request.app, session_entry)` — follow that pattern for both new 400 returns (set `session_entry["status_code"]`/`["error_reason"]`, await `_maybe_flush_session_log`, then return). Also set `session_entry["model"]` if needed for log usefulness — check how neighboring early-exits do it and match.

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_units.py tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add dashscope_proxy_lib/provider_router.py dashscope_proxy_lib/handlers.py tests/test_units.py tests/test_integration.py
git commit -m "feat: provider-prefixed model routing with 400 on bad pin"
```
