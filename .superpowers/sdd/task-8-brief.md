# Task 8: Verification and Integration Testing

**What to do:**

Perform final verification that all StreamLake references have been replaced with OpenLux across the entire codebase.

## Verification Steps

1. **Search for remaining StreamLake references** (excluding design docs):
   ```
   rg -n "StreamLake" --type py --type md .env.example 2>&1
   ```
   Expected: No output (all references updated)

2. **Verify OpenLux references are present**:
   ```
   rg -n "OpenLux" --type py --type md .env.example | head -20
   ```
   Expected: Multiple lines showing OpenLux references

3. **Run full test suite**:
   ```
   python -m pytest tests/ -v
   ```
   Expected: All tests pass (note: integration tests may have pre-existing fixture issues)

4. **Verify configuration loads correctly**:
   ```python
   python -c "from dashscope_proxy_lib.config import TERTIARY_MODELS; import json; print(json.dumps(TERTIARY_MODELS, indent=2))"
   ```
   Expected: JSON output showing 4 new models (gemini-3.7-flash, gpt-5.6-terra, gpt-5.6-sol, qwen3.8-max)

5. **Verify provider router initialization**:
   ```python
   python -c "
   from dashscope_proxy_lib.provider_router import ProviderRouter
   router = ProviderRouter()
   print('Tertiary configured:', router.is_tertiary_configured())
   print('Tertiary model IDs:', router._tertiary_model_ids)
   "
   ```
   Expected:
   - Tertiary configured: False (unless OPENLUX_API_KEY is set)
   - Tertiary model IDs: {'gemini-3.7-flash', 'gpt-5.6-terra', 'gpt-5.6-sol', 'qwen3.8-max'}

6. **Create final summary commit** (if any files were missed):
   ```
   git add -A
   git status
   ```

## Report Format

Write your full report to `.superpowers/sdd/task-8-report.md`:
- Verification results for each step
- Any issues found
- Final status of the migration

Then report back with ONLY (under 15 lines):
- **Status:** DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT
- One-line summary of verification results
- Any concerns or issues found
- The report file path
