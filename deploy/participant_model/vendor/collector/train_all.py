"""New all-data CatBoost fit with pilot-selected parameters and no holdout claim."""

from collections import Counter
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import shutil

from .store import encoded, instant, now
from .training import SCHEMA
from .train_baseline import evaluate, read_dataset


def file_sha256(path):
    checksum=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):checksum.update(chunk)
    return checksum.hexdigest()


def reference_settings(directory, manifest, names, model_class):
    paths=[directory/name for name in ('metrics.json','feature-schema.json','model.cbm')]
    if any(not path.is_file() or path.is_symlink() for path in paths):
        raise ValueError('Reference must contain the preserved pilot model, schema and metrics')
    report=json.loads(paths[0].read_text())
    schema=json.loads(paths[1].read_text())
    if (report.get('format')!='doscbot.baseline-model.v1' or report.get('stage')!='experimental_baseline' or
        report.get('target')!=SCHEMA['target'] or schema!=SCHEMA or
        report.get('schema_sha256')!=manifest['schema_sha256'] or
        set(report.get('sports') or {})!=set(manifest.get('requested_sports') or manifest.get('sports',{}))):
        raise ValueError('Reference pilot has a different target, schema or sport selection')
    parameters=report.get('parameters') or {};trees=report.get('tree_count')
    if (parameters.get('device')!='CPU' or type(trees) is not int or not 1<=trees<=5000 or
        type(parameters.get('iterations')) is not int or not trees<=parameters['iterations']<=5000 or
        type(parameters.get('depth')) is not int or not 2<=parameters['depth']<=8 or
        type(parameters.get('seed')) is not int or not 0<=parameters['seed']<2**31):
        raise ValueError('Invalid pilot training parameters')
    if report.get('best_iteration')!=trees-1:
        raise ValueError('Pilot best iteration and saved tree count disagree')
    # Load solely to verify the saved model and retrieve its actual learning
    # rate; its trees are NOT passed as init_model to the new fit.
    pilot=model_class();pilot.load_model(str(paths[2]))
    actual=pilot.get_all_params()
    if (pilot.tree_count_!=trees or pilot.feature_names_!=names or
        actual.get('depth')!=parameters['depth'] or actual.get('random_seed')!=parameters['seed'] or
        actual.get('loss_function')!='Logloss' or actual.get('task_type')!='CPU' or
        pilot.get_cat_feature_indices()!=[names.index(name) for name in SCHEMA['categorical']]):
        raise ValueError('Saved pilot does not match its metrics or feature schema')
    learning_rate=actual.get('learning_rate')
    if (isinstance(learning_rate,bool) or not isinstance(learning_rate,(int,float)) or
        not math.isfinite(learning_rate) or not 0<learning_rate<=1):
        raise ValueError('Invalid saved pilot learning rate')
    return {'iterations':trees,'depth':parameters['depth'],'seed':parameters['seed'],'learning_rate':learning_rate}, {
        'directory':str(directory),'model_sha256':file_sha256(paths[2]),'metrics_sha256':file_sha256(paths[0]),
        'parameter_selection':'Fixed saved pilot tree count, depth, seed and actual learning rate'}


def train_all(dataset, finding, reference, output, *, threads=2,max_rows=200000):
    if type(threads) is not int or not 1<=threads<=64 or type(max_rows) is not int or not 10<=max_rows<=2000000:
        raise ValueError('Invalid CPU training bounds')
    dataset,finding,reference,output=(Path(path).resolve() for path in (dataset,finding,reference,output))
    for path in (dataset,reference,output):
        if path==finding or finding in path.parents or path in finding.parents:
            raise ValueError('All-data training must stay outside finding')
    if output.exists() or any(output==path or path in output.parents or output in path.parents for path in (dataset,reference)):
        raise ValueError('Use a new model directory outside the input export and preserved pilot')
    manifest,names,folds=read_dataset(dataset,max_rows,all_available=True)
    rows=folds['all']
    try:
        from catboost import CatBoostClassifier,Pool
    except ImportError as error:
        raise ValueError('Run bash scripts/setup-training.sh and use .venv-training/bin/python') from error
    settings,provenance=reference_settings(reference,manifest,names,CatBoostClassifier)
    counts=Counter(row['sport'] for row in rows);total=len(rows)
    weights={sport:total/(len(counts)*count) for sport,count in counts.items()}
    pool=Pool([row['x'] for row in rows],label=[row['y'] for row in rows],feature_names=names,
              cat_features=SCHEMA['categorical'],weight=[weights[row['sport']] for row in rows])
    model=CatBoostClassifier(iterations=settings['iterations'],depth=settings['depth'],learning_rate=settings['learning_rate'],
        loss_function='Logloss',random_seed=settings['seed'],task_type='CPU',thread_count=threads,
        allow_writing_files=False,verbose=False,use_best_model=False)
    started=now()
    model.fit(pool)
    fitted=now()
    probabilities=[float(value[1]) for value in model.predict_proba(pool)]
    groups={row['group']:row for row in rows}
    report={'format':'doscbot.all-data-model.v1','stage':'experimental_all_data','target':SCHEMA['target'],
        'started_at':started,'fitted_at':fitted,'finished_at':now(),
        'python':platform.python_version(),'packages':{name:version(name) for name in ('catboost','numpy','pandas','scipy')},
        'parameters':{**settings,'threads':threads,'device':'CPU','initialization':'from_scratch'},
        'tree_count':model.tree_count_,'reference_pilot':provenance,'dataset_directory':str(dataset),
        'dataset_rows_sha256':manifest['rows_sha256'],'schema_sha256':manifest['schema_sha256'],
        'preparation_code_sha256':manifest.get('preparation_code_sha256'),
        'trainer_code_sha256':hashlib.sha256((Path(__file__).read_bytes()+Path(__file__).with_name('train_baseline.py').read_bytes())).hexdigest(),
        'dataset_checked_at':manifest['checked_at'],'snapshot':manifest.get('snapshot'),
        'sports':dict(counts),'sport_weights':weights,'training_events':len(groups),'training_rows':total,
        'unlabelled_rows_skipped':manifest['export_rows']-total,
        'latest_label_received_at':max((row['label_received_at'] for row in rows),key=instant),
        'evaluation':{'training':evaluate(rows,probabilities)},'holdout_evaluation':None,
        'needs_future_evaluation':True,'validated_for_betting':False,
        'warnings':['all_available_labels_used; reported metrics are training metrics, not holdout quality',
                    'normal_period_labels_only; unsupported overtime/retirement formats may bias selection',
                    'participant rows are paired; event counts are the independent sample counts']}
    if len(groups)<1000:report['warnings'].append('training:small_number_of_independent_events')
    output.mkdir(parents=True,exist_ok=False)
    try:
        model.save_model(str(output/'model.cbm'))
        report['model_sha256']=file_sha256(output/'model.cbm')
        (output/'feature-schema.json').write_text(json.dumps(SCHEMA,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        with (output/'training-events.jsonl').open('x',encoding='utf-8') as stream:
            for key,row in sorted(groups.items()):
                stream.write(encoded({'event_id':key,**{name:row[name] for name in
                    ('sample_sha256','as_of','kickoff_at','label_received_at')}})+'\n')
        (output/'feature-importance.tsv').write_text('feature\timportance\n'+''.join(
            name+'\t'+str(float(value))+'\n' for name,value in zip(names,model.get_feature_importance())),encoding='utf-8')
        report['finished_at']=now()
        (output/'metrics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    except BaseException:
        shutil.rmtree(output);raise
    return report
