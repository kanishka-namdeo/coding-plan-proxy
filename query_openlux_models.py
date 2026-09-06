import urllib.request
import json
import os
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv('OPENLUX_API_KEY')
base_url = os.getenv('OPENLUX_TARGET_BASE', 'https://api.openlux.ai/v1')

# Models to find
target_models = ['gpt-5.6-sol', 'gemini-3.7-flash', 'gpt-5.6-terra', 'qwen3.8-max']

print('Querying full models list from OpenLux...\n')

try:
    url = f'{base_url}/models'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {api_key}'})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode())
        
        # Look for our target models
        models_list = data.get('data', [])
        print(f'Total models available: {len(models_list)}\n')
        
        found_models = []
        for model in models_list:
            model_id = model.get('id')
            if model_id in target_models:
                found_models.append(model)
                print(f'Found: {model_id}')
                print(json.dumps(model, indent=2))
                print()
        
        # Check if any model has pricing info
        if models_list:
            sample = models_list[0]
            print('Sample model structure:')
            print(json.dumps(sample, indent=2))
            
        if not found_models:
            print('\nNone of the target models were found in the list.')
            
except urllib.error.HTTPError as e:
    print(f'HTTP Error: {e.code}')
    print(f'Response: {e.read().decode()[:500]}')
except Exception as e:
    print(f'Error: {e}')
