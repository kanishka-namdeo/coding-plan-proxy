import urllib.request
import json
import os
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv('OPENLUX_API_KEY')

url = 'https://api.openlux.ai/v1/models'
req = urllib.request.Request(url, headers={
    'Authorization': f'Bearer {api_key}',
    'Content-Type': 'application/json'
})

with urllib.request.urlopen(req, timeout=10) as resp:
    result = json.loads(resp.read())
    models = result.get('data', [])
    
    # Filter for GPT and Claude models
    gpt_models = [m['id'] for m in models if 'gpt' in m['id'].lower() and '5.6' in m['id']]
    claude_models = [m['id'] for m in models if 'claude' in m['id'].lower()]
    
    print('GPT 5.6 models:')
    for model in sorted(gpt_models):
        print(f'  - {model}')
    
    print('\nClaude models (first 20):')
    for model in sorted(claude_models)[:20]:
        print(f'  - {model}')
