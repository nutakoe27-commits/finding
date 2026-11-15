"""Prospective admin experiment. Collector DB is read-only; results live separately."""
import sys
sys.dont_write_bytecode = True
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from urllib.request import Request, urlopen


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def error_details(error):
    """Diagnostics without SQL, parameters, payloads, paths or credentials."""
    result = {'error_type': type(error).__name__}
    if isinstance(error, sqlite3.Error):
        result.update(sqlite_code=getattr(error, 'sqlite_errorcode', None),
                      sqlite_name=getattr(error, 'sqlite_errorname', None))
    return result


def load_source():
    root = Path(__file__).resolve().parent/'vendor'
    for name, expected in json.loads((root/'SHA256.json').read_text()).items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != expected:
            raise ValueError('Pinned source changed')
    sys.path.insert(0, str(root))
    from collector import model_evaluation as evaluation, training
    return evaluation, training


def atomic_report(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(canonical(value))
    temporary.chmod(0o644)
    os.replace(temporary, path)


def read_packets(directory):
    result = []
    for path in sorted(directory.glob('*.json')):
        envelope = json.loads(path.read_text())
        if digest(envelope['data']) != envelope['sha256']:
            raise ValueError('Frozen forecast checksum mismatch')
        result.append(envelope['data'])
    return result


def current_catalog(database, db, cutoff):
    """Read latest presence, not change-history timestamps, from the owned archive."""
    from collector.monitor_data import readonly
    from collector.store import instant
    path = database.parent/'source/winline.sqlite3'
    if not path.is_file():
        return None, {'state': 'not_available'}
    owner = db.execute("SELECT value FROM metadata WHERE key='source_archive'").fetchone()
    if (path.is_symlink() or not path.resolve().is_relative_to(database.parent.resolve()) or
            not owner or owner[0] != str(path.resolve())):
        return {}, {'state': 'unverified'}
    with closing(readonly(path)) as source:
        snapshot = source.execute('SELECT id,observed_at FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
        if not snapshot:
            return {}, {'state': 'empty'}
        info = {'state': 'ready', 'observed_at': snapshot['observed_at'], 'snapshot_id': snapshot['id']}
        age = (cutoff-instant(snapshot['observed_at'])).total_seconds()
        if not 0 <= age <= 300:
            return {}, {**info, 'state': 'stale_or_future'}
        result = {}
        for row in source.execute('SELECT id,last_seen,raw_json,content_hash FROM events WHERE snapshot_id=?', (snapshot['id'],)):
            if (instant(row['last_seen']) != instant(snapshot['observed_at']) or
                    hashlib.sha256(row['raw_json'].encode()).hexdigest() != row['content_hash']):
                continue
            result[str(row['id'])] = (json.loads(row['raw_json']), row['content_hash'])
        return result, info


def remote_predict(features, model_sha, schema_sha, token):
    request = Request('http://127.0.0.1:18090/predict',
        data=canonical({'features': features, 'model_sha256': model_sha}),
        headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'}, method='POST')
    with urlopen(request, timeout=15) as response:
        value = json.loads(response.read(65537))
    p = value.get('probability')
    if (value.get('format') != 'prognozich.participant-prediction.v1' or
            value.get('target') != 'participant_win.v1' or value.get('model_sha256') != model_sha or
            value.get('schema_sha256') != schema_sha or value.get('features_sha256') != digest(features) or
            any(value.get(k) != features[k] for k in ('sport', 'selection_side', 'market_scope')) or
            type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1):
        raise ValueError('Inference response does not match input/model')
    return p


def cycle(bundle, database, state, token_file, *, predict=remote_predict):
    ev, tr = load_source()
    from collector.monitor_data import readonly
    from collector.store import instant
    from collector.train_baseline import read_dataset, evaluate
    from collector.training_evidence import native_at, participant_key, identifier
    from collector import statistics_contract as stats
    current = datetime.now(timezone.utc)
    metrics = json.loads((bundle/'model/metrics.json').read_text())
    manifest, names, folds = read_dataset(bundle/'dataset', 200000, all_available=True)
    statistics_only = metrics.get('feature_profile') == stats.PROFILE
    expected_schema = stats.SCHEMA if statistics_only else tr.SCHEMA
    training_rows = folds['all']
    if statistics_only:
        if metrics.get('statistics_contract_sha256') != hashlib.sha256(Path(stats.__file__).read_bytes()).hexdigest():
            raise ValueError('Statistics contract changed')
        stats.validate_policy(metrics['decision_policy'])
        training_rows = [r for r in training_rows if stats.supported(stats.project({k:None if type(v) is float and math.isnan(v) else v for k,v in zip(names,r['x'])}))]
    code_hash = digest({n:hashlib.sha256((Path(tr.__file__).parent/(n+'.py')).read_bytes()).hexdigest()
        for n in ('training','training_evidence','training_diagnostics','profiles','store','outcomes','monitor_data')})
    if (metrics.get('target') != 'participant_win.v1' or metrics['schema_sha256'] != digest(expected_schema) or
            json.loads((bundle/'model/feature-schema.json').read_text()) != expected_schema or
            metrics['dataset_rows_sha256'] != manifest['rows_sha256'] or
            metrics['preparation_code_sha256'] != code_hash or
            hashlib.sha256((bundle/'model/model.cbm').read_bytes()).hexdigest() != metrics['model_sha256']):
        raise ValueError('Model bundle or preparation mismatch')
    trained = {r['group']: r for r in training_rows}
    if len(trained) != metrics['training_events'] or len(training_rows) != metrics['training_rows']:
        raise ValueError('Training population mismatch')
    ready = instant(metrics['finished_at'])
    if ready > current or ready < instant(manifest['checked_at']):
        raise ValueError('Model readiness time mismatch')
    lead = timedelta(minutes=manifest['lead_minutes'])
    experiment = metrics['model_sha256'] + ('-stats-'+digest(metrics['decision_policy'])[:12] if statistics_only else '')
    directory = state/'frozen'/experiment
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    packets = read_packets(directory)
    seen = {p['packet']['event']['source_event_id'] for p in packets}
    seen_statistics = {p['statistics_id'] for p in packets}
    skipped = Counter()
    candidates = []
    # No writes or schema initialization on the collector database.
    if database.is_symlink():
        raise ValueError('Redirected collector database')
    with closing(readonly(database)) as db:
        if db.execute("SELECT value FROM metadata WHERE key='owner'").fetchone()[0] != 'doscbot.fresh-collector.v1':
            raise ValueError('Wrong collector database')
        protected = ev.training_statistics_ids(db, trained)
        current = datetime.now(timezone.utc)
        catalogue, catalogue_info = current_catalog(database, db, current)
        rows = db.execute('''WITH ranked AS (
            SELECT id,row_number() OVER(PARTITION BY event_id ORDER BY julianday(as_of) DESC,id DESC) n
            FROM samples WHERE julianday(as_of)>=julianday(?) AND julianday(as_of)<=julianday(?)
            AND julianday(kickoff_at)>=julianday(?))
            SELECT s.* FROM samples s JOIN ranked r ON s.id=r.id WHERE r.n=1
            ORDER BY julianday(s.kickoff_at),s.event_id LIMIT 2000''',
            (ready.isoformat(), current.isoformat(), (current+lead).isoformat()))
        for row in rows:
            if row['event_id'] in trained or row['event_id'] in seen:
                skipped['already_trained_or_recorded'] += 1; continue
            try:
                payload = ev.validated_sample(row)
                e, widget = payload['event'], payload['widget_data']
                sid = str(widget['statistics_id'])
                if sid in protected or sid in seen_statistics:
                    skipped['duplicate_or_trained_statistics'] += 1; continue
                if e['sport'] not in (metrics['sports'] if statistics_only else manifest['requested_sports']):
                    skipped['unsupported_sport'] += 1; continue
                if instant(e['kickoff_at'])-instant(payload['as_of']) < lead:
                    skipped['sample_horizon'] += 1; continue
                market, reason = tr.winner_market(e)
                if reason:
                    skipped[reason] += 1; continue
                if catalogue is not None:
                    if catalogue_info['state'] != 'ready':
                        skipped['current_catalog_'+catalogue_info['state']] += 1; continue
                    entry = catalogue.get(e['source_event_id'])
                    if not entry:
                        skipped['not_in_verified_current_catalog'] += 1; continue
                    value, source_sha = entry
                    evidence = {'kind': 'current_catalog', **catalogue_info, 'event_sha256': source_sha}
                else:
                    native = native_at(db, e['source_event_id'], current)
                    if not native or (current-instant(native[0]['observed_at'])).total_seconds() > 300:
                        skipped['current_schedule_stale_or_missing'] += 1; continue
                    value = native[1]
                    evidence = {'kind': 'event_observation', 'observed_at': native[0]['observed_at'],
                                'event_sha256': native[0]['sha256']}
                if (value.get('isLive') not in (False,0,None) or type(value.get('idSport')) is not int or
                        tr.NATIVE_SPORTS.get(value['idSport']) != e['sport'] or str(value.get('id')) != e['source_event_id'] or
                        [participant_key(n) for n in value.get('members',[])] != [participant_key(e[n]) for n in ('home','away')] or
                        abs((instant(value['date'])-instant(e['kickoff_at'])).total_seconds()) > 60 or
                        identifier(value.get('idRadar')) not in (None, identifier(widget['statistics_id']))):
                    skipped['schedule_or_identity_changed'] += 1; continue
                if db.execute("SELECT 1 FROM observations WHERE kind='event_outcome' AND entity_id=? LIMIT 1", (e['source_event_id'],)).fetchone():
                    skipped['outcome_already_exists'] += 1; continue
                features = [{'side':side,'features':stats.project(f) if statistics_only else f}
                    for side,f in tr.participant_rows(payload,{**market,'prices':{}} if statistics_only else market)]
                if statistics_only and not all(stats.supported(f['features']) for f in features):
                    skipped['insufficient_participant_statistics'] += 1; continue
                for f in features:
                    if statistics_only: stats.vector(f['features'])
                    else: ev.vector(f['features'],names)
                candidates.append({'statistics_id':sid, 'schedule_evidence':evidence, 'packet':{'format':'doscbot.frozen-forecast.v1',
                    'event':{k:e.get(k) for k in ('source_event_id','sport','home','away','competition','kickoff_at')},
                    'sample_sha256':row['sha256'],'sample_as_of':row['as_of'],
                    'model_sha256':metrics['model_sha256'],'market':market,'rows':features}})
                seen_statistics.add(sid)
                if len(candidates) >= 10: break
            except (KeyError,TypeError,ValueError,OverflowError):
                skipped['invalid_sample'] += 1
    # End the collector read transaction before network calls.
    token = token_file.read_text().strip()
    inference_error = None
    for item in candidates:
        packet = item['packet']
        if (datetime.now(timezone.utc)-instant(item['schedule_evidence']['observed_at'])).total_seconds() > 300:
            skipped['schedule_expired_before_inference'] += 1; continue
        try:
            for row in packet['rows']:
                row['probability'] = predict(row['features'],metrics['model_sha256'],metrics['schema_sha256'],token)
        except Exception as error:
            inference_error = type(error).__name__; break
        issued = datetime.now(timezone.utc)
        if issued < current or instant(packet['event']['kickoff_at'])-issued < lead:
            skipped['horizon_passed_during_inference'] += 1; continue
        if (issued-instant(item['schedule_evidence']['observed_at'])).total_seconds() > 300:
            skipped['schedule_expired_during_inference'] += 1; continue
        packet['issued_at'] = issued.isoformat()
        if statistics_only:
            item['variants'] = stats.decide({r['side']:r['probability'] for r in packet['rows']},packet['market']['prices'],metrics['decision_policy'])
        item['header'] = {'mode':'prospective','model_sha256':metrics['model_sha256'],
            'model_ready_at':ready.isoformat(),'lead_minutes':manifest['lead_minutes'],
            'sealed_at':issued.isoformat(),'requested_sports':manifest['requested_sports']}
        target = directory/(digest({'event':packet['event']['source_event_id']})+'.json')
        # One frozen answer per event/model. Atomic publish after fsync.
        temporary = target.with_suffix('.tmp')
        with temporary.open('wb') as f:
            f.write(canonical({'sha256':digest(item),'data':item}));f.flush();os.fsync(f.fileno())
        sealed = datetime.now(timezone.utc)
        if sealed < issued or instant(packet['event']['kickoff_at'])-sealed < lead:
            temporary.unlink();skipped['horizon_passed_while_saving'] += 1;continue
        # Record the time just before publication, without extending the horizon.
        item['header']['sealed_at'] = sealed.isoformat()
        with temporary.open('wb') as f:
            f.write(canonical({'sha256':digest(item),'data':item}));f.flush();os.fsync(f.fileno())
        if target.exists(): raise ValueError('Duplicate frozen event')
        os.replace(temporary,target)
        published = datetime.now(timezone.utc)
        if published < sealed or instant(packet['event']['kickoff_at'])-published < lead:
            target.unlink();skipped['horizon_passed_while_saving'] += 1;continue
        fd = os.open(directory, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        packets.append(item)
    checked = datetime.now(timezone.utc)
    scored, probabilities, matches = [], [], []
    unscored = Counter()
    with closing(readonly(database)) as db:
        for item in packets:
            p = item['packet']
            try:
                scoring_packet = p
                if statistics_only:
                    source = db.execute('SELECT * FROM samples WHERE event_id=? AND sha256=? ORDER BY id LIMIT 1',
                        (p['event']['source_event_id'],p['sample_sha256'])).fetchone()
                    payload = ev.validated_sample(source)
                    full_rows = list(tr.participant_rows(payload,p['market']))
                    if len(p['rows'])!=2 or any(record['side']!=side or record['features']!=stats.project(f) for (side,f),record in zip(full_rows,p['rows'])):
                        raise ValueError('Frozen statistics features changed')
                    if item.get('variants') != stats.decide({r['side']:r['probability'] for r in p['rows']},p['market']['prices'],metrics['decision_policy']):
                        raise ValueError('Frozen decision policy changed')
                    # Full legacy features are reconstructed ONLY for evidence replay, never sent to CatBoost.
                    scoring_packet = {**p,'rows':[{**record,'features':full} for (_,full),record in zip(full_rows,p['rows'])]}
                score = ev.score_packets(db,[scoring_packet],item['header'],checked)
                outcome = score['outcomes'][0]
                winner, reason = outcome['winner'], outcome['reason']
            except (ValueError,KeyError,TypeError,OverflowError):
                winner, reason = None, 'frozen_input_validation_failed'
            if winner:
                for row in p['rows']:
                    scored.append({'y':int(winner==row['side']),'sport':p['event']['sport'],
                        'group':p['event']['source_event_id'],'quality':row['features']['capture_state'],
                        'baseline':(1/p['market']['prices'][row['side']])/sum(1/v for v in p['market']['prices'].values())})
                    probabilities.append(row['probability'])
            else: unscored[reason or 'pending'] += 1
            matches.append({'event':p['event'],'issued_at':p['issued_at'],'sample_as_of':p['sample_as_of'],
                'scope':p['market']['scope'],'market':p['market']['name'], 'state':'scored' if winner else 'pending',
                'winner':winner,'reason':reason,'prices':p['market']['prices'],'variants':item.get('variants'),
                'probabilities':{row['side']:row['probability'] for row in p['rows']}})
    return {'format':'prognozich.model-admin.v1','state':'degraded' if inference_error else 'ready',
        'checked_at':checked.isoformat(),'model_sha256':metrics['model_sha256'],
        'target':'participant_win.v1','training_events':metrics['training_events'],
        'training_rows':metrics['training_rows'],'lead_minutes':manifest['lead_minutes'],
        'recorded_events':len(packets),'scored_events':len(scored)//2,'pending_events':len(packets)-len(scored)//2,
        'evaluation':evaluate(scored,probabilities) if scored else None,
        'matches':sorted(matches,key=lambda m:m['issued_at'],reverse=True)[:100],
        'skipped':dict(skipped),'unscored_reasons':dict(unscored),'inference_error':inference_error,
        'catalogue':catalogue_info,'feature_profile':metrics.get('feature_profile','with_prices.v1'),
        'variant_metrics':variant_metrics(matches) if statistics_only else None,
        'decision_policy':metrics.get('decision_policy'),'unsupported_sports':metrics.get('unsupported_sports',[]),
        'experimental':True,'mode':'prospective','quality_verified':False}



def variant_metrics(matches):
    a={'selected':0,'evaluated':0,'hits':0,'accuracy':None}
    b={'bets':0,'settled':0,'won':0,'lost':0,'skipped':0,'pending':0,'profit_units':0.0,'roi':None,'accuracy':None,'coverage':0.0}
    for m in matches:
        variants=m.get('variants') or {};first=variants.get('statistics') or {};second=variants.get('statistics_price') or {}
        if first.get('side'):
            a['selected']+=1
            if m['state']=='scored':a['evaluated']+=1;a['hits']+=int(first['side']==m['winner'])
        if second.get('decision')!='bet':b['skipped']+=1;continue
        b['bets']+=1
        if m['state']!='scored':b['pending']+=1;continue
        won=second['side']==m['winner'];b['settled']+=1;b['won' if won else 'lost']+=1
        b['profit_units']+=second['odds']-1 if won else -1
    a['accuracy']=a['hits']/a['evaluated'] if a['evaluated'] else None
    b['accuracy']=b['won']/b['settled'] if b['settled'] else None
    b['roi']=b['profit_units']/b['settled'] if b['settled'] else None
    b['coverage']=b['bets']/len(matches) if matches else 0.0
    return {'statistics':a,'statistics_price':b}

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--bundle',type=Path,default=Path(os.environ.get('PARTICIPANT_MODEL_BUNDLE','/var/lib/doscbot-models/all-20261115-1')))
    p.add_argument('--database',type=Path,default=Path('/var/lib/doscbot-collector/data/dataset.sqlite3'))
    p.add_argument('--state',type=Path,default=Path('/var/lib/prognozich-model-client'))
    p.add_argument('--token-file',type=Path,default=Path('/etc/prognozich-model-client/token'))
    args=p.parse_args()
    args.state.mkdir(parents=True,exist_ok=True)
    os.umask(0o077)
    with (args.state/'worker.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
        try:
            result=cycle(args.bundle,args.database,args.state,args.token_file)
        except Exception as error:
            result={'format':'prognozich.model-admin.v1','state':'error',
                'checked_at':datetime.now(timezone.utc).isoformat(),**error_details(error)}
        atomic_report(args.state/'report.json',result)
        print(json.dumps({k:result.get(k) for k in ('state','recorded_events','scored_events','pending_events','catalogue','skipped','inference_error','error_type','sqlite_code','sqlite_name')}))
        if result['state'] == 'error':
            raise SystemExit(1)


if __name__ == '__main__': main()
