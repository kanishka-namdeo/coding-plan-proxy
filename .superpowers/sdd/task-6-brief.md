# Task 6: Update Documentation

**Goal:** Remove quota configuration and behavior descriptions from documentation.

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `README_SCRIPT.md`
- Modify: `dashscope_proxy_lib/AGENTS.md`

## Exact Requirements

### .env.example
Remove all lines containing:
- `REQUESTS_PER_5H`
- `REQUESTS_PER_WEEK`
- `REQUESTS_PER_MONTH`
- `QUOTA_RETRY_COOLDOWN`
- `QUOTA_MAX_RETRIES`

### README.md
1. Remove the quota configuration table entries:
```markdown
| `requests_per_5h` | 6000 | Rolling 5-hour request cap |
| `requests_per_week` | 45000 | Weekly request cap |
| `requests_per_month` | 90000 | Monthly request cap |
```

2. Update the rate limiting feature description:
```markdown
**Multi-layer rate limiting**
Enforces RPS, RPM, and TPM (via Token Bucket). A configurable safety factor keeps usage below the hard limits.
```

### README_SCRIPT.md
Remove quota references from TUI feature description.

### dashscope_proxy_lib/AGENTS.md
Update the `RateLimiter` description:
```markdown
- `RateLimiter` — combines sliding window + token bucket + circuit breaker
```

## Tests to Run

After making changes:
1. Run: `py -m pytest tests/test_units.py::TestSlidingWindowCounter -v`

Expected: Tests pass (documentation changes don't affect code).

## Commit

After implementing:
```bash
git add .env.example README.md README_SCRIPT.md dashscope_proxy_lib/AGENTS.md
git commit -m "docs: remove quota configuration and behavior descriptions"
```