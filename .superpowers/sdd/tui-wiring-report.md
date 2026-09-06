# TUI Server Wiring — Implementation Report

Branch: `feat/tui-server-wiring`
Workspace: `C:\Users\kanis\Desktop\alibaba_proxy`
Plan: `C:\Users\kanis\.cursor\plans\tui_server_wiring_01cb3ee1.plan.md` (not edited)

## What was implemented

All 8 plan tasks:

1. Pure helpers in `tui_status.py` (`is_multi_provider`, `series_from_status`, `provider_section_visible`, `failover_alert_should_show`, `overview_request_stats`) plus `TestTuiStatusHelpers`.
2. `max_queue_size` exported on `RateLimiter.status()` and `MultiProviderRateLimiter.status()`.
3. `_poll_loop` samples envelope series (global pending + aggregated latencies) and adaptive interval uses `series["queue_depth"]`.
4. Configured provider sections stay visible at zero forwards (`Configured (idle)`); `status-running` only when `total_forwarded > 0`; circuit-open prefix retained.
5. Failover ALERT is no longer gated on other warnings; Overview adds Rejected, Pending `n/max`, Success Rate.
6. `_load_display_config()` added; `_load_config()` shape unchanged. TUI Config tab uses `_config_rows`. Facade re-exports `_load_display_config`.
7. Logs tab honors `Log.auto_scroll` from `_autoscroll_enabled`. Removed `_fmt_time_delta`, `LatencyTracker.add_batch`, `self._active_alerts`.
8. README + root/`tests`/`dashscope_proxy_lib` DOX updated. Full unit + integration suite run (no e2e, no screenshot recapture).

## Commits (short SHA + subject) per task

| Task | SHA | Subject |
|------|-----|---------|
| 1 | `642e497` | feat(tui): add pure status helpers for multi-provider poller |
| 2 | `5c8a836` | feat(limiter): export max_queue_size on status snapshots |
| — | `8f6b13d` | feat: TUI server wiring and config updates *(not from this plan; see Concerns)* |
| 3 | `1460787` | fix(tui): sample global queue and aggregated latencies |
| 4 | `8d33c36` | fix(tui): show configured providers before first forward |
| 5 | `989f6c6` | fix(tui): ungate failover alerts and show overview success rate |
| 6 | `d45e80e` | feat(tui): show network, logging, and per-provider limits in Config |
| 7 | `7e48261` | fix(tui): honor auto-scroll and remove dead helpers |
| 8 | `9f9e7ba` | docs: align TUI README and DOX with multi-provider wiring |

## TDD evidence

### Task 1
- RED: `python -m pytest tests/test_units.py::TestTuiStatusHelpers -v` → 7 failed, `ModuleNotFoundError: No module named 'tui_status'`
- GREEN: same command → **7 passed** after adding `tui_status.py`

### Task 2
- RED: `python -m pytest tests/test_units.py::TestMultiProviderRateLimiter::test_status_includes_global_pending_and_max_queue -v` → `KeyError: 'max_queue_size'`
- GREEN: same test passed after adding the keys. (`TestRateLimiter` class does not exist; used `TestRateLimiterCanProceed` / `TestRateLimiterTokenManagement` / `TestCircuitBreaker` instead.)

### Task 6
- RED: `python -m pytest tests/test_units.py::TestLoadDisplayConfig -v` → `AttributeError: module 'dashscope_proxy' has no attribute '_load_display_config'`
- GREEN: `python -m pytest tests/test_units.py::TestLoadConfig tests/test_units.py::TestLoadDisplayConfig -v` → **4 passed** (`_load_config` tests unchanged)

## Test commands and results

- `python -m pytest tests/test_units.py::TestTuiStatusHelpers -v` — 7 passed (Task 1 GREEN)
- `python -m pytest tests/test_units.py::TestMultiProviderRateLimiter::test_status_includes_global_pending_and_max_queue -v` — passed (Task 2 GREEN)
- `python -m py_compile proxy_tui.py` — OK (Tasks 3–7)
- `python -m pytest tests/test_units.py::TestLoadConfig tests/test_units.py::TestLoadDisplayConfig -v` — 4 passed (Task 6 GREEN)
- Task 7: `TestTuiStatusHelpers` + `TestLoadDisplayConfig` + `test_status_includes_global_pending_and_max_queue` — 9 passed
- **Full suite:** `python -m pytest tests/test_units.py tests/test_integration.py -v` → **278 passed**, 1 pre-existing warning (`TestUnserializableBody` unawaited coroutine). e2e not run.

Interpreter: `python` (Windows PATH; `py` launcher not available).

## Files changed (this plan)

- Created: `tui_status.py`
- Modified: `tests/test_units.py`, `tests/AGENTS.md`, `dashscope_proxy_lib/rate_limiter.py`, `proxy_tui.py`, `dashscope_proxy_lib/config.py`, `dashscope_proxy.py`, `README.md`, `AGENTS.md`, `dashscope_proxy_lib/AGENTS.md`

## Concerns

1. **Extra commit `8f6b13d`** (`feat: TUI server wiring and config updates`) appeared on `feat/tui-server-wiring` between Tasks 2 and 3. It is not one of the plan’s per-task commits. It bundled previously uncommitted local work (`.env.example`, README, `rate_limiter.py` quinary/senary constructor args, `server.py`, OpenLux scripts, `.superpowers/` scratch, `battery-report.html`, etc.). Origin of that commit is outside this implementer’s intended `git add`/`commit` set.

2. **Task 3 briefly landed on `master`** (`316755b`) after a checkout mix-up; it was cherry-picked onto `feat/tui-server-wiring` as `1460787` and `master` was reset to `8f6b13d`. Confirm `master` is where you expect it (`origin/master` was already ahead 42 at start of this work).

3. **`_load_display_config` network/timeout values** read `os.environ.get(...)` so monkeypatched env in tests is visible without process restart. Module-level `CODING_PLAN_CONFIG` / `*_CODING_PLAN_CONFIG` values are still import-time snapshots (same as `_load_config`).

4. Plan asked for `py -m pytest`; this environment has `python` only.

5. `dashscope_proxy_lib/AGENTS.md` was updated in Task 8 (not listed in the plan file list) because `_load_display_config` is a config-module contract.
