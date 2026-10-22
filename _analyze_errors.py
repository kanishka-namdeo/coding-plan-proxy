"""Group a session log's non-200 entries by status / reason / model / provider.

The date is derived (UTC, matching the log rotation key) instead of the
hardcoded 2026-09-07 literal. Corrupt lines are skipped with a visible count —
a kill mid-append leaves a truncated final line, and one such line previously
aborted the whole report.

Usage: py _analyze_errors.py [YYYY-MM-DD]
"""
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

date = sys.argv[1] if len(sys.argv) > 1 else (
    datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

errors = []
total = 0
skipped = 0
try:
    with open(f'session_logs/{date}.jsonl', encoding='utf-8') as f:
        for line in f:
            total += 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if entry.get('status_code') != 200:
                errors.append(entry)
except FileNotFoundError:
    print(f"No session log for {date}")
    raise SystemExit(1)

print(f"Date: {date}")
print(f"Total requests: {total}")
print(f"Total errors: {len(errors)}")
if skipped:
    print(f"(skipped {skipped} unparsable/corrupt line(s))")
print()

# Group by error type
by_status = Counter()
by_reason = Counter()
by_model = Counter()
by_provider = Counter()

for e in errors:
    by_status[e.get('status_code')] += 1
    by_reason[e.get('error_reason', 'null')] += 1
    by_model[e.get('model', 'unknown')] += 1
    by_provider[e.get('provider', 'unknown')] += 1

print("=== By status code ===")
for k, v in by_status.most_common():
    print(f"  {k}: {v}")

print("\n=== By error_reason ===")
for k, v in by_reason.most_common():
    print(f"  {k}: {v}")

print("\n=== By model ===")
for k, v in by_model.most_common():
    print(f"  {k}: {v}")

print("\n=== By provider ===")
for k, v in by_provider.most_common():
    print(f"  {k}: {v}")

print("\n=== Detailed errors ===")
for e in errors[:30]:
    print(f"  {e.get('request_id')} | {e.get('model','?'):20s} | {e.get('provider','?'):10s} | "
          f"{e.get('status_code')} | {e.get('error_reason','null')} | retry={e.get('retry_count')} | "
          f"attempted={e.get('attempted_providers', [])} | "
          f"duration={e.get('duration_ms', 0):.0f}ms")