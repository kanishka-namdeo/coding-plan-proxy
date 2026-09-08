# Task 8: Update Screenshot Capture Mock Status

**Goal:** Remove quota fields from mock status in screenshot capture script.

**Files:**
- Modify: `capture_screenshots.py`

## Exact Requirements

### _default_status() method (around line 35)
Remove quota fields:
```python
    def _default_status(self) -> dict:
        return {
            "rps_limit": 0, "rpm_limit": 0, "rpm_current": 0,
            "tpm_limit": 0, "tpm_available": 0, "tpm_reserved": 0,
            # Remove: "requests_5h": 0, "requests_5h_limit": 0,
            # Remove: "requests_week": 0, "requests_week_limit": 0,
            # Remove: "requests_month": 0, "requests_month_limit": 0,
            "total_forwarded": 0, "queue_drops": 0, "queue_p50_ms": 0, "queue_p95_ms": 0, "queue_p99_ms": 0, "total_429s": 0,
            "total_rejected": 0, "total_tokens_consumed": 0,
            "pending_requests": 0, "recent_latencies": [],
            "model_usage": {}, "uptime_seconds": 0,
            "circuit_open": False, "circuit_failure_count": 0,
        }
```

## Tests to Run

After making changes:
1. Run: `py -m pytest tests/test_units.py::TestSlidingWindowCounter -v`

Expected: Tests pass (mock status only).

## Commit

After implementing:
```bash
git add capture_screenshots.py
git commit -m "refactor(screenshots): remove quota fields from mock status"
```