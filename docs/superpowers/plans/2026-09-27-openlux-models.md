# OpenLux Models Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 4 missing OpenLux models to the tertiary provider model list.

**Architecture:** Simple config update - add model IDs to `TERTIARY_MODELS` in `dashscope_proxy_lib/config.py`.

**Tech Stack:** Python, existing proxy configuration system.

## Global Constraints

- Follow existing model naming conventions in `TERTIARY_MODELS`
- Maintain alphabetical order within the list (optional but consistent)
- No breaking changes to existing functionality

---

### Task 1: Add Missing OpenLux Models to Config

**Files:**
- Modify: `dashscope_proxy_lib/config.py:433-451`
- Test: `tests/test_units.py` (existing tests cover this)

**Interfaces:**
- Consumes: None (config-only change)
- Produces: Updated `TERTIARY_MODELS` constant with 4 new entries

- [ ] **Step 1: Review current TERTIARY_MODELS**

Read the current `TERTIARY_MODELS` definition to understand the format and find insertion points.

- [ ] **Step 2: Add missing models**

Add these 4 models to `TERTIARY_MODELS["data"]`:
- `{"id": "gpt-5.6-terra", "object": "model"}`
- `{"id": "gpt-6-sol", "object": "model"}`
- `{"id": "mimo-v2.6-flash", "object": "model"}`
- `{"id": "jev-1.13.0", "object": "model"}`

Insert them in appropriate positions to maintain rough alphabetical grouping (gpt models together, mimo models together).

- [ ] **Step 3: Verify the change**

Check that:
- All 4 models are added
- JSON syntax is valid (proper commas, braces)
- No duplicate entries created

- [ ] **Step 4: Run unit tests**

```bash
py -m pytest tests/test_units.py -v -k "tertiary or Tertiary or openlux" --tb=short
```

Expected: All tests pass (tests verify provider routing works)

- [ ] **Step 5: Run integration tests**

```bash
py -m pytest tests/test_integration.py -v -k "tertiary or Tertiary or openlux or routing" --tb=short
```

Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add dashscope_proxy_lib/config.py
git commit -m "feat: add missing OpenLux models (gpt-5.6-terra, gpt-6-sol, mimo-v2.6-flash, jev-1.13.0)"
```

---

## Self-Review Checklist

- [ ] All 4 missing models from user image are added
- [ ] Models follow existing JSON format (`{"id": "...", "object": "model"}`)
- [ ] No syntax errors in config.py
- [ ] Tests pass after changes

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-09-27-openlux-models.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
