"""Frozen-model temporal backtests and forecasts recorded before kickoff."""

from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
import json
import math
from pathlib import Path
import shutil

from .monitor_data import readonly
from .store import check_observation_times,digest,encoded,instant,now,sample_blocker,widget_blocker
from .training import NATIVE_SPORTS,SCHEMA,linked_label,participant_rows,winner_market
from .training_evidence import identifier,native_at,participant_key
from .train_all import file_sha256
from .train_baseline import evaluate,read_dataset


def outside(path, finding):
    path=Path(path).resolve();finding=Path(finding).resolve()
    if path==finding or finding in path.parents or path in finding.parents:
        raise ValueError('Evaluation must stay outside finding')
    return path


@contextmanager
def owned_database(state,finding):
    state=outside(state,finding);path=state/'dataset.sqlite3'
    if path.is_symlink() or not path.is_file():raise ValueError('Own collector snapshot is missing or redirected')
    db=readonly(path)
    try:
        owner=(db.execute("SELECT value FROM metadata WHERE key='owner'").fetchone()
               if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone() else None)
        if not owner or owner[0]!='doscbot.fresh-collector.v1':raise ValueError('Only the new collector database is allowed')
        yield db
    finally:db.close()


def vector(features,names):
    if set(features)!=set(names):raise ValueError('Unexpected prediction feature schema')
    if any(not isinstance(features[key],str) for key in SCHEMA['categorical']):raise ValueError('Invalid categorical feature')
    for key in SCHEMA['numeric']:
        value=features[key]
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)):
            raise ValueError('Invalid numeric feature')
        if type(features[key+'__missing']) is not int or features[key+'__missing']!=int(value is None):
            raise ValueError('Invalid missing-value indicator')
    return [features[key] if features[key] is not None else float('nan') for key in names]


def load_model(directory,finding,max_rows):
    directory=outside(directory,finding)
    paths=[directory/name for name in ('metrics.json','feature-schema.json','model.cbm','training-events.jsonl')]
    if any(not p.is_file() or p.is_symlink() for p in paths):raise ValueError('A complete all-data model and training ledger are required')
    report=json.loads(paths[0].read_text());schema=json.loads(paths[1].read_text())
    if (report.get('format')!='doscbot.all-data-model.v1' or report.get('stage')!='experimental_all_data' or report.get('target')!=SCHEMA['target'] or
        schema!=SCHEMA or report.get('schema_sha256')!=digest(schema) or report.get('model_sha256')!=file_sha256(paths[2])):
        raise ValueError('Model, target, schema or checksum mismatch')
    dataset=outside(report['dataset_directory'],finding)
    manifest,names,folds=read_dataset(dataset,max_rows,all_available=True)
    if manifest['rows_sha256']!=report['dataset_rows_sha256']:raise ValueError('Original training export has changed')
    groups={row['group']:row for row in folds['all']}
    ledger={}
    for line in paths[3].read_text().splitlines():
        value=json.loads(line);key=value['event_id']
        if key in ledger:raise ValueError('Duplicate training-ledger event')
        ledger[key]=value
    if set(ledger)!=set(groups) or len(groups)!=report['training_events']:raise ValueError('Training ledger mismatch')
    for key,row in groups.items():
        if any(ledger[key].get(field)!=row[field] for field in ('sample_sha256','as_of','kickoff_at','label_received_at')):
            raise ValueError('Training ledger does not match its original export')
    ready=instant(report['finished_at'])
    if ready<instant(manifest['checked_at']) or any(instant(row['label_received_at'])>ready for row in groups.values()):
        raise ValueError('Model readiness precedes its training data')
    from catboost import CatBoostClassifier
    model=CatBoostClassifier();model.load_model(str(paths[2]))
    if (model.tree_count_!=report['tree_count'] or model.feature_names_!=names or
        model.get_cat_feature_indices()!=[names.index(key) for key in SCHEMA['categorical']]):
        raise ValueError('Saved model does not match its feature contract')
    return model,names,groups,{'model_directory':str(directory),'model_sha256':report['model_sha256'],
        'model_ready_at':ready.isoformat(),'training_events':len(groups),'lead_minutes':manifest['lead_minutes'],
        'requested_sports':manifest['requested_sports'],'schema_sha256':digest(SCHEMA),
        'training_ledger_sha256':file_sha256(paths[3]),'evaluator_code_sha256':file_sha256(Path(__file__))}


def validated_sample(row):
    payload=json.loads(row['payload']);event=payload['event'];widget=payload.get('widget_data')
    if (payload.get('format')!='doscbot.source-input.v2' or digest(payload)!=row['sha256'] or
        str(event['source_event_id'])!=row['event_id'] or instant(payload['as_of'])!=instant(row['as_of']) or
        instant(event['kickoff_at'])!=instant(row['kickoff_at'])):raise ValueError('Sample integrity mismatch')
    reason=sample_blocker(event,payload['team_statistics'],row['as_of'])
    reason=reason or ('widget_missing' if not widget else widget_blocker(event,payload['team_statistics'],widget))
    if reason:raise ValueError(reason)
    if (not all(participant_key(event.get(side)) for side in ('home','away')) or
        participant_key(event['home'])==participant_key(event['away']) or
        any((widget.get('match') or {}).get(key) is True for key in ('cancelled','postponed','retired','walkover','disqualified'))):
        raise ValueError('Unavailable target or participant identity')
    check_observation_times(payload,instant(row['as_of']))
    return payload


def training_statistics_ids(db,groups):
    identifiers=set();found=set();keys=list(groups)
    for start in range(0,len(keys),500):
        batch=keys[start:start+500]
        for row in db.execute("SELECT * FROM samples WHERE event_id IN ("+','.join('?' for _ in batch)+')',batch):
            if row['sha256']!=groups[row['event_id']]['sample_sha256']:continue
            payload=validated_sample(row)
            identifiers.add(str(payload['widget_data']['statistics_id']));found.add(row['event_id'])
    if found!=set(groups):raise ValueError('Snapshot lacks frozen training samples; use the continuous collector database')
    return identifiers


def original_sample(db,packet):
    if packet.get('format')!='doscbot.frozen-forecast.v1':raise ValueError('Unsupported forecast format')
    row=db.execute('SELECT * FROM samples WHERE event_id=? AND sha256=? ORDER BY id LIMIT 1',
                   (packet['event']['source_event_id'],packet['sample_sha256'])).fetchone()
    if not row:raise ValueError('Frozen prediction sample is unavailable')
    payload=validated_sample(row)
    if instant(payload['as_of'])!=instant(packet['sample_as_of']):raise ValueError('Forecast sample time mismatch')
    event=payload['event']
    if any(event.get(key)!=packet['event'].get(key) for key in packet['event']):raise ValueError('Forecast event mismatch')
    market,reason=winner_market(event)
    if reason or encoded(market)!=encoded(packet['market']):raise ValueError('Frozen market mismatch')
    rows=list(participant_rows(payload,market))
    if len(packet['rows'])!=2 or [r['side'] for r in packet['rows']]!=['home','away']:
        raise ValueError('Forecast needs one ordered participant pair')
    for (side,features),record in zip(rows,packet['rows']):
        if side!=record['side'] or encoded(features)!=encoded(record['features']):raise ValueError('Frozen features changed')
        probability=record['probability']
        if isinstance(probability,bool) or not isinstance(probability,(int,float)) or not math.isfinite(probability) or not 0<=probability<=1:
            raise ValueError('Invalid frozen probability')
    return payload


def score_packets(db,packets,header,cutoff):
    scored=[];probabilities=[];pending=Counter();outcomes=[]
    ready=instant(header['model_ready_at']);lead=timedelta(minutes=header['lead_minutes'])
    for packet in packets:
        payload=original_sample(db,packet);event=payload['event'];kickoff=instant(event['kickoff_at'])
        sample_time=instant(payload['as_of']);issued=instant(packet['issued_at'])
        if (packet.get('model_sha256')!=header['model_sha256'] or sample_time<ready or issued<sample_time or
            issued<ready or issued>cutoff or kickoff-sample_time<lead):raise ValueError('Invalid model or forecast timing')
        recorded=max(issued,instant(header['sealed_at'])) if header['mode']=='prospective' else issued
        if recorded>cutoff:raise ValueError('Prediction batch was not yet sealed at the scoring cutoff')
        if header['mode']=='prospective' and kickoff-recorded<lead:raise ValueError('Forecast was not recorded before its horizon')
        winner,reason,result=linked_label(db,event,payload['widget_data'],packet['market'],cutoff,as_of=payload['as_of'])
        if winner:
            source=json.loads(db.execute('SELECT payload FROM observations WHERE id=?',(result['observation_id'],)).fetchone()[0])
            forecast_time=recorded if header['mode']=='prospective' else sample_time
            dates=[instant(source[key]) for key in ('kickoff_at','result_start_at') if source.get(key)]
            if min(dates)-forecast_time<lead:winner=None;reason='revised_start_violates_prediction_horizon'
        outcomes.append({'event_id':event['source_event_id'],'winner':winner,'reason':reason,'result':result})
        if not winner:pending[reason or 'unlabelled']+=1;continue
        for record in packet['rows']:
            features=record['features']
            scored.append({'y':int(winner==record['side']),'sport':event['sport'],'group':event['source_event_id'],
                'quality':features['capture_state'],'baseline':features['implied_selected']})
            probabilities.append(record['probability'])
    sports=Counter(row['sport'] for row in scored)
    return {'scored_events':len(scored)//2,'scored_rows':len(scored),
            'scored_sports':{sport:sports[sport]//2 for sport in header['requested_sports']},'unscored_reasons':dict(pending),
            'evaluation':evaluate(scored,probabilities) if scored else None,'outcomes':outcomes,
            'warnings':(['small_number_of_independent_events'] if len(scored)<200 else [])+
                ['normal_period_labels_only; unsupported outcomes may bias selection',
                 'retrospective predictions were computed after events' if header['mode']=='retrospective_temporal_holdout'
                 else 'prospective forecasts were frozen before kickoff'],
            'validated_for_betting':False}


def save_bundle(output,packets,report):
    output.mkdir(parents=True,exist_ok=False)
    try:
        with (output/'predictions.jsonl').open('x',encoding='utf-8') as stream:
            for packet in packets:stream.write(encoded(packet)+'\n')
        sealed=instant(now())
        if sealed<instant(report['recorded_at']):raise ValueError('Clock moved backwards while saving forecasts')
        if report['mode']=='prospective' and any(instant(p['event']['kickoff_at'])-sealed<timedelta(minutes=report['lead_minutes']) for p in packets):
            raise ValueError('Forecast horizon passed while saving; start a new forecast batch')
        report['sealed_at']=sealed.isoformat()
        report['predictions_sha256']=file_sha256(output/'predictions.jsonl')
        report['prediction_events']=len(packets)
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    except BaseException:shutil.rmtree(output);raise


def make_forecasts(state,finding,directory,output,*,retrospective=False,threads=2,max_samples=200000):
    state=outside(state,finding);directory=outside(directory,finding);output=outside(output,finding)
    if type(threads) is not int or not 1<=threads<=64 or type(max_samples) is not int or not 10<=max_samples<=2000000:
        raise ValueError('Invalid evaluation limits')
    if output.exists() or any(output==p or p in output.parents or output in p.parents for p in (state,directory)):
        raise ValueError('Use a new evaluation directory outside snapshots and model')
    model,names,trained,header=load_model(directory,finding,max_samples)
    started=instant(now());ready=instant(header['model_ready_at']);lead=timedelta(minutes=header['lead_minutes'])
    if ready>started:raise ValueError('Model is not available at this evaluation time')
    header.update(format='doscbot.forecast-batch.v1',mode='retrospective_temporal_holdout' if retrospective else 'prospective',
                  started_at=started.isoformat(),database=str(state/'dataset.sqlite3'),skipped={},
                  inference_only=True,validated_for_betting=False)
    packets=[];skipped=Counter();seen_statistics=set()
    with owned_database(state,finding) as db:
        protected=training_statistics_ids(db,trained)
        header['snapshot']={'max_sample_id':db.execute('SELECT max(id) FROM samples').fetchone()[0],
                            'max_observation_id':db.execute('SELECT max(id) FROM observations').fetchone()[0]}
        def horizon(as_of,kickoff):
            try:return int(instant(kickoff)-instant(as_of)>=lead)
            except (TypeError,ValueError):return 0
        db.create_function('forecast_horizon',2,horizon,deterministic=True)
        query="""WITH ranked AS (SELECT id,row_number() OVER(PARTITION BY event_id ORDER BY julianday(as_of) DESC,id DESC) rn
            FROM samples WHERE julianday(as_of)>=julianday(?) AND julianday(as_of)<=julianday(?) AND forecast_horizon(as_of,kickoff_at)=1)
            SELECT s.* FROM samples s JOIN ranked r ON s.id=r.id WHERE r.rn=1 ORDER BY julianday(s.kickoff_at),s.event_id LIMIT ?"""
        for index,row in enumerate(db.execute(query,(ready.isoformat(),started.isoformat(),max_samples+1))):
            if index==max_samples:raise ValueError('Evaluation exceeds max-samples')
            if row['event_id'] in trained:skipped['trained_event']+=1;continue
            try:
                payload=validated_sample(row);event=payload['event'];widget=payload['widget_data'];kickoff=instant(row['kickoff_at'])
                if str(widget['statistics_id']) in protected:skipped['trained_statistics_id']+=1;continue
                if str(widget['statistics_id']) in seen_statistics:skipped['duplicate_statistics_id']+=1;continue
                if event['sport'] not in header['requested_sports']:skipped['unsupported_sport']+=1;continue
                if not ready<=instant(row['as_of'])<=started:skipped['sample_outside_evaluation_window']+=1;continue
                if (kickoff>started if retrospective else kickoff-started<lead):skipped['outside_forecast_window']+=1;continue
                market,reason=winner_market(event)
                if reason:skipped[reason]+=1;continue
                if not retrospective:
                    native=native_at(db,event['source_event_id'],started)
                    if not native:skipped['current_schedule_unverified']+=1;continue
                    value=native[1]
                    if (value.get('isLive') not in (False,0,None) or type(value.get('idSport')) is not int or
                        str(value.get('id'))!=event['source_event_id'] or NATIVE_SPORTS.get(value.get('idSport'))!=event['sport'] or
                        [participant_key(x) for x in value.get('members',[])]!=[participant_key(event[x]) for x in ('home','away')] or
                        abs((instant(value['date'])-kickoff).total_seconds())>60 or
                        identifier(value.get('idRadar')) not in (None,identifier(widget['statistics_id']))):
                        skipped['current_schedule_or_identity_changed']+=1;continue
                    if db.execute("SELECT 1 FROM observations WHERE kind='event_outcome' AND entity_id=? AND julianday(received_at)<=julianday(?) LIMIT 1",
                                  (event['source_event_id'],started.isoformat())).fetchone():
                        skipped['result_already_observed']+=1;continue
                records=[]
                for side,features in participant_rows(payload,market):
                    vector(features,names);records.append({'side':side,'features':features})
                packets.append({'format':'doscbot.frozen-forecast.v1','event':{key:event.get(key) for key in
                    ('source_event_id','sport','home','away','competition','kickoff_at')},'sample_sha256':row['sha256'],
                    'sample_as_of':row['as_of'],'model_sha256':header['model_sha256'],'market':market,'rows':records})
                seen_statistics.add(str(widget['statistics_id']))
            except (KeyError,TypeError,ValueError,OverflowError):skipped['invalid_sample']+=1
        if packets:
            from catboost import Pool
            data=[vector(r['features'],names) for p in packets for r in p['rows']]
            values=model.predict_proba(Pool(data,feature_names=names,cat_features=SCHEMA['categorical']),thread_count=threads)
            for packet,probabilities in zip(packets,zip(values[::2],values[1::2])):
                for record,p in zip(packet['rows'],probabilities):record['probability']=float(p[1])
        issued=instant(now())
        if issued<started:raise ValueError('Clock moved backwards during prediction')
        if not retrospective:
            kept=[p for p in packets if instant(p['event']['kickoff_at'])-issued>=lead]
            skipped['horizon_passed_during_prediction']+=len(packets)-len(kept);packets=kept
        for packet in packets:packet['issued_at']=issued.isoformat()
        header.update(recorded_at=issued.isoformat(),skipped=dict(skipped),
                      forecast_sports=dict(Counter(p['event']['sport'] for p in packets)))
        if retrospective:header.update(score_packets(db,packets,header,issued))
    save_bundle(output,packets,header)
    return header


def score_forecasts(state,finding,predictions,output):
    state=outside(state,finding);predictions=outside(predictions,finding);output=outside(output,finding)
    if output.exists() or any(output==p or p in output.parents or output in p.parents for p in (state,predictions)):
        raise ValueError('Use a new scoring directory outside inputs')
    if any((predictions/name).is_symlink() for name in ('report.json','predictions.jsonl')):raise ValueError('Redirected prediction files')
    header=json.loads((predictions/'report.json').read_text())
    if (header.get('format')!='doscbot.forecast-batch.v1' or header.get('mode')!='prospective' or
        header.get('schema_sha256')!=digest(SCHEMA) or
        header.get('predictions_sha256')!=file_sha256(predictions/'predictions.jsonl')):
        raise ValueError('Prospective prediction batch is missing or changed')
    model_path=outside(header['model_directory'],finding)
    if output==model_path or model_path in output.parents or output in model_path.parents:
        raise ValueError('Scoring output must stay outside the preserved model')
    packets=[json.loads(line) for line in (predictions/'predictions.jsonl').read_text().splitlines()]
    if len(packets)!=header['prediction_events'] or len({p['event']['source_event_id'] for p in packets})!=len(packets):
        raise ValueError('Forecast count or duplicate-event mismatch')
    at=instant(now())
    with owned_database(state,finding) as db:
        score=score_packets(db,packets,header,at)
    report={'format':'doscbot.forecast-score.v1','mode':'prospective','checked_at':at.isoformat(),
            'model_sha256':header['model_sha256'],'predictions_directory':str(predictions),
            'predictions_sha256':header['predictions_sha256'],'prediction_events':len(packets),**score}
    output.mkdir(parents=True,exist_ok=False)
    try:(output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    except BaseException:shutil.rmtree(output);raise
    return report
