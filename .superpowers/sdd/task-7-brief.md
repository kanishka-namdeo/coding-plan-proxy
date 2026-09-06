# Task 7: Update Documentation Files

**Files:**
- Modify: `AGENTS.md`
- Modify: `README_SCRIPT.md` (if exists)

**What to do:**

1. Search for all "StreamLake" references in documentation files
2. Replace each occurrence with "OpenLux"
3. Verify no StreamLake references remain
4. Commit the changes

**Verification:**
- Run: `rg -n "StreamLake" AGENTS.md README_SCRIPT.md 2>&1`
- Expected: No output (all references updated)

**Commit message:**
```
docs: update documentation files from StreamLake to OpenLux
```
