# Task 7: Update E2E Test Configuration

**Goal:** Remove quota keys from e2e test config.

**Files:**
- Modify: `tests/test_e2e_real.py`

## Exact Requirements

### e2e_config fixture (around line 50)
Remove quota keys:
```python
@pytest.fixture(scope="module")
def e2e_config(real_api_key):
    """Config for e2e tests."""
    return {
        "rpm_limit": 2400,
        "tpm_limit": 5_000_000,
        "safety_factor": 0.8,
        # Remove: "requests_per_5h": 6000,
        # Remove: "requests_per_week": 45000,
        # Remove: "requests_per_month": 90000,
        "max_queue_size": 200,
        "max_retries": 2,
        "base_backoff": 1.0,
    }
```

## Tests to Run

After making changes:
1. Run: `py -m pytest tests/test_units.py::TestSlidingWindowCounter -v`

Expected: Tests pass (config change only).

## Commit

After implementing:
```bash
git add tests/test_e2e_real.py
git commit -m "test(e2e): remove quota configuration from e2e tests"
```