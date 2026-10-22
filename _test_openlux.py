"""Probe a few tertiary (OpenLux) models through the running proxy.

Exits non-zero when any model fails, so an unattended run cannot report success
while measuring nothing. Error bodies are handled defensively: a gated upstream
can answer with a non-JSON body (the repo documents Cloudflare's `error code:
1010` HTML), and an unguarded json.loads there aborted the remaining probes.
"""
import json
import sys
import urllib.error
import urllib.request

url = 'http://127.0.0.1:8899/v1/chat/completions'
models = ['gemini-3.7-flash', 'gpt-5.6-terra', 'qwen3.8-max', 'gpt-5.6-sol']

failures = 0
for model in models:
    data = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': 'Say hello in one word'}],
        'max_tokens': 20
    }).encode()

    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            content = result['choices'][0]['message']['content'][:80]
            print(f'{model}: OK - {content}')
    except urllib.error.HTTPError as e:
        failures += 1
        try:
            body = json.loads(e.read())
            msg = body.get('error', {}).get('message', str(body))[:80]
        except (ValueError, UnicodeDecodeError, AttributeError):
            msg = 'non-JSON error body'
        print(f'{model}: HTTP {e.code} - {msg}')
    except Exception as e:
        failures += 1
        print(f'{model}: Error - {e}')

sys.exit(1 if failures else 0)