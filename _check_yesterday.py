import json
from collections import Counter, defaultdict

# Check yesterday's logs too
for date in ['2026-09-06', '2026-09-07']:
    print(f"\n=== {date} ===")
    errors = []
    total = 0
    retries = 0
    
    try:
        with open(f'session_logs/{date}.jsonl') as f:
            for line in f:
                total += 1
                entry = json.loads(line)
                if entry.get('retry_count', 0) > 0:
                    retries += 1
                if entry['status_code'] != 200:
                    errors.append(entry)
    except FileNotFoundError:
        print("File not found")
        continue
    
    print(f"Total requests: {total}")
    print(f"Requests with retries: {retries}")
    print(f"Errors: {len(errors)} ({len(errors)/total*100:.1f}%)")
    
    if errors:
        by_status = Counter(e['status_code'] for e in errors)
        by_reason = Counter(e.get('error_reason', 'null') for e in errors)
        by_model = Counter(e.get('model', 'unknown') for e in errors)
        
        print(f"\nBy status: {dict(by_status)}")
        print(f"By reason: {dict(by_reason)}")
        print(f"By model: {dict(by_model)}")
        
        print("\nSample errors:")
        for e in errors[:10]:
            print(f"  {e['timestamp_utc'][:19]} | {e.get('model','?'):20s} | {e['status_code']} | {e.get('error_reason','null')}")
