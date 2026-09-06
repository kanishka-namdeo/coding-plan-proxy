# Task 6: Update Test Files

**Files:**
- Modify: `tests/test_units.py`
- Modify: `tests/test_integration.py`

**What to do:**

1. Search for all "StreamLake" references in test files
2. Replace each occurrence with "OpenLux" in comments and test data
3. Fix the 3 failing tests that reference the old model name `kat-coder-pro-v2.5`:
   - `test_tertiary_model_routed_to_tertiary`
   - `test_get_all_models_includes_tertiary_when_configured`
   - `test_model_provider_map_tertiary_override`
4. Update these tests to use one of the new model names (e.g., `gemini-3.7-flash`)
5. Verify all tests pass
6. Commit the changes

**Verification:**
- Run: `rg -n "StreamLake" tests/`
- Expected: No output (all references updated)
- Run: `py -m pytest tests/test_units.py tests/test_integration.py -v`
- Expected: All tests pass (0 failures)

**Commit message:**
```
test: update test files from StreamLake to OpenLux
```
