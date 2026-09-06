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

NOTE: `config.py` already has `import os` at the top — use plain `os`, not `os as _os`.

In `dashscope_proxy.py`, add `split_provider_prefix` to the `request_transform` import and `__all__` list (same import block at lines 120-124, same `__all__` section at lines 178-179). Also add `PROVIDER_SLUGS` and `MODEL_FALLBACK_ORDER` to the config import block and `__all__` (alongside the other config names).

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m pytest tests/test_units.py::TestSplitProviderPrefix tests/test_units.py -v`
Expected: PASS (full unit file, no regressions)

- [ ] **Step 5: Commit**

```bash
git add dashscope_proxy_lib/request_transform.py dashscope_proxy_lib/config.py dashscope_proxy.py tests/test_units.py
git commit -m "feat: provider prefix parsing with slug aliases"
```
