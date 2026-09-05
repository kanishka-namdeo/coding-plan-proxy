# Overlapping-Model Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support overlapping model IDs across providers via `<provider>/<model>` pin syntax, overlap failover, and deduped `/v1/models`.

**Architecture:** Add prefix parse/strip in `request_transform.py`, an overlap registry plus slug aliases in `provider_router.py`, a pre-stream provider-rotation failover loop in `handlers.py`, and one new `MODEL_FALLBACK_ORDER` env in `config.py`. All changes additive; bare names behave exactly as today.

**Tech Stack:** Python 3, aiohttp, pytest (`asyncio_mode = "auto"`).

## Global Constraints

- Facade pattern: tests patch `dashscope_proxy.CONSTANT`, so new constants must resolve at runtime via `_cfg()` helpers, never direct imports.
- Rate limiter configs in tests use large limits (6000 RPM, 10M TPM); backoffs tiny (0.05s base).
- Integration tests use `aiohttp_client`, inline mock upstreams, `_patch_target_base()` context manager, and the autouse `_reset_provider_router` fixture.
- Async tests rely on `asyncio_mode = "auto"` (no marker needed).
- DOX pass: update `dashscope_proxy_lib/AGENTS.md` provider-routing bullet when routing semantics change.

---

### Task 1: Prefix parsing + new provider slugs

**Files:**
- Modify: `dashscope_proxy_lib/request_transform.py` (add `split_provider_prefix()`, extend `normalize_model_name()`)
- Modify: `dashscope_proxy.py` (re-export `split_provider_prefix`)
- Modify: `dashscope_proxy_lib/config.py` (add `PROVIDER_SLUGS` dict, `MODEL_FALLBACK_ORDER` env)
- Test: `tests/test_units.py` (append new test class)

**Interfaces:**
- Consumes: existing `normalize_model_name(model_name: str) -> str`.
- Produces: `split_provider_prefix(model: str) -> tuple[str | None, str]`; `PROVIDER_SLUGS: dict[str, str]` mapping new slug → canonical provider name (`{"dashscope": "primary", "mimo": "secondary", "openlux": "tertiary", "ark": "quaternary", "metaspark": "quinary", "deepseek": "senary"}`); `MODEL_FALLBACK_ORDER: list[str]` (parsed from env, may be `[]`).

- [ ] **Step 1: Write the failing test**

```python
class TestSplitProviderPrefix:
    def test_bare_model_returns_no_prefix(self, dashscope_module):
        from dashscope_proxy_lib.request_transform import split_provider_prefix
        assert split_provider_prefix("gpt-5.6-sol") == (None, "gpt-5.6-sol")

    def test_openlux_prefix_splits(self, dashscope_module):
        from dashscope_proxy_lib.request_transform import split_provider_prefix
        assert split_provider_prefix("openlux/gpt-5.6-sol") == ("tertiary", "gpt-5.6-sol")

    def test_prefix_alias_normalized(self, dashscope_module):
        from dashscope_proxy_lib.request_transform import split_provider_prefix
        assert split_provider_prefix("mimo/mimo-v2-5-pro") == ("secondary", "mimo-v2.5-pro")

    def test_unknown_slug_returns_none(self, dashscope_module):
        from dashscope_proxy_lib.request_transform import split_provider_prefix
        assert split_provider_prefix("nosuch/gpt-5.6-sol") == (None, "nosuch/gpt-5.6-sol")

    def test_positional_alias_still_works(self, dashscope_module):
        from dashscope_proxy_lib.request_transform import split_provider_prefix
        assert split_provider_prefix("tertiary/gpt-5.6-sol") == ("tertiary", "gpt-5.6-sol")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_units.py::TestSplitProviderPrefix -v`
Expected: FAIL with "cannot import name 'split_provider_prefix'"

- [ ] **Step 3: Write minimal implementation**

In `dashscope_proxy_lib/request_transform.py`, after `normalize_model_name`, add:

```python
PROVIDER_SLUG_MAP = {
    "dashscope": "primary", "primary": "primary",
    "mimo": "secondary", "secondary": "secondary",
    "openlux": "tertiary", "tertiary": "tertiary",
    "ark": "quaternary", "quaternary": "quaternary",
    "metaspark": "quinary", "quinary": "quinary",
    "deepseek": "senary", "senary": "senary",
}


def split_provider_prefix(model: str) -> tuple:
    """Split optional '<provider>/<model>' prefix. Returns (provider_or_None, bare_model)."""
    if "/" not in model:
        return None, model
    head, _, tail = model.partition("/")
    provider = PROVIDER_SLUG_MAP.get(head.lower())
    if provider is None or not tail:
        return None, model
    return provider, normalize_model_name(tail)
```

In `dashscope_proxy_lib/config.py`, after `MODEL_PROVIDER_MAP`, add:

```python
import os as _os

PROVIDER_SLUGS: dict[str, str] = {
    "dashscope": "primary",
    "mimo": "secondary",
    "openlux": "tertiary",
    "ark": "quaternary",
    "metaspark": "quinary",
    "deepseek": "senary",
}

MODEL_FALLBACK_ORDER: list[str] = [
    s.strip().lower()
    for s in _os.environ.get("MODEL_FALLBACK_ORDER", "").split(",")
    if s.strip()
]
```

In `dashscope_proxy.py`, add `split_provider_prefix` to the `request_transform` import and `__all__` list (same import block at lines 120-124, same `__all__` section at lines 178-179).

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m pytest tests/test_units.py::TestSplitProviderPrefix tests/test_units.py -v`
Expected: PASS (full unit file, no regressions)

- [ ] **Step 5: Commit**

```bash
git add dashscope_proxy_lib/request_transform.py dashscope_proxy_lib/config.py dashscope_proxy.py tests/test_units.py
git commit -m "feat: provider prefix parsing with slug aliases"
```

### Task 2: Overlap registry + deduped `/v1/models`

**Files:**
- Modify: `dashscope_proxy_lib/provider_router.py` (`ProviderRouter.__init__`, `get_all_models()`, add `get_providers_for_model()`, `get_model_overlaps()`)
- Test: `tests/test_units.py` (append new test class)

**Interfaces:**
- Consumes: `split_provider_prefix()` from Task 1; `MODEL_FALLBACK_ORDER` from Task 1.
- Produces: `ProviderRouter.get_providers_for_model(model_name: str) -> list[ProviderConfig]` (all configured providers serving the bare ID, in try-order); `ProviderRouter.get_model_overlaps() -> dict[str, list[str]]` (`model_id → [provider names]`, only multi-provider entries).

- [ ] **Step 1: Write the failing test**

```python
class TestOverlapRegistry:
    def test_single_provider_model_returns_one(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_API_KEY", "k")
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_BASE_URL", "https://t.example.com/v1")
        router = dashscope_module.ProviderRouter()
        providers = router.get_providers_for_model("gemini-3.7-flash")
        assert [p.name for p in providers] == ["tertiary"]

    def test_unknown_model_returns_empty(self, dashscope_module):
        router = dashscope_module.ProviderRouter()
        assert router.get_providers_for_model("no-such-model-xyz") == []

    def test_get_all_models_dedupes_overlap(self, dashscope_module, monkeypatch):
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_API_KEY", "k")
        monkeypatch.setattr("dashscope_proxy_lib.config.TERTIARY_BASE_URL", "https://t.example.com/v1")
        monkeypatch.setattr(
            "dashscope_proxy_lib.config.TERTIARY_MODELS",
            {"object": "list", "data": [{"id": "dup-model", "object": "model"}]},
        )
        monkeypatch.setattr(
            "dashscope_proxy_lib.config.MOCK_MODELS",
            {"object": "list", "data": [{"id": "dup-model", "object": "model"}]},
        )
        router = dashscope_module.ProviderRouter()
        models = router.get_all_models()
        ids = [m["id"] for m in models["data"]]
        assert ids.count("dup-model") == 1
        entry = next(m for m in models["data"] if m["id"] == "dup-model")
        assert entry["providers"] == ["primary", "tertiary"]
        assert entry["provider_models"] == ["dashscope/dup-model", "openlux/dup-model"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_units.py::TestOverlapRegistry -v`
Expected: FAIL with "has no attribute 'get_providers_for_model'"

- [ ] **Step 3: Write minimal implementation**

In `dashscope_proxy_lib/provider_router.py`:

1. In `__init__`, after the `_xxx_model_ids` sets are built, build the registry.
   Seed with primary's `MOCK_MODELS` ids first (primary is always available),
   then senary → secondary (pre-flight decision: registry includes primary so
   `get_providers_for_model` covers primary-served models):

```python
from dashscope_proxy_lib.config import MOCK_MODELS
self._overlap_registry: dict[str, list[str]] = {}
for _entry in MOCK_MODELS.get("data", []):
    self._overlap_registry.setdefault(_entry["id"], []).append("primary")
_all_sets = [
    ("senary", self._senary_model_ids),
    ("quinary", self._quinary_model_ids),
    ("quaternary", self._quaternary_model_ids),
    ("tertiary", self._tertiary_model_ids),
    ("secondary", self._secondary_model_ids),
]
for _pname, _ids in _all_sets:
    if getattr(self, _pname).is_available:
        for _mid in _ids:
            self._overlap_registry.setdefault(_mid, []).append(_pname)
```

2. Add methods (place after `get_provider_for_model`):

```python
def get_providers_for_model(self, model_name: str) -> list:
    """All configured providers serving the bare model ID, in try-order."""
    from dashscope_proxy_lib.request_transform import split_provider_prefix
    _, bare = split_provider_prefix(model_name)
    names = list(self._overlap_registry.get(bare, []))
    order = _cfg("MODEL_FALLBACK_ORDER") or []
    if order:
        names.sort(key=lambda n: order.index(n) if n in order else len(order))
    return [getattr(self, n) for n in names]

def get_model_overlaps(self) -> dict:
    """model_id -> provider names, only entries served by 2+ providers."""
    return {m: list(p) for m, p in self._overlap_registry.items() if len(p) > 1}
```

3. Rewrite `get_all_models()` to dedupe: iterate primary + each configured provider's list in existing order, keep first occurrence of each `id`, then attach `providers` and `provider_models` from the registry. `providers` holds canonical provider names (`primary`, `secondary`, …) matching `get_provider_status()` keys; `provider_models` uses the new slugs via `_slug_for = {"primary": "dashscope", "secondary": "mimo", "tertiary": "openlux", "quaternary": "ark", "quinary": "metaspark", "senary": "deepseek"}`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_units.py -v`
Expected: PASS, no regressions (existing `test_get_all_models_*` tests use `len()` and membership assertions that still hold)

- [ ] **Step 5: Commit**

```bash
git add dashscope_proxy_lib/provider_router.py tests/test_units.py
git commit -m "feat: overlap registry and deduped model list"
```

### Task 3: Prefix-pin routing + 400 on bad pin

**Files:**
- Modify: `dashscope_proxy_lib/provider_router.py` (`get_provider_for_model()`)
- Modify: `dashscope_proxy_lib/handlers.py` (parse prefix at model extraction, strip before upstream, 400 on unknown/unconfigured pin)
- Test: `tests/test_units.py` + `tests/test_integration.py` (append tests)

**Interfaces:**
- Consumes: `split_provider_prefix()`, `get_providers_for_model()` from Tasks 1-2.
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
`get_provider_for_model` stays total; the HTTP handler in Task 3 returns 400
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_units.py tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add dashscope_proxy_lib/provider_router.py dashscope_proxy_lib/handlers.py tests/test_units.py tests/test_integration.py
git commit -m "feat: provider-prefixed model routing with 400 on bad pin"
```

### Task 4: Cross-provider failover on 429/5xx/timeout

**Files:**
- Modify: `dashscope_proxy_lib/handlers.py` (`handle_request` retry loop → provider rotation on retryable upstream failures)
- Test: `tests/test_integration.py` (append failover tests)

**Interfaces:**
- Consumes: `router.get_providers_for_model()` from Task 2; existing `should_retry_429`, `_compute_backoff`, per-provider `limiter`.
- Produces: session entry gains `attempted_providers: list[str]`; structured failover log per hop.

- [ ] **Step 1: Write the failing test**

```python
async def test_overlap_failover_on_502(aiohttp_client):
    """Overlapping model on tertiary+quaternary: 502 on first → served by second."""
    async def tertiary_handler(request):
        return web.Response(status=502, text='{"error":"bad gateway"}')
    async def quaternary_handler(request):
        return web.json_response({"choices": [], "usage": {"total_tokens": 5}})

    t_app = web.Application()
    t_app.router.add_post("/v1/chat/completions", tertiary_handler)
    t_client = await aiohttp_client(t_app)
    q_app = web.Application()
    q_app.router.add_post("/v1/chat/completions", quaternary_handler)
    q_client = await aiohttp_client(q_app)

    import dashscope_proxy_lib.config as _c
    _c.TERTIARY_API_KEY = "t-key"
    _c.TERTIARY_BASE_URL = str(t_client.server.make_url("/v1")).rstrip("/")
    _c.TERTIARY_MODELS["data"].append({"id": "overlap-failover-model", "object": "model"})
    _c.QUATERNARY_API_KEY = "q-key"
    _c.QUATERNARY_BASE_URL = str(q_client.server.make_url("/v1")).rstrip("/")
    _c.QUATERNARY_MODELS["data"].append({"id": "overlap-failover-model", "object": "model"})
    try:
        app = dashscope_proxy.create_app()
        app["rate_limiter"] = dashscope_proxy.MultiProviderRateLimiter(make_test_config())
        app["shutting_down"] = asyncio.Event()
        client = await aiohttp_client(app)
        resp = await client.post("/v1/chat/completions", json={
            "model": "overlap-failover-model",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status == 200
    finally:
        _c.TERTIARY_MODELS["data"][:] = [m for m in _c.TERTIARY_MODELS["data"] if m["id"] != "overlap-failover-model"]
        _c.QUATERNARY_MODELS["data"][:] = [m for m in _c.QUATERNARY_MODELS["data"] if m["id"] != "overlap-failover-model"]

async def test_pinned_request_does_not_fail_over(aiohttp_client):
    """Pinned provider returning 502 → 502 to client, no cross-provider retry."""
    async def bad_handler(request):
        return web.Response(status=502, text='{"error":"bad gateway"}')
    bad_app = web.Application()
    bad_app.router.add_post("/v1/chat/completions", bad_handler)
    bad_client = await aiohttp_client(bad_app)

    import dashscope_proxy_lib.config as _c
    _c.TERTIARY_API_KEY = "t-key"
    _c.TERTIARY_BASE_URL = str(bad_client.server.make_url("/v1")).rstrip("/")
    _c.TERTIARY_MODELS["data"].append({"id": "pinned-failover-model", "object": "model"})
    try:
        app = dashscope_proxy.create_app()
        app["rate_limiter"] = dashscope_proxy.MultiProviderRateLimiter(make_test_config())
        app["shutting_down"] = asyncio.Event()
        client = await aiohttp_client(app)
        resp = await client.post("/v1/chat/completions", json={
            "model": "openlux/pinned-failover-model",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status == 502
    finally:
        _c.TERTIARY_MODELS["data"][:] = [m for m in _c.TERTIARY_MODELS["data"] if m["id"] != "pinned-failover-model"]

async def test_upstream_400_does_not_fail_over(aiohttp_client):
    """Upstream 400 is terminal — no cross-provider retry."""
    async def bad_handler(request):
        return web.Response(status=400, text='{"error":"bad request"}')
    async def good_handler(request):
        return web.json_response({"choices": []})
    t_app = web.Application()
    t_app.router.add_post("/v1/chat/completions", bad_handler)
    t_client = await aiohttp_client(t_app)
    q_app = web.Application()
    q_app.router.add_post("/v1/chat/completions", good_handler)
    q_client = await aiohttp_client(q_app)

    import dashscope_proxy_lib.config as _c
    _c.TERTIARY_API_KEY = "t-key"
    _c.TERTIARY_BASE_URL = str(t_client.server.make_url("/v1")).rstrip("/")
    _c.TERTIARY_MODELS["data"].append({"id": "overlap-400-model", "object": "model"})
    _c.QUATERNARY_API_KEY = "q-key"
    _c.QUATERNARY_BASE_URL = str(q_client.server.make_url("/v1")).rstrip("/")
    _c.QUATERNARY_MODELS["data"].append({"id": "overlap-400-model", "object": "model"})
    try:
        app = dashscope_proxy.create_app()
        app["rate_limiter"] = dashscope_proxy.MultiProviderRateLimiter(make_test_config())
        app["shutting_down"] = asyncio.Event()
        client = await aiohttp_client(app)
        resp = await client.post("/v1/chat/completions", json={
            "model": "overlap-400-model",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status == 400
    finally:
        _c.TERTIARY_MODELS["data"][:] = [m for m in _c.TERTIARY_MODELS["data"] if m["id"] != "overlap-400-model"]
        _c.QUATERNARY_MODELS["data"][:] = [m for m in _c.QUATERNARY_MODELS["data"] if m["id"] != "overlap-400-model"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest tests/test_integration.py::test_overlap_failover_on_502 tests/test_integration.py::test_pinned_request_does_not_fail_over tests/test_integration.py::test_upstream_400_does_not_fail_over -v`
Expected: FAIL (first test gets 502 — no failover exists; collection may fail on missing `asyncio` marker — file relies on `asyncio_mode = "auto"`, so plain `async def test_` matches existing tests in the file)

- [ ] **Step 3: Write minimal implementation**

In `handlers.py::handle_request`, restructure provider selection (line ~237) into an ordered candidate list with rotation:

```python
router = get_provider_router()
if pinned_name is not None:
    candidates = [getattr(router, pinned_name)]
else:
    overlap = router.get_providers_for_model(model_name or "")
    if overlap:
        candidates = overlap
    else:
        candidates = [router.get_provider_for_model(model_name or "")]
provider = candidates[0]
provider_name = provider.name
attempted_providers: list[str] = [provider_name]
session_entry["attempted_providers"] = attempted_providers
candidate_idx = 0
```

Wrap the existing `while retry <= ...` loop so that on terminal-per-provider failures (retryable 429 exhausted for this provider, `MAX_5XX_RETRIES` exceeded for this provider, connection error retries exhausted) the handler advances `candidate_idx` to the next candidate that (a) is not pinned-only-skipped and (b) whose limiter circuit is not open, rebuilds `target_url`/`headers`/`limiter` for the new provider, logs `_log(logging.WARNING, "provider failover", request_id=request_id, model=model_name, from_provider=..., to_provider=..., reason=...)`, appends to `attempted_providers`, and continues. Rules:

- Pinned requests (`pinned_name is not None`): `candidates` has length 1 → no rotation, existing behavior unchanged.
- Upstream `4xx` (non-429): return immediately, no rotation (terminal).
- Quota-429 (`should_retry_429` false): keep existing same-provider cooldown path, no rotation.
- `5xx` / retryable-429 / `aiohttp.ClientError` / timeout: rotate to next candidate; only return 502 after all candidates exhausted.
- Queue wait + TPM reservation happen once before the loop (existing code); reconcile/refund once at the end (existing code) — no double-charge across hops.
- Streaming: rotation applies to pre-stream status decisions only; once `resp.prepare()` succeeds, keep the existing streaming path with no mid-stream switch.

Keep the diff minimal: introduce a small local helper `_switch_to_next_provider()` inside `handle_request` that returns `False` when no candidates remain.

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest tests/test_integration.py -v`
Expected: PASS (full integration file, no regressions)

- [ ] **Step 5: Commit**

```bash
git add dashscope_proxy_lib/handlers.py tests/test_integration.py
git commit -m "feat: cross-provider failover for overlapping models"
```

### Task 5: Status overlaps, facade exports, docs, full suite

**Files:**
- Modify: `dashscope_proxy_lib/handlers.py` (`/v1/proxy/status` → add `model_overlaps`)
- Modify: `dashscope_proxy.py` (re-export `PROVIDER_SLUGS`, `MODEL_FALLBACK_ORDER`)
- Modify: `dashscope_proxy_lib/AGENTS.md` (update provider-routing bullet)
- Modify: `.env.example` (document `MODEL_FALLBACK_ORDER`)
- Test: full suite `py -m pytest tests/`

**Interfaces:**
- Consumes: `get_model_overlaps()` from Task 2.
- Produces: `GET /v1/proxy/status` includes `model_overlaps: {model_id: [providers]}`.

- [ ] **Step 1: Add `model_overlaps` to status endpoint**

In `handlers.py`, in the `/v1/proxy/status` branch (line ~111), extend:

```python
status = {
    "rate_limits": rate_limiter.status(),
    "providers": router.get_provider_status(),
    "model_overlaps": router.get_model_overlaps(),
}
```

- [ ] **Step 2: Update facade exports**

In `dashscope_proxy.py`, add `PROVIDER_SLUGS` and `MODEL_FALLBACK_ORDER` to the config import block and `__all__` (alongside existing `MODEL_PROVIDER_MAP`-style entries).

- [ ] **Step 3: Update docs**

In `dashscope_proxy_lib/AGENTS.md`, update the provider-routing bullet to: explicit `provider/model` pin first (prefix stripped before upstream), then `MODEL_PROVIDER_MAP`, then overlap-set failover in `MODEL_FALLBACK_ORDER` (default senary→…→primary), then primary default; `/v1/models` deduped with `providers`/`provider_models` fields.

In `.env.example`, append:

```
# Optional: overlap failover try-order (canonical provider names, comma-separated).
# Unset = senary,quinary,quaternary,tertiary,secondary,primary.
# MODEL_FALLBACK_ORDER=openlux,ark,dashscope
```

- [ ] **Step 4: Run full test suite**

Run: `py -m pytest tests/ -v`
Expected: PASS (unit + integration; e2e skipped without API key)

- [ ] **Step 5: Commit**

```bash
git add dashscope_proxy_lib/handlers.py dashscope_proxy.py dashscope_proxy_lib/AGENTS.md .env.example
git commit -m "feat: overlap status, exports, and docs"
```

## Self-Review

**1. Spec coverage:**
- §1 syntax + resolution order → Tasks 1, 3 (parse/strip, slugs, pin→400, MAP second, bare default).
- §1 `mimo-v2-5` alias under prefix → Task 1 test `test_prefix_alias_normalized`.
- §2 overlap registry → Task 2 (`get_providers_for_model`, `get_model_overlaps`).
- §2 `/v1/models` dedup + `providers`/`provider_models` → Task 2.
- §3 failover on 429/5xx/timeout only, never 4xx → Task 4 (three integration tests).
- §3 single queue/TPM across hops, per-provider circuit skip, `attempted_providers`, budgets unchanged, quota-429 stays → Task 4 implementation rules.
- §3 no mid-stream switch → Task 4 rules.
- §4 `MODEL_PROVIDER_MAP` slugs, `MODEL_FALLBACK_ORDER`, no new required env → Tasks 1, 3, 5.
- §4 `model_overlaps` status, failover logs, no TUI changes → Task 5.
- §4 unit + integration + backward-compat tests → Tasks 1-4 tests, Task 5 full suite.
- No gaps found.

**2. Placeholder scan:** no TBD/TODO/"similar to"/missing code — every step has exact code, paths, commands, expected output.

**3. Type consistency:** `split_provider_prefix` returns `tuple[str | None, str]` throughout; `get_providers_for_model` returns `list[ProviderConfig]`; `get_model_overlaps` returns `dict[str, list[str]]`; `MODEL_FALLBACK_ORDER` is `list[str]`; `attempted_providers` is `list[str]`. Consistent across tasks.
