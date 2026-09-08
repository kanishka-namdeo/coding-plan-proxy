import json

retries = []
with open('session_logs/2026-09-07.jsonl') as f:
    for line in f:
        entry = json.loads(line)
        if entry.get('retry_count', 0) > 0:
            retries.append(entry)

print(f"Requests with retries: {len(retries)}")
for r in retries[:20]:
    print(f"  {r['request_id']} | {r.get('model','?'):20s} | {r.get('provider','?'):10s} | "
          f"{r['status_code']} | retry={r['retry_count']} | {r.get('error_reason','null')}")
