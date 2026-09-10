# Task 9 Report — Wave 5 Admission-time RPS shaping

**Status:** DONE  
**Branch:** `perf/measure-first-hardening`

## Commits

- `60f00ea` — `perf: apply RPS spacing at admission time`
- `4cbf69e` — docs/tests: probe herd `rps_limit` + DOX

## Deliverables

| Item | Path | Notes |
| --- | --- | --- |
| Admit stamp | `rate_limiter.py` | `try_admit` sets `last_request_time` under asyncio lock |
| Cross-lock | same | `record_request` / `record_completion` write it under asyncio lock |
| Units | `test_units.py` | Admit-time RPS test; status hammer raises `rps_limit` |
| Integration | `test_integration.py` | Probe herd raises `rps_limit` |
| DOX | root/lib/tests `AGENTS.md` | Admission-time RPS + lock ownership |

## Tests

- TDD RED→GREEN on admit stamp; integration **74 passed**
