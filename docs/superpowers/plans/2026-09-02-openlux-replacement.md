# OpenLux Provider Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace StreamLake tertiary provider with OpenLux, updating environment variables, model list, and all references throughout the codebase.

**Architecture:** Direct replacement approach - keep the "tertiary" provider slot but swap configuration from StreamLake to OpenLux. Update environment variable names from `STREAMLAKE_*` to `OPENLUX_*`, change default base URL to `https://api.openlux.ai/v1`, and update model list to include 4 new models (gemini-3.7-flash, gpt-5.6-terra, gpt-5.6-sol, qwen3.8-max). All internal constant names remain `TERTIARY_*` to minimize code changes.

**Tech Stack:** Python, aiohttp, pytest, Textual TUI

## Global Constraints

- Keep all internal constant names as `TERTIARY_*` (no code-level renaming)
- Maintain backward compatibility - no breaking changes to API
- Preserve existing rate limit defaults and structure
- Update all user-facing labels and comments from "StreamLake" to "OpenLux"

---

### Task 1: Update Configuration Constants

**Files:**
- Modify: `dashscope_proxy_lib/config.py:57-58` (env vars)
- Modify: `dashscope_proxy_lib/config.py:245-249` (TERTIARY_MODELS)
- Modify: `dashscope_proxy_lib/config.py:57` (comment)

**Interfaces:**
- Consumes: Environment variables `OPENLUX_API_KEY`, `OPENLUX_TARGET_BASE`
- Produces: Updated `TERTIARY_API_KEY`, `TERTIARY_BASE_URL`, `TERTIARY_MODELS` constants

- [ ] **Step 1: Update environment variable names and default URL**

Open `dashscope_proxy_lib/config.py` and replace lines 57-58:

```python
# OLD (lines 57-58):
TERTIARY_API_KEY = os.environ.get("STREAMLAKE_API_KEY", "").strip()
TERTIARY_BASE_URL = os.environ.get("STREAMLAKE_TARGET_BASE", "").strip()

# NEW:
TERTIARY_API_KEY = os.environ.get("OPENLUX_API_KEY", "").strip()
TERTIARY_BASE_URL = os.environ.get("OPENLUX_TARGET_BASE", "https://api.openlux.ai/v1").strip()
```

- [ ] **Step 2: Update the comment above the env vars**

Replace line 56 comment:

```python
# OLD (line 56):
# Tertiary provider configuration (optional - StreamLake)

# NEW:
# Tertiary provider configuration (optional - OpenLux)
```

- [ ] **Step 3: Update TERTIARY_MODELS with new model list**

Replace lines 245-249:

```python
# OLD (lines 245-249):
TERTIARY_MODELS = {
    "object": "list",
    "data": [
        {"id": "kat-coder-pro-v2.5", "object": "model"},
    ]
}

# NEW:
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

- [ ] **Step 4: Update comment above TERTIARY_MODELS**

Replace line 244 comment:

```python
# OLD (line 244):
# Tertiary provider models (StreamLake)

# NEW:
# Tertiary provider models (OpenLux)
```

- [ ] **Step 5: Verify changes**

Run: `python -c "from dashscope_proxy_lib.config import TERTIARY_API_KEY, TERTIARY_BASE_URL, TERTIARY_MODELS; print(f'Base URL: {TERTIARY_BASE_URL}'); print(f'Models: {[m[\"id\"] for m in TERTIARY_MODELS[\"data\"]]}')"`

Expected output:
```
Base URL: https://api.openlux.ai/v1
Models: ['gemini-3.7-flash', 'gpt-5.6-terra', 'gpt-5.6-sol', 'qwen3.8-max']
```

- [ ] **Step 6: Commit changes**

```bash
git add dashscope_proxy_lib/config.py
git commit -m "feat: update tertiary provider config from StreamLake to OpenLux

- Rename env vars: STREAMLAKE_* → OPENLUX_*
- Update default base URL to https://api.openlux.ai/v1
- Replace model list with gemini-3.7-flash, gpt-5.6-terra, gpt-5.6-sol, qwen3.8-max
- Update comments to reference OpenLux"
```

---

### Task 2: Update Provider Router Comments

**Files:**
- Modify: `dashscope_proxy_lib/provider_router.py:28` (comment)

**Interfaces:**
- Consumes: Nothing
- Produces: Updated comment for clarity

- [ ] **Step 1: Update comment in _build_tertiary_model_ids function**

Open `dashscope_proxy_lib/provider_router.py` and replace line 28:

```python
# OLD (line 28):
def _build_tertiary_model_ids(models: dict) -> set[str]:
    """Build lookup set for tertiary (StreamLake) models."""
    return {entry["id"] for entry in models.get("data", [])}

# NEW:
def _build_tertiary_model_ids(models: dict) -> set[str]:
    """Build lookup set for tertiary (OpenLux) models."""
    return {entry["id"] for entry in models.get("data", [])}
```

- [ ] **Step 2: Commit changes**

```bash
git add dashscope_proxy_lib/provider_router.py
git commit -m "docs: update provider router comment to reference OpenLux"
```

---

### Task 3: Update Server Comments

**Files:**
- Modify: `dashscope_proxy_lib/server.py` (multiple comments)

**Interfaces:**
- Consumes: Nothing
- Produces: Updated comments for clarity

- [ ] **Step 1: Search for StreamLake references in server.py**

Run: `grep -n "StreamLake" dashscope_proxy_lib/server.py`

Expected: Find all lines containing "StreamLake"

- [ ] **Step 2: Update all StreamLake references to OpenLux**

For each line found, replace "StreamLake" with "OpenLux" in comments. Common locations:
- Line ~106: Comment about tertiary_config initialization
- Line ~112: Log message about multi-provider mode

Example:
```python
# OLD:
# Create tertiary rate limiter if StreamLake is configured

# NEW:
# Create tertiary rate limiter if OpenLux is configured
```

- [ ] **Step 3: Verify no StreamLake references remain**

Run: `grep -n "StreamLake" dashscope_proxy_lib/server.py`

Expected: No output (all references updated)

- [ ] **Step 4: Commit changes**

```bash
git add dashscope_proxy_lib/server.py
git commit -m "docs: update server.py comments to reference OpenLux"
```

---

### Task 4: Update TUI Display Labels

**Files:**
- Modify: `proxy_tui.py` (multiple locations)

**Interfaces:**
- Consumes: Nothing
- Produces: Updated user-facing labels in TUI

- [ ] **Step 1: Search for StreamLake references in proxy_tui.py**

Run: `grep -n "StreamLake" proxy_tui.py`

Expected: Find all lines containing "StreamLake" in labels and status text

- [ ] **Step 2: Update all StreamLake labels to OpenLux**

Replace all occurrences of "StreamLake" with "OpenLux" in:
- Panel titles
- Status line text
- Any user-visible labels

Example:
```python
# OLD:
yield Static("StreamLake", classes="provider-name")
yield Static("StreamLake: Not configured", id="tertiary-status")

# NEW:
yield Static("OpenLux", classes="provider-name")
yield Static("OpenLux: Not configured", id="tertiary-status")
```

- [ ] **Step 3: Verify no StreamLake references remain**

Run: `grep -n "StreamLake" proxy_tui.py`

Expected: No output (all references updated)

- [ ] **Step 4: Commit changes**

```bash
git add proxy_tui.py
git commit -m "feat: update TUI labels from StreamLake to OpenLux"
```

---

### Task 5: Update Environment Example File

**Files:**
- Modify: `.env.example`

**Interfaces:**
- Consumes: Nothing
- Produces: Updated environment variable documentation

- [ ] **Step 1: Update environment variable names**

Open `.env.example` and replace:

```bash
# OLD:
# StreamLake API key (optional - for tertiary provider)
STREAMLAKE_API_KEY=your_streamlake_key_here

# StreamLake target base URL (optional - for tertiary provider)
STREAMLAKE_TARGET_BASE=https://vanchin.streamlake.ai/api/gateway/coding/v1

# NEW:
# OpenLux API key (optional - for tertiary provider)
OPENLUX_API_KEY=your_openlux_key_here

# OpenLux target base URL (optional - for tertiary provider)
OPENLUX_TARGET_BASE=https://api.openlux.ai/v1
```

- [ ] **Step 2: Verify changes**

Run: `grep -E "(STREAMLAKE|OPENLUX)" .env.example`

Expected: Only OPENLUX references should appear

- [ ] **Step 3: Commit changes**

```bash
git add .env.example
git commit -m "docs: update .env.example with OpenLux configuration"
```

---

### Task 6: Update Test Files

**Files:**
- Modify: `tests/test_units.py` (comments and test data)
- Modify: `tests/test_integration.py` (comments and test data)

**Interfaces:**
- Consumes: Nothing
- Produces: Updated test documentation

- [ ] **Step 1: Search for StreamLake references in test files**

Run: `grep -n "StreamLake" tests/test_units.py tests/test_integration.py`

Expected: Find all lines containing "StreamLake" in comments or test data

- [ ] **Step 2: Update all StreamLake references to OpenLux**

For each occurrence, replace "StreamLake" with "OpenLux" in:
- Comments describing test scenarios
- Test data strings
- Docstrings

Example:
```python
# OLD:
# Test StreamLake provider routing

# NEW:
# Test OpenLux provider routing
```

- [ ] **Step 3: Verify no StreamLake references remain**

Run: `grep -n "StreamLake" tests/test_units.py tests/test_integration.py`

Expected: No output (all references updated)

- [ ] **Step 4: Run tests to verify nothing broke**

Run: `pytest tests/test_units.py tests/test_integration.py -v`

Expected: All tests pass

- [ ] **Step 5: Commit changes**

```bash
git add tests/test_units.py tests/test_integration.py
git commit -m "test: update test comments to reference OpenLux"
```

---

### Task 7: Update Documentation Files

**Files:**
- Modify: `AGENTS.md`
- Modify: `README_SCRIPT.md` (if exists)

**Interfaces:**
- Consumes: Nothing
- Produces: Updated documentation

- [ ] **Step 1: Search for StreamLake references in documentation**

Run: `grep -n "StreamLake" AGENTS.md README_SCRIPT.md 2>/dev/null || echo "No matches or file not found"`

Expected: Find all lines containing "StreamLake"

- [ ] **Step 2: Update all StreamLake references to OpenLux**

For each occurrence, replace "StreamLake" with "OpenLux" in:
- Architecture descriptions
- Provider explanations
- Configuration examples

Example:
```markdown
# OLD:
- `dashscope_proxy_lib/` — proxy core with StreamLake integration

# NEW:
- `dashscope_proxy_lib/` — proxy core with OpenLux integration
```

- [ ] **Step 3: Verify no StreamLake references remain**

Run: `grep -n "StreamLake" AGENTS.md README_SCRIPT.md 2>/dev/null || echo "No matches"`

Expected: No output (all references updated)

- [ ] **Step 4: Commit changes**

```bash
git add AGENTS.md README_SCRIPT.md
git commit -m "docs: update documentation to reference OpenLux"
```

---

### Task 8: Verification and Integration Testing

**Files:**
- None (verification only)

**Interfaces:**
- Consumes: All previous changes
- Produces: Verified working implementation

- [ ] **Step 1: Verify no StreamLake references remain in codebase**

Run: `grep -r "StreamLake" --include="*.py" --include="*.md" --include=".env*" . 2>/dev/null | grep -v ".git" | grep -v "docs/superpowers"`

Expected: No output (all references updated, excluding design docs)

- [ ] **Step 2: Verify OpenLux references are present**

Run: `grep -r "OpenLux" --include="*.py" --include="*.md" --include=".env*" . 2>/dev/null | grep -v ".git" | head -20`

Expected: Multiple lines showing OpenLux references in updated files

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -v`

Expected: All tests pass

- [ ] **Step 4: Test configuration loading**

Run: `python -c "from dashscope_proxy_lib.config import TERTIARY_MODELS; import json; print(json.dumps(TERTIARY_MODELS, indent=2))"`

Expected: JSON output showing 4 new models

- [ ] **Step 5: Test provider router initialization**

Run:
```python
python -c "
from dashscope_proxy_lib.provider_router import ProviderRouter
router = ProviderRouter()
print('Tertiary configured:', router.is_tertiary_configured())
print('Tertiary model IDs:', router._tertiary_model_ids)
"
```

Expected:
```
Tertiary configured: False (unless OPENLUX_API_KEY is set)
Tertiary model IDs: {'gemini-3.7-flash', 'gpt-5.6-terra', 'gpt-5.6-sol', 'qwen3.8-max'}
```

- [ ] **Step 6: Test with actual OpenLux API key (if available)**

If you have an OpenLux API key:
1. Set environment variables:
   ```bash
   export OPENLUX_API_KEY=your_actual_key
   export OPENLUX_TARGET_BASE=https://api.openlux.ai/v1
   ```

2. Start the proxy:
   ```bash
   python dashscope_proxy.py
   ```

3. In another terminal, test model listing:
   ```bash
   curl http://localhost:8899/v1/models | jq '.data[] | select(.id | test("gpt-5.6|gemini-3.7|qwen3.8"))'
   ```

4. Test routing to a new model:
   ```bash
   curl http://localhost:8899/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "gpt-5.6-terra",
       "messages": [{"role": "user", "content": "Hello"}]
     }'
   ```

Expected: Request routes to OpenLux provider

- [ ] **Step 7: Verify TUI displays correctly**

If running with TUI:
1. Start proxy with TUI: `python dashscope_proxy.py`
2. Check that "OpenLux" label appears in tertiary provider section
3. Verify status shows "OpenLux: Not configured" or "OpenLux: Active" depending on env vars

- [ ] **Step 8: Create final summary commit**

```bash
git add -A
git status
git commit -m "chore: complete OpenLux provider replacement

- All StreamLake references updated to OpenLux
- Environment variables renamed (STREAMLAKE_* → OPENLUX_*)
- Model list updated with 4 new models
- All tests passing
- Documentation updated"
```

---

## Post-Implementation Checklist

After completing all tasks, verify:

- [ ] All StreamLake references removed from code (except design docs)
- [ ] Environment variables renamed in `.env.example`
- [ ] All 4 new models appear in TERTIARY_MODELS
- [ ] TUI displays "OpenLux" label
- [ ] All tests pass
- [ ] Provider router correctly identifies new models
- [ ] Documentation updated
- [ ] No breaking changes to API

## Migration Notes for Users

Users need to:
1. Update their `.env` file: rename `STREAMLAKE_*` to `OPENLUX_*`
2. Obtain OpenLux API key from https://api.openlux.ai
3. Set `OPENLUX_TARGET_BASE=https://api.openlux.ai/v1`
4. Restart proxy service
5. Update any scripts or configurations using old model names
