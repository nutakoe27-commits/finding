"""Compare current preparation with exported feature rows using read-only samples.

No inference, training, result queries or database writes. Imports only the
explicitly supplied collector Python package. A match is evidence
on this dataset, not proof of general equivalence or future model quality.
"""
import sys
sys.dont_write_bytecode = True

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def audit(bundle, code, database):
    bundle, code, database = map(Path, (bundle, code, database))
    if not database.is_file() or database.is_symlink():
        raise ValueError('collector_database_missing_or_symlink')
    metrics = json.loads((bundle / 'model/metrics.json').read_text())
    schema = json.loads((bundle / 'model/feature-schema.json').read_text())
    manifest = json.loads((bundle / 'dataset/manifest.json').read_text())
    raw = (bundle / 'dataset/rows.jsonl').read_bytes()
    if (hashlib.sha256(raw).hexdigest() != metrics['dataset_rows_sha256'] or
            hashlib.sha256(raw).hexdigest() != manifest['rows_sha256'] or
            digest(schema) != metrics['schema_sha256']):
        raise ValueError('export_checksum_mismatch')
    code_hash = digest({n: hashlib.sha256((code / 'collector' / (n+'.py')).read_bytes()).hexdigest()
                        for n in ('training', 'training_evidence', 'training_diagnostics',
                                  'profiles', 'store', 'outcomes', 'monitor_data')})
    if code_hash != metrics['preparation_code_sha256']:
        raise ValueError('preparation_revision_mismatch')
    sys.path.insert(0, str(code.resolve()))
    training = importlib.import_module('collector.training')
    store = importlib.import_module('collector.store')
    if Path(training.__file__).resolve() != (code/'collector/training.py').resolve():
        raise ValueError('unexpected_collector_module')
    if training.SCHEMA != schema:
        raise ValueError('current_schema_differs')
    rows = [json.loads(line) for line in raw.splitlines()]
    if len(rows) != manifest['export_rows']:
        raise ValueError('export_row_count_mismatch')
    groups = {}
    for row in rows:
        if row.get('format') != 'doscbot.participant-training-row.v1':
            raise ValueError('unexpected_export_format')
        groups.setdefault(row['sample_id'], []).append(row)
    checked = matched = 0
    failures = Counter()
    details = []

    def reject(sample_id, reason, fields=None):
        failures[reason] += 1
        if len(details) < 20:
            details.append({'sample_id': sample_id, 'reason': reason, 'fields': fields or []})

    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        owner = db.execute("SELECT value FROM metadata WHERE key='owner'").fetchone()
        if not owner or owner[0] != 'doscbot.fresh-collector.v1':
            raise ValueError('not_the_new_collector_database')
        for sample_id, expected in groups.items():
            row = db.execute('SELECT id,event_id,as_of,kickoff_at,sha256,payload FROM samples WHERE id=?', (sample_id,)).fetchone()
            if row is None:
                reject(sample_id, 'sample_missing'); continue
            try:
                payload = json.loads(row['payload'])
                event, widget = payload['event'], payload['widget_data']
                if (digest(payload) != row['sha256'] or
                        any(r['sample_sha256'] != row['sha256'] or str(r['event_group']) != row['event_id'] or
                            store.instant(r['as_of']) != store.instant(row['as_of']) or
                            store.instant(r['kickoff_at']) != store.instant(row['kickoff_at']) for r in expected) or
                        str(event['source_event_id']) != row['event_id'] or
                        store.instant(payload['as_of']) != store.instant(row['as_of']) or
                        store.instant(event['kickoff_at']) != store.instant(row['kickoff_at'])):
                    reject(sample_id, 'sample_identity_or_checksum_mismatch'); continue
                if payload.get('format') != 'doscbot.source-input.v2':
                    reject(sample_id, 'unsupported_sample_format'); continue
                reason = store.sample_blocker(event, payload['team_statistics'], row['as_of'])
                reason = reason or ('widget_missing' if not widget else store.widget_blocker(event, payload['team_statistics'], widget))
                if reason:
                    reject(sample_id, 'current_eligibility_rejected:'+reason); continue
                store.check_observation_times(payload, store.instant(row['as_of']))
                if (store.instant(row['kickoff_at'])-store.instant(row['as_of'])).total_seconds() < manifest['lead_minutes']*60:
                    reject(sample_id, 'horizon_mismatch'); continue
                if len(expected) != 2 or {r['features']['selection_side'] for r in expected} != {'home', 'away'}:
                    reject(sample_id, 'invalid_export_pair'); continue
                market, _ = training.winner_market(event)
                actual = dict(training.participant_rows(payload, market))
                checked += 1
                differing = set()
                for r in expected:
                    side = r['features']['selection_side']
                    generated = actual[side]
                    for key in set(generated) | set(r['features']):
                        if key not in generated or key not in r['features'] or canonical(generated[key]) != canonical(r['features'][key]):
                            differing.add(key)
                    if canonical(market) != canonical(r.get('market')):
                        differing.add('market')
                if differing:
                    reject(sample_id, 'feature_or_market_mismatch', sorted(differing))
                else:
                    matched += 1
            except (KeyError, ValueError, TypeError, OverflowError):
                reject(sample_id, 'invalid_sample_or_current_preparation')
    return {'state': 'all_exported_features_match' if not failures and matched == len(groups) else 'not_verified',
            'export_rows': len(rows), 'export_events': len(groups), 'compared_events': checked,
            'matched_events': matched, 'failures': dict(failures), 'examples': details,
            'expected_preparation_sha256': metrics['preparation_code_sha256'],
            'current_preparation_sha256': code_hash,
            'original_code_hash_matches': code_hash == metrics['preparation_code_sha256'],
            'database_access': 'read_only_samples', 'outcomes_queried': False,
            'model_calls': 0, 'future_quality_evaluated': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', default='/var/lib/doscbot-models/all-20261115-1')
    parser.add_argument('--code', default='/opt/doscbot-collector/app')
    parser.add_argument('--database', default='/var/lib/doscbot-collector/data/dataset.sqlite3')
    args = parser.parse_args()
    try:
        result = audit(args.bundle, args.code, args.database)
    except Exception as error:
        # Avoid printing database contents, configuration or exception messages.
        result = {'state': 'audit_failed', 'error_type': type(error).__name__,
                  'next_step': 'Check code path, collector dependencies and bundle/database availability'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result['state'] == 'all_exported_features_match' else 2)
