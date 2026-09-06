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

NOTE: `MOCK_MODELS` is not currently imported in `provider_router.py` — extend the existing top-of-file `from dashscope_proxy_lib.config import (...)` to include it (file convention is top-level imports for model lists). CAUTION: `get_all_models()` does a local `from dashscope_proxy_lib.config import MOCK_MODELS` inside the function — switch it to use the top-level import so `monkeypatch.setattr("dashscope_proxy_lib.config.MOCK_MODELS", ...)` in the test takes effect (a top-level `from` binding would freeze the reference and break the patch). Either import the module (`from dashscope_proxy_lib import config`) and use `config.MOCK_MODELS`, or keep the function-local import. Pick one and be consistent: reads of `MOCK_MODELS` must go through the module attribute at call time, not a frozen top-level binding.

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
