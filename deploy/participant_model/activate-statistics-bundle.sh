#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run as root on VPS-1.'; exit 1; }
bundle=${1:?Usage: activate-statistics-bundle.sh /var/lib/doscbot-models/statistics-VERSION}
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python3 - "$bundle" "$source_dir" <<'PY'
import hashlib,importlib.util,json,sys
from pathlib import Path
from urllib.request import urlopen
bundle=Path(sys.argv[1]).resolve()
if not bundle.is_relative_to(Path('/var/lib/doscbot-models')):raise SystemExit('Bundle must be inside /var/lib/doscbot-models')
spec=importlib.util.spec_from_file_location('worker',Path(sys.argv[2])/'worker.py');worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker);worker.load_source()
from collector import statistics_contract as c
from collector.train_baseline import read_dataset
m=json.loads((bundle/'model/metrics.json').read_text())
manifest,_,_=read_dataset(bundle/'dataset',200000,all_available=True)
if (m.get('feature_profile')!=c.PROFILE or m['schema_sha256']!=worker.digest(c.SCHEMA) or
    json.loads((bundle/'model/feature-schema.json').read_text())!=c.SCHEMA or
    m['statistics_contract_sha256']!=hashlib.sha256(Path(c.__file__).read_bytes()).hexdigest() or
    m['dataset_rows_sha256']!=manifest['rows_sha256'] or
    m['model_sha256']!=hashlib.sha256((bundle/'model/model.cbm').read_bytes()).hexdigest()):
    raise SystemExit('Statistics model, schema or input checksums differ')
if m.get('decision_mode','price_gate.v1') not in ('primary_llm.v1','price_gate.v1'):raise SystemExit('Unknown decision mode')
if m.get('decision_mode')!='primary_llm.v1':c.validate_policy(m['decision_policy'])
with urlopen('http://127.0.0.1:18090/health',timeout=5) as response:health=json.load(response)
if health.get('state')!='ready' or health.get('model_sha256')!=m['model_sha256']:
    raise SystemExit('Deploy this statistics model on VPS-2 first; tunnel model differs')
if any(x in str(bundle) for x in ('\n','\r','"','\\')):raise SystemExit('Unsupported bundle path')
print('Statistics bundle and remote model: OK')
PY
systemctl stop prognozich-model-worker.timer prognozich-model-worker.service
python3 - "$bundle" <<'PY'
import os,sys
from pathlib import Path
from datetime import datetime,timezone
p=Path('/etc/prognozich-model-client/worker.env')
if p.exists():
    backup=p.with_name('worker.env.before-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.write_bytes(p.read_bytes());backup.chmod(0o600)
temporary=p.with_name('worker.env.new')
temporary.write_text('PARTICIPANT_MODEL_BUNDLE="'+str(Path(sys.argv[1]).resolve())+'"\n');temporary.chmod(0o600)
os.replace(temporary,p)
PY
bash "$source_dir/install-worker.sh"
