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

In `handlers.py`, in the `/v1/proxy/status` branch (around line 111), extend the status dict:

```python
status = {
    "rate_limits": rate_limiter.status(),
    "providers": router.get_provider_status(),
    "model_overlaps": router.get_model_overlaps(),
}
```

- [ ] **Step 2: Update facade exports**

In `dashscope_proxy.py`, add `PROVIDER_SLUGS` and `MODEL_FALLBACK_ORDER` to the config import block (around line 50-80) and to `__all__` (around line 160-190), alongside the other config names.

- [ ] **Step 3: Update docs**

**AGENTS.md** — The Task 4 reviewer may have already updated the routing bullet in the working tree. Verify and complete if needed. The provider-routing bullet should document:
- Explicit `provider/model` pin first (prefix stripped before upstream)
- Then `MODEL_PROVIDER_MAP`
- Then overlap-set failover in `MODEL_FALLBACK_ORDER` order (default senary→quinary→quaternary→tertiary→secondary→primary)
- Then primary default
- `/v1/models` is deduped with `providers`/`provider_models` fields

**.env.example** — Append:

```
# Optional: overlap failover try-order (canonical provider names, comma-separated).
# Unset = senary,quinary,quaternary,tertiary,secondary,primary.
# MODEL_FALLBACK_ORDER=openlux,ark,dashscope
```

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: unit + integration pass; e2e skipped without API key; the 4 pre-existing failures in secondary/quaternary routing tests will still be present (unrelated to this work)

- [ ] **Step 5: Commit**

```bash
git add dashscope_proxy_lib/handlers.py dashscope_proxy.py dashscope_proxy_lib/AGENTS.md .env.example
git commit -m "feat: overlap status, exports, and docs"
```

NOTE: The tree has unrelated uncommitted changes in other files (rate_limiter.py, server.py, proxy_tui.py, etc.) — DO NOT commit them. Use `git add` on only the four paths listed above.