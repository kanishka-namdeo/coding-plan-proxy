import json
from collections import Counter

errors = []
total = 0
with open('session_logs/2026-09-07.jsonl') as f:
    for line in f:
        total += 1
        entry = json.loads(line)
        if entry['status_code'] != 200:
            errors.append(entry)

print(f"Total requests: {total}")
print(f"Total errors: {len(errors)}")
print()

# Group by error type
by_status = Counter()
by_reason = Counter()
by_model = Counter()
by_provider = Counter()

for e in errors:
    by_status[e['status_code']] += 1
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
    print(f"  {e['request_id']} | {e.get('model','?'):20s} | {e.get('provider','?'):10s} | "
          f"{e['status_code']} | {e.get('error_reason','null')} | retry={e['retry_count']} | "
          f"attempted={e.get('attempted_providers', [])} | "
          f"duration={e.get('duration_ms', 0):.0f}ms")
