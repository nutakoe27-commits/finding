"""CPU-only experimental CatBoost fit on a verified temporal training export."""

from collections import Counter
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import shutil

from .store import digest, encoded, instant, now
from .training import SCHEMA


def read_dataset(directory, max_rows, *, all_available=False):
    directory=Path(directory).resolve()
    if any((directory/name).is_symlink() for name in ('manifest.json','schema.json','rows.jsonl')):
        raise ValueError('Refusing redirected training export files')
    manifest=json.loads((directory/'manifest.json').read_text())
    schema=json.loads((directory/'schema.json').read_text())
    if schema!=SCHEMA or manifest.get('schema_sha256')!=digest(schema):
        raise ValueError('Unsupported or inconsistent training schema')
    checked=instant(manifest['checked_at'])
    if all_available:
        if (manifest.get('valid_from') is not None or manifest.get('test_from') is not None or
                manifest.get('split_required') is not True):
            raise ValueError('All-data training needs a fresh prepare-training export without temporal boundaries')
        valid=test=None
    else:
        if not manifest.get('structurally_ready'):
            raise ValueError('Prepare an export with valid temporal boundaries and labelled train/validation/test for each requested sport')
        valid,test=(instant(manifest[key]) for key in ('valid_from','test_from'))
        if not valid<test<=checked:raise ValueError('Invalid temporal boundaries')
    if type(manifest.get('lead_minutes')) is not int or not 0<=manifest['lead_minutes']<=10080:
        raise ValueError('Invalid prediction horizon')
    names=schema['categorical']+schema['numeric']+schema['missing_indicators']
    folds={key:[] for key in (('all',) if all_available else ('train','validation','test'))}; groups={}; checksum=hashlib.sha256();count=0
    with (directory/'rows.jsonl').open('rb') as handle:
        for line in handle:
            checksum.update(line);count+=1
            if count>max_rows:raise ValueError('Export exceeds max-rows; use more RAM or an explicitly larger limit')
            row=json.loads(line); features=row['features']
            if row.get('format')!='doscbot.participant-training-row.v1' or set(features)!=set(names):
                raise ValueError('Unexpected training row or feature columns')
            kickoff=instant(row['kickoff_at']);as_of=instant(row['as_of'])
            partition='all' if all_available else 'train' if kickoff<valid else 'validation' if kickoff<test else 'test'
            expected_partition='unassigned' if all_available else partition
            if row['partition']!=expected_partition or as_of>=kickoff or (kickoff-as_of).total_seconds()<manifest['lead_minutes']*60 or as_of>checked:
                raise ValueError('Row violates temporal split or prediction horizon')
            key=str(row['event_group']);side=features['selection_side']
            identity=(partition,features['sport'],row['as_of'],row['kickoff_at'],row['sample_sha256'])
            group=groups.setdefault(key,{'identity':identity,'sides':set(),'labels':[]})
            if group['identity']!=identity or side not in ('home','away') or side in group['sides']:
                raise ValueError('Match group is duplicated or crosses data partitions')
            group['sides'].add(side);group['labels'].append(row.get('target'))
            for name in schema['categorical']:
                if not isinstance(features[name],str):raise ValueError('Categorical feature must be a string')
            for name in schema['numeric']:
                value=features[name]
                if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)):
                    raise ValueError('Invalid numeric feature')
                if features[name+'__missing']!=int(value is None):raise ValueError('Invalid missing-value indicator')
            label=row.get('target')
            if label is None:continue
            if type(label) is not int or label not in (0,1):raise ValueError('Target must be 0, 1 or null')
            cutoff=checked if all_available else valid if partition=='train' else test if partition=='validation' else checked
            result=row.get('result') or {}
            if not result.get('received_at') or not kickoff<=instant(result['received_at'])<=cutoff:
                raise ValueError('Result was not available at the declared fit/evaluation cutoff')
            data=[features[name] if features[name] is not None else float('nan') for name in names]
            folds[partition].append({'x':data,'y':label,'sport':features['sport'],'group':key,
                'quality':features['capture_state'],'baseline':features['implied_selected'],
                'sample_sha256':row['sample_sha256'],'as_of':row['as_of'],
                'kickoff_at':row['kickoff_at'],'label_received_at':result['received_at']})
    if checksum.hexdigest()!=manifest.get('rows_sha256') or count!=manifest.get('export_rows'):
        raise ValueError('Training rows checksum or count mismatch')
    if any(group['sides']!={'home','away'} or (None in group['labels'] and set(group['labels'])!={None}) for group in groups.values()):
        raise ValueError('Each match must have a consistent pair of participant rows')
    if any(None not in group['labels'] and sum(group['labels'])>1 for group in groups.values()):
        raise ValueError('Both participants cannot win the same market')
    training=folds['all' if all_available else 'train']
    if not all(folds.values()) or {row['y'] for row in training}!={0,1}:
        raise ValueError('Need labelled data and both target classes in training')
    sports=set(manifest.get('requested_sports') or manifest.get('sports',{}))
    if not sports or any({row['sport'] for row in folds[fold]}!=sports for fold in folds):
        raise ValueError('Requested sports are missing from a labelled data partition')
    if any({row['y'] for row in training if row['sport']==sport}!={0,1} for sport in sports):
        raise ValueError('Each requested sport needs both train classes')
    return manifest,names,folds


def metrics(rows, probabilities):
    if len(rows)!=len(probabilities):raise ValueError('Prediction length mismatch')
    if not rows:return None
    if any(not math.isfinite(p) or not 0<=p<=1 for p in probabilities):raise ValueError('Invalid predicted probability')
    loss=-sum(row['y']*math.log(max(1e-15,p))+(1-row['y'])*math.log(max(1e-15,1-p)) for row,p in zip(rows,probabilities))/len(rows)
    brier=sum((row['y']-p)**2 for row,p in zip(rows,probabilities))/len(rows)
    bins=[]
    for index in range(10):
        selected=[(row['y'],p) for row,p in zip(rows,probabilities) if min(int(p*10),9)==index]
        if selected:bins.append({'bin':index,'rows':len(selected),'predicted':sum(p for _,p in selected)/len(selected),
                                 'observed':sum(y for y,_ in selected)/len(selected)})
    return {'rows':len(rows),'events':len({row['group'] for row in rows}),'log_loss':loss,'brier':brier,'calibration':bins}


def evaluate(rows, probabilities):
    result={'overall':metrics(rows,probabilities),'sports':{},'quality':{}}
    for field,target in (('sport','sports'),('quality','quality')):
        for key in sorted({row[field] for row in rows}):
            indices=[i for i,row in enumerate(rows) if row[field]==key]
            result[target][key]=metrics([rows[i] for i in indices],[probabilities[i] for i in indices])
    baseline=[i for i,row in enumerate(rows) if row['baseline'] is not None]
    result['bookmaker_baseline']=metrics([rows[i] for i in baseline],[rows[i]['baseline'] for i in baseline])
    result['model_on_baseline_rows']=metrics([rows[i] for i in baseline],[probabilities[i] for i in baseline])
    return result


def train_baseline(dataset, finding, output, *, threads=2,iterations=400,depth=5,max_rows=200000):
    if not 1<=threads<=64 or not 10<=iterations<=5000 or not 2<=depth<=8 or not 10<=max_rows<=2000000:
        raise ValueError('Invalid CPU training bounds')
    dataset,finding,output=Path(dataset).resolve(),Path(finding).resolve(),Path(output).resolve()
    if dataset==finding or finding in dataset.parents or dataset in finding.parents:
        raise ValueError('Training dataset must be outside finding')
    if output.exists() or output==finding or finding in output.parents or output==dataset or dataset in output.parents:
        raise ValueError('Use a new model directory outside finding and the input export')
    manifest,names,folds=read_dataset(dataset,max_rows)
    try:
        from catboost import CatBoostClassifier,Pool
    except ImportError as error:
        raise ValueError('Run bash scripts/setup-training.sh and use .venv-training/bin/python') from error
    counts=Counter(row['sport'] for row in folds['train']);total=sum(counts.values())
    def pool(split,weighted=False):
        rows=folds[split]
        return Pool([row['x'] for row in rows],label=[row['y'] for row in rows],feature_names=names,
                    cat_features=SCHEMA['categorical'],
                    weight=[total/(len(counts)*counts[row['sport']]) for row in rows] if weighted else None)
    train,validation=pool('train',True),pool('validation')
    model=CatBoostClassifier(iterations=iterations,depth=depth,learning_rate=.05,loss_function='Logloss',
        eval_metric='Logloss',random_seed=42,task_type='CPU',thread_count=threads,
        allow_writing_files=False,verbose=False)
    started=now()
    model.fit(train,eval_set=validation,early_stopping_rounds=40,use_best_model=True)
    report={'format':'doscbot.baseline-model.v1','stage':'experimental_baseline','started_at':started,'finished_at':now(),
            'target':SCHEMA['target'],'dataset_rows_sha256':manifest['rows_sha256'],'schema_sha256':manifest['schema_sha256'],
            'preparation_code_sha256':manifest.get('preparation_code_sha256'),
            'python':platform.python_version(),'packages':{name:version(name) for name in ('catboost','numpy','pandas','scipy')},
            'parameters':{'threads':threads,'iterations':iterations,'depth':depth,'seed':42,'device':'CPU'},
            'best_iteration':model.get_best_iteration(),'tree_count':model.tree_count_,
            'valid_from':manifest['valid_from'],'test_from':manifest['test_from'],'sports':dict(counts),
            'evaluation':{},'warnings':[],'validated_for_betting':False}
    for split in ('train','validation','test'):
        probabilities=[float(row[1]) for row in model.predict_proba(pool(split))]
        report['evaluation'][split]=evaluate(folds[split],probabilities)
        if report['evaluation'][split]['overall']['events']<(1000 if split=='train' else 100):
            report['warnings'].append(split+':small_number_of_independent_events')
    report['warnings'].append('normal_period_labels_only; unsupported overtime/retirement formats may bias selection')
    report['warnings'].append('participant rows are paired; event counts are the independent sample counts')
    output.mkdir(parents=True,exist_ok=False)
    try:
        model.save_model(str(output/'model.cbm'))
        (output/'feature-schema.json').write_text(json.dumps(SCHEMA,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (output/'metrics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (output/'feature-importance.tsv').write_text('feature\timportance\n'+''.join(
            name+'\t'+str(float(importance))+'\n' for name,importance in zip(names,model.get_feature_importance())),encoding='utf-8')
    except BaseException:
        shutil.rmtree(output);raise
    return report
