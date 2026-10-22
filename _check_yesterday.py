"""Summarise session logs for the last N UTC days.

Dates are DERIVED (UTC, matching the log rotation key) instead of hardcoded —
the previous version looped over fixed 2026-09-06/07 literals, so despite its
name it never actually checked "yesterday".

Usage: py _check_yesterday.py [days]   (default 2)
"""
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 2
SKIPPED = 0

for offset in range(DAYS):
    date = (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")
    print(f"\n=== {date} ===")
    errors = []
    total = 0
    retries = 0

    try:
        with open(f'session_logs/{date}.jsonl', encoding='utf-8') as f:
            for line in f:
                total += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # A kill mid-append leaves a truncated final line. Skipping
                    # it must stay VISIBLE, or the counts below silently skew.
                    SKIPPED += 1
                    continue
                if entry.get('retry_count', 0) > 0:
                    retries += 1
                if entry.get('status_code') != 200:
                    errors.append(entry)
    except FileNotFoundError:
        print("File not found")
        continue

    # An existing-but-empty day used to raise ZeroDivisionError and lose the
    # whole report.
    pct = (len(errors) / total * 100) if total else 0.0

    print(f"Total requests: {total}")
    print(f"Requests with retries: {retries}")
    print(f"Errors: {len(errors)} ({pct:.1f}%)")

    if errors:
        by_status = Counter(e.get('status_code') for e in errors)
        by_reason = Counter(e.get('error_reason', 'null') for e in errors)
        by_model = Counter(e.get('model', 'unknown') for e in errors)

        print(f"\nBy status: {dict(by_status)}")
        print(f"By reason: {dict(by_reason)}")
        print(f"By model: {dict(by_model)}")

        print("\nSample errors:")
        for e in errors[:10]:
            ts = e.get('timestamp_utc', '')[:19]
            print(f"  {ts} | {e.get('model','?'):20s} | {e.get('status_code')} | "
                  f"{e.get('error_reason','null')}")

if SKIPPED:
    print(f"\n(Skipped {SKIPPED} unparsable/corrupt line(s).)")