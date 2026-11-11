"""Small, reproducible evaluations of the actual independent Winline model.

Saved mode scores real prematch forecasts. Historical mode reconstructs forecasts
from older results only; it cannot prove those data were available at kickoff.
No network requests, model generation, database writes or service restarts.
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import csv
import json
import math
from pathlib import Path
import sqlite3
from urllib.parse import quote

import numpy as np

from .bookmakers import instant
from .chances import INDEPENDENT_MODEL_VERSION, fit_goals, football_result, goal_probabilities, norm, report_markets

KEYS=('home','draw','away')
VERSION=INDEPENDENT_MODEL_VERSION


def results(db):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    table='archive_latest_results' if 'archive_latest_results' in tables else 'results' if 'results' in tables else None
    if table is None:raise ValueError('В базе нет архива результатов Winline.')
    rows=[];rejected=Counter();identities={}
    for raw in db.execute('SELECT * FROM '+table+' WHERE sport_id=1 ORDER BY start_at'):
        row=dict(raw)
        try:
            value=json.loads(row['payload'] if table=='archive_latest_results' else row['raw_json'])
            at=instant(row['start_at']);parsed=football_result(value.get('score'))
            if parsed is None:rejected['unsupported_or_ambiguous_score']+=1;continue
            h,a=value.get('member1'),value.get('member2');competition=value.get('championship')
            if not h or not a or not competition or norm(h)==norm(a):raise ValueError('identity')
            if at>=datetime.now(timezone.utc):rejected['future']+=1;continue
            identity=(at,norm(h),norm(a),norm(competition))
            record={'key':row['result_key'],'home':h,'away':a,'competition':competition,'at':at,
                    'goals':parsed['full'],'half':parsed['half'],'observed_at':row.get('observed_at',row.get('first_seen')),
                    'statistics_id':str(value['idRadar']) if value.get('idRadar') else None}
            if identity in identities and (identities[identity] is None or identities[identity]['goals']!=record['goals']):
                identities[identity]=None;rejected['conflicting_results']+=1
            elif identity not in identities:identities[identity]=record
        except (ValueError,TypeError,KeyError):rejected['invalid_result']+=1
    rows=[r for r in identities.values() if r is not None]
    return sorted(rows,key=lambda r:(r['at'],r['key'])),dict(rejected)


def valid_probabilities(value):
    return (isinstance(value,dict) and set(value)==set(KEYS) and
            all(type(value[k]) in (float,int) and math.isfinite(value[k]) and 0<=value[k]<=1 for k in KEYS) and
            abs(sum(value.values())-1)<1e-6)


def match_result(event,rows):
    try:at=instant(event['kickoff_at'])
    except (KeyError,ValueError,TypeError):return None
    candidates=[r for r in rows if abs((r['at']-at).total_seconds())<=120 and
                all(norm(r[k])==norm(event.get(k)) for k in ('home','away','competition'))]
    return candidates[0] if len(candidates)==1 else None


def settled_markets(assessment,result,context):
    """Score only conditions actually saved before kickoff, including refunds."""
    conditions={m['key']:m for m in context.get('markets',[])};output=[]
    for prediction in assessment.get('recommended_markets') or report_markets(assessment):
        market=conditions.get(prediction['market_key'])
        if not market:continue
        period=norm(market.get('period'));pair=result['goals']
        if period in ('1 тайм','1-й тайм'):
            pair=result['half']
        elif period in ('2 тайм','2-й тайм'):
            pair=tuple(a-b for a,b in zip(result['goals'],result['half'])) if result['half'] else None
        elif period not in ('','осн.время','осн. время','основное время','матч','весь матч','full time','regular time'):pair=None
        if pair is None:
            output.append({'market':prediction,'state':'not_scored','reason':'missing_period_result'});continue
        grid=np.zeros((max(pair)+1,)*2);grid[pair]=1
        condition={**market,'period':'осн.время'}
        values=goal_probabilities(condition,grid,context['event']).get(prediction['outcome_key'])
        if values is None:
            output.append({'market':prediction,'state':'not_scored','reason':'unsupported_settlement'});continue
        output.append({'market':prediction,'state':'push' if values.get('push_probability')==1 else 'win' if values['probability']==1 else 'loss'})
    return output


def saved_cases(db,rows,limit):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'event_analyses' not in tables:return [],{'no_saved_forecasts':1},0
    selected={};rejected=Counter();pending=0
    for row in db.execute('SELECT payload FROM event_analyses ORDER BY generated_at'):
        try:
            saved=json.loads(row[0]);context=saved.get('context') or {};event=context.get('event') or {}
            assessment=context.get('probability_assessment') or {};p=assessment.get('outcome_probabilities')
            if event.get('sport')!='football' or event.get('phase')!='prematch' or not assessment.get('independent_only'):continue
            if assessment.get('model_version')!=VERSION:rejected['other_model_version']+=1;continue
            if not valid_probabilities(p):rejected['invalid_or_missing_probabilities']+=1;continue
            kickoff=instant(event['kickoff_at']);generated=instant(saved['generated_at'])
            if generated>=kickoff:rejected['forecast_after_kickoff']+=1;continue
            if instant(context['snapshot']['observed_at'])>generated:rejected['future_snapshot']+=1;continue
            stats=context.get('team_statistics')
            if stats and (not stats.get('observed_at') or instant(stats['observed_at'])>generated or stats.get('match_phase') in ('live','finished')):
                rejected['statistics_not_prematch']+=1;continue
            result=match_result(event,rows)
            if result is None:pending+=1;continue
            identity=(norm(event['home']),norm(event['away']),kickoff)
            if identity in selected:rejected['duplicate_forecast']+=1;continue
            selected[identity]={'result':result,'probabilities':p,'forecast_at':generated.isoformat(),
                'model_version':assessment.get('model_version','legacy_independent'),
                'sample':assessment.get('data_quality',{}).get('participants',[]),
                'market_results':settled_markets(assessment,result,context),
                'statistics_snapshot':bool(stats),'popular_markets_observed':(context.get('market_selection') or {}).get('policy')=='source-popular-v1'}
        except (ValueError,TypeError,KeyError):rejected['invalid_saved_metadata']+=1
    cohort=sorted(selected.values(),key=lambda c:(c['result']['at'],c['result']['key']))[-limit:]
    return cohort,dict(rejected),pending


def historical_cases(rows,limit):
    # Fix the cohort by time BEFORE checking model eligibility or outcomes.
    cohort=rows[-limit:];cases=[];rejected=Counter()
    for target in cohort:
        h,a=norm(target['home']),norm(target['away'])
        cutoff=target['at']-timedelta(days=1)
        training=[{'home':norm(r['home']),'away':norm(r['away']),'goals':r['goals'],'at':r['at'],
                   'same_competition':norm(r['competition'])==norm(target['competition']),
                   'scope_weight':1 if norm(r['competition'])==norm(target['competition']) else .5}
                  for r in rows if r['at']<cutoff and
                  (norm(r['competition'])==norm(target['competition']) or h in (norm(r['home']),norm(r['away'])) or a in (norm(r['home']),norm(r['away'])))]
        case={'result':target,'forecast_at':target['at'].isoformat(),'model_version':VERSION,
              'market_results':[],'statistics_snapshot':False,'popular_markets_observed':False}
        fit=fit_goals(training[-1500:],h,a,target['at'])
        if fit is None:
            rejected['insufficient_prior_history']+=1;case['probabilities']=None;case['sample']=[]
        else:
            grid,summaries=fit;home,away=np.indices(grid.shape)
            case['probabilities']={'home':float(grid[home>away].sum()),'draw':float(grid[home==away].sum()),'away':float(grid[home<away].sum())}
            case['sample']=[{'side':side,'name':target[side],'confirmed_matches':s['matches']} for side,s in zip(('home','away'),summaries)]
        cases.append(case)
    return cases,dict(rejected),0


def metrics(cases):
    tested=[c for c in cases if valid_probabilities(c.get('probabilities'))]
    if not tested:return None
    losses=[];briers=[];hits=0;confidence=0
    for case in tested:
        h,a=case['result']['goals'];truth='home' if h>a else 'away' if a>h else 'draw';p=case['probabilities']
        losses.append(-math.log(max(1e-15,p[truth])));briers.append(sum((p[k]-(k==truth))**2 for k in KEYS))
        hits+=max(p,key=p.get)==truth;confidence+=max(p.values())
    n=len(tested);rate=hits/n;z=1.959963984540054;denom=1+z*z/n
    center=(rate+z*z/(2*n))/denom;radius=z*math.sqrt(rate*(1-rate)/n+z*z/(4*n*n))/denom
    return {'evaluated':n,'favorite_hits':hits,'accuracy':rate,'accuracy_wilson_95':[max(0,center-radius),min(1,center+radius)],
            'mean_favorite_probability':confidence/n,'brier_3way':sum(briers)/n,'log_loss':sum(losses)/n,
            'uniform_baseline':{'brier_3way':2/3,'log_loss':math.log(3)}}


def run(db,mode='saved',limit=10):
    if mode not in ('saved','historical-results','recorded-statistics') or type(limit) is not int or not 1<=limit<=100:raise ValueError('Режим saved/historical-results/recorded-statistics; размер выборки от 1 до 100.')
    rows,invalid=results(db)
    inventory={}
    if mode=='recorded-statistics':
        from .snapshot_evaluation import recorded_cases
        cases,rejected,pending,inventory=recorded_cases(db,rows,limit)
    else:cases,rejected,pending=(saved_cases(db,rows,limit) if mode=='saved' else historical_cases(rows,limit))
    summary=metrics(cases);output=[]
    for case in cases:
        target=case['result'];p=case['probabilities'];h,a=target['goals'];truth='home' if h>a else 'away' if a>h else 'draw'
        output.append({'result_key':target['key'],'kickoff_at':target['at'].isoformat(),'home':target['home'],'away':target['away'],
            'competition':target['competition'],'actual_score':f'{h}:{a}','actual_outcome':truth,
            'probabilities':p,'favorite':max(p,key=p.get) if p else None,'favorite_hit':max(p,key=p.get)==truth if p else None,
            **{k:v for k,v in case.items() if k not in ('result','probabilities')}})
    return {'format':'matchlab.winline-evaluation.v1','model_version':VERSION,'generated_at':datetime.now(timezone.utc).isoformat(),'mode':mode,
            'state':'small_sample' if summary else 'no_evaluable_forecasts','requested':limit,'selected':len(cases),
            'evaluated':summary['evaluated'] if summary else 0,'pending':pending,'metrics':summary,'matches':output,
            'available_football_results':len(rows),'excluded_results':invalid,'excluded_forecasts':rejected,
            'snapshot_inventory':inventory,
            'calibration_verified':False,'profitability_verified':False,
            'selection':{'saved':'latest completed eligible saved fixtures of current model version; first forecast per fixture',
                'historical-results':'latest valid completed results, fixed before model eligibility; unscored fixtures are not replaced',
                'recorded-statistics':'latest completed fixtures with recorded prematch statistics; earliest retained valid snapshot per fixture; selected before model eligibility'}[mode],
            'limitations':[
                f'{limit} матчей — первичная проверка, а не подтверждение точности процентов; для калибровки нужна значительно большая независимая выборка.',
                'Выборки разных рынков одного матча связаны; доходность без исторических цен не проверяется.',
                *(['Проверены только сохранённые до начала вероятности и статистика; новые страницы завершённых матчей не скачиваются.'] if mode=='saved' else [
                    'Текущая модель пересчитана на записанной до начала статистике; это воспроизведение исторического входа, а не фактически выданный тогда прогноз.',
                    'Архивная история берётся только из записей с временем наблюдения и получения не позже снимка; более поздние версии исключены.',
                    'Проверяется независимый исход 1X2. Нейросеть не вызывается, качество её объяснений и восемь рынков этим прогоном не проверяются.'] if mode=='recorded-statistics' else [
                    'Ретроспективная реконструкция, а не сохранённый предматчевый прогноз: доступность и версии источника на тот момент не доказаны.',
                    'Использованы только более ранние результаты с отступом 24 часа. Нынешние сезонные средние, таблица, форма и статистика целевого матча исключены.',
                    'Полных предматчевых снимков статистики и популярных рынков нет: проверяется только 1X2, восемь рынков не придумываются.'])]}


def save(report,path):
    from .backtest import save_report
    path=Path(path);save_report(report,path)
    def cell(value):
        return "'"+value if isinstance(value,str) and value.startswith(('=','+','-','@','\t','\r','\n')) else value
    with path.with_suffix('.csv').open('w',newline='',encoding='utf-8-sig') as stream:
        writer=csv.writer(stream);writer.writerow(['kickoff','home','away','competition','P1','X','P2','actual','favorite_hit','mode'])
        for row in report['matches']:
            p=row['probabilities'] or {};writer.writerow([cell(v) for v in [row['kickoff_at'],row['home'],row['away'],row['competition'],*[p.get(k) for k in KEYS],row['actual_score'],row['favorite_hit'],report['mode']]])


def evaluate_path(path,mode='saved',limit=10):
    from .mysql_backend import readonly
    with readonly(path) as db:
        return run(db,mode,limit)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path);parser.add_argument('--limit',type=int,default=10)
    parser.add_argument('--mode',choices=('saved','historical-results','recorded-statistics'),default='saved')
    parser.add_argument('--output',type=Path,default=Path('data/evaluations/winline-10.json'))
    args=parser.parse_args()
    if args.database is None:
        from .config import Settings
        args.database=Settings.from_env().database
    try:
        report=evaluate_path(args.database,args.mode,args.limit);save(report,args.output)
    except (OSError,ValueError,sqlite3.Error) as error:parser.exit(1,f'Ошибка проверки: {error}\n')
    print(json.dumps({k:v for k,v in report.items() if k!='matches'},ensure_ascii=False,indent=2))
    print('JSON:',args.output,'CSV:',args.output.with_suffix('.csv'))


if __name__=='__main__':main()
