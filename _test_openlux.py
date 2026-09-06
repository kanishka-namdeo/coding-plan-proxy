import urllib.request, json

url = 'http://127.0.0.1:8899/v1/chat/completions'
models = ['gemini-3.7-flash', 'gpt-5.6-terra', 'qwen3.8-max', 'gpt-5.6-sol']

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
        body = json.loads(e.read())
        msg = body.get('error', {}).get('message', str(body))[:80]
        print(f'{model}: HTTP {e.code} - {msg}')
    except Exception as e:
        print(f'{model}: Error - {e}')
