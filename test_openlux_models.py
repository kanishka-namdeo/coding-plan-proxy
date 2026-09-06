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

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
        models = result.get('data', [])
        print(f'Total models: {len(models)}')
        print('\nAll models:')
        for model in models:
            print(f'  - {model["id"]}')
except urllib.error.HTTPError as e:
    print(f'HTTP Error: {e.code}')
    print(f'Body: {e.read().decode()}')
except Exception as e:
    print(f'Error: {e}')
