"""List session-log entries that needed at least one retry.

The date is derived (UTC, matching the log rotation key) instead of the
hardcoded 2026-09-07 literal that made this a one-day historical snapshot.
Corrupt lines are skipped with a visible count rather than aborting the report.

Usage: py _check_retries.py [YYYY-MM-DD]
"""
import json
import sys
from datetime import datetime, timedelta, timezone

date = sys.argv[1] if len(sys.argv) > 1 else (
    datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

retries = []
skipped = 0
try:
    with open(f'session_logs/{date}.jsonl', encoding='utf-8') as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if entry.get('retry_count', 0) > 0:
                retries.append(entry)
except FileNotFoundError:
    print(f"No session log for {date}")
    raise SystemExit(1)

print(f"{date}: requests with retries: {len(retries)}")
if skipped:
    print(f"(skipped {skipped} unparsable/corrupt line(s))")
for r in retries[:20]:
    print(f"  {r.get('request_id')} | {r.get('model','?'):20s} | {r.get('provider','?'):10s} | "
          f"{r.get('status_code')} | retry={r.get('retry_count')} | {r.get('error_reason','null')}")