"""Transport/compatibility check only, not evaluation on unseen matches."""
import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

p = argparse.ArgumentParser()
p.add_argument('--bundle', default='/var/lib/doscbot-models/all-20261115-1')
args = p.parse_args()
bundle = Path(args.bundle)
metrics = json.loads((bundle / 'model/metrics.json').read_text())
with (bundle / 'dataset/rows.jsonl').open() as f:
    features = json.loads(next(f))['features']
token = Path('/etc/prognozich-model-client/token').read_text().strip()
request = Request('http://127.0.0.1:18090/predict',
    data=json.dumps({'model_sha256': metrics['model_sha256'], 'features': features}).encode(),
    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
with urlopen(request, timeout=15) as response:
    value = json.load(response)
if value['model_sha256'] != metrics['model_sha256']:
    raise SystemExit('Unexpected model version')
print(json.dumps({'state': 'compatibility_check_ok', 'new_match_evaluation': False, 'result': value}, ensure_ascii=False, indent=2))
