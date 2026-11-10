"""Exploratory paired comparisons; never changes the production forecast model."""
import argparse
from collections import Counter
from datetime import datetime,timezone
import csv
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import zlib
from urllib.parse import quote

import numpy as np
from scipy.optimize import brentq,least_squares
from scipy.stats import nbinom,poisson,skellam

from .bookmakers import instant
from .chances import INDEPENDENT_MODEL_VERSION,football_result,norm
from .winline_backtest import KEYS,valid_probabilities


METHODS={
    'poisson_v19':'Текущий Пуассон v19',
    'uniform':'Равномерный ориентир',
    'shrink_25':'Пуассон + 25% равномерного ориентира',
    'shrink_50':'Пуассон + 50% равномерного ориентира',
    'temperature_150':'Сглаживание вероятностей, T=1.5',
    'dixon_coles_005':'Диксон–Коулз, ρ=−0.05',
    'dixon_coles_010':'Диксон–Коулз, ρ=−0.10',
    'negative_binomial_5':'Отрицательно-биномиальная, k=5',
    'negative_binomial_10':'Отрицательно-биномиальная, k=10',
}
MARKET_METHODS={'odds_proportional':'Коэффициенты: пропорциональное снятие маржи',
                'odds_shin':'Коэффициенты: метод Шина',
                'odds_power':'Коэффициенты: степенное снятие маржи'}
HISTORY_METHODS={'league_dirichlet':'Исторические частоты турнира с prior Дирихле',
                 'elo_davidson':'Elo–Davidson с вероятностью ничьей'}


def distribution(values):
    values=np.asarray(values,dtype=float)
    if values.shape!=(3,) or not np.isfinite(values).all() or (values<0).any() or values.sum()<=0:
        raise ValueError('Некорректное распределение вероятностей.')
    values=values/values.sum()
    return dict(zip(KEYS,map(float,values)))


def matrix_outcomes(matrix):
    h,a=np.indices(matrix.shape)
    return distribution([matrix[h>a].sum(),matrix[h==a].sum(),matrix[h<a].sum()])


def poisson_outcomes(home,away):
    return distribution([skellam.sf(0,home,away),skellam.pmf(0,home,away),skellam.cdf(-1,home,away)])


def recover_rates(probabilities):
    """Recover the original Poisson means, not a new fit to the match result.

    There are two independent 1X2 probabilities and two Poisson means. Reject
    distributions that cannot be recovered to a tight tolerance in model range.
    """
    target=np.array([probabilities[k] for k in KEYS])
    def residual(log_rates):
        return np.array(list(poisson_outcomes(*np.exp(log_rates)).values()))-target
    best=None
    for initial in ((1.2,1.2),(.5,2.5),(2.5,.5)):
        fit=least_squares(residual,np.log(initial),bounds=(math.log(.001),math.log(30)),
                          ftol=1e-12,xtol=1e-12,gtol=1e-12,max_nfev=200)
        error=float(np.max(np.abs(residual(fit.x))))
        if best is None or error<best[0]:best=(error,fit.x)
        if error<1e-8:break
    if best is None or best[0]>1e-7:raise ValueError('Исходное распределение не восстановлено как независимый Пуассон.')
    return tuple(map(float,np.exp(best[1])))


def dixon_coles(home,away,rho):
    # Clamp using means only, so all four low-score factors remain positive.
    rho=max(rho,-.99/max(home,away))
    limit=max(12,int(poisson.ppf(1-1e-12,max(home,away))))
    grid=np.outer(poisson.pmf(np.arange(limit+1),home),poisson.pmf(np.arange(limit+1),away))
    grid[0,0]*=1-home*away*rho
    grid[0,1]*=1+home*rho
    grid[1,0]*=1+away*rho
    grid[1,1]*=1-rho
    return matrix_outcomes(grid)


def negative_binomial(home,away,shape):
    limit=max(12,int(nbinom.ppf(1-1e-11,shape,shape/(shape+max(home,away)))))
    if limit>512:raise ValueError('Слишком длинный хвост распределения.')
    grid=np.outer(nbinom.pmf(np.arange(limit+1),shape,shape/(shape+home)),
                  nbinom.pmf(np.arange(limit+1),shape,shape/(shape+away)))
    return matrix_outcomes(grid)


def variants(probabilities):
    """Only the saved probabilities enter this function; no outcome labels."""
    p=np.array([probabilities[k] for k in KEYS])
    values={'poisson_v19':probabilities,'uniform':dict.fromkeys(KEYS,1/3),
            'shrink_25':distribution(.75*p+.25/3),'shrink_50':distribution(.5*p+.5/3),
            'temperature_150':distribution(p**(1/1.5))}
    errors={}
    try:home,away=recover_rates(probabilities)
    except ValueError:
        return values,{key:'poisson_reconstruction_failed' for key in METHODS if key not in values}
    for key,rho in [('dixon_coles_005',-.05),('dixon_coles_010',-.10)]:values[key]=dixon_coles(home,away,rho)
    for key,shape in [('negative_binomial_5',5),('negative_binomial_10',10)]:values[key]=negative_binomial(home,away,shape)
    return values,errors


def devig(odds):
    """Three mutually exclusive 90-minute odds from ONE historical snapshot."""
    if not isinstance(odds,dict) or set(odds)!=set(KEYS) or any(
            type(odds[k]) not in (int,float) or not math.isfinite(odds[k]) or odds[k]<=1 for k in KEYS):
        raise ValueError('Нужны три десятичных коэффициента больше единицы.')
    q=np.array([1/odds[k] for k in KEYS]);total=float(q.sum())
    output={'odds_proportional':distribution(q)}
    logq=np.log(q)
    hi=max(1,math.log(1/3)/float(np.max(logq)))*1.01
    power=brentq(lambda k:float(np.sum(np.exp(k*logq)))-1,0,hi) if abs(total-1)>1e-12 else 1
    output['odds_power']=distribution(q**power)
    if total>=1-1e-12:
        def shin_values(z):
            # Stable form avoids cancellation near z=1.
            ratio=q*q/total
            return 2*ratio/(np.sqrt(z*z+4*(1-z)*ratio)+z)
        z=brentq(lambda z:float(shin_values(z).sum())-1,0,1) if total>1+1e-12 else 0
        output['odds_shin']=distribution(shin_values(z))
    return output


def measures(cases,method):
    values=[(row['predictions'][method],row['actual_outcome']) for row in cases if method in row['predictions']]
    if not values:return None
    n=len(values);unique=[]
    for p,truth in values:
        top=[k for k in KEYS if abs(p[k]-max(p.values()))<1e-12]
        if len(top)==1:unique.append(top[0]==truth)
    return {'matches':n,'favorite_matches':len(unique),'favorite_hits':sum(unique) if unique else None,
            'accuracy':sum(unique)/len(unique) if unique else None,
            'brier_3way':sum(sum((p[k]-(truth==k))**2 for k in KEYS) for p,truth in values)/n,
            'log_loss':-sum(math.log(max(1e-15,p[truth])) for p,truth in values)/n,
            'mean_favorite_probability':sum(max(p.values()) for p,_ in values)/n}


def load_cases(report):
    if report.get('format')!='matchlab.winline-evaluation.v1' or not isinstance(report.get('matches'),list):
        raise ValueError('Нужен отчёт Winline evaluation v1.')
    if report.get('model_version')!=INDEPENDENT_MODEL_VERSION:
        raise ValueError('Версия исходной модели не совпадает с поддерживаемой версией сравнения.')
    cases=[];seen=set();missing=0
    for raw in report['matches']:
        if raw.get('probabilities') is None:missing+=1;continue
        if not valid_probabilities(raw.get('probabilities')):raise ValueError('Некорректные исходные вероятности.')
        pair=football_result(raw.get('actual_score'))
        truth=raw.get('actual_outcome')
        if pair is None or truth not in KEYS:raise ValueError('Нет подтверждённого результата для проверки.')
        h,a=pair['full']
        if truth!=('home' if h>a else 'away' if a>h else 'draw'):raise ValueError('Счёт не соответствует исходу.')
        key=raw.get('result_key')
        if not isinstance(key,str) or not key or key in seen:raise ValueError('Повтор или отсутствие идентификатора матча.')
        seen.add(key)
        case={k:raw.get(k) for k in ('result_key','home','away','competition','kickoff_at','forecast_at','actual_outcome','actual_score')}
        case['predictions'],case['unavailable']=variants(raw['probabilities'])
        cases.append(case)
    return cases,missing


def historical_quotes(db):
    """Read retained, timed snapshots. match_quotes rejects anything after as_of."""
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    output=[]
    from .bookmakers import make_snapshot,validate_snapshot
    from .chances import full_time,is_1x2
    if 'bookmaker_quotes' in tables:
        for raw in db.execute("SELECT payload FROM bookmaker_quotes WHERE source='winline'"):
            try:
                value=validate_snapshot(json.loads(raw[0]))
                if value['source']=='winline':output.append(value)
            except (ValueError,KeyError,TypeError):continue
    def from_markets(event,markets,captured):
        if event.get('sport')!='football' or event.get('phase')!='prematch' or not captured:return
        for market in markets:
            try:
                market={**market,'outcomes_complete':market.get('outcomes_complete',len(market.get('outcomes',[]))==3)}
                if not full_time(market) or not is_1x2(market):continue
                at=market.get('observed_at') or captured
                if instant(at)>instant(captured):continue
                aliases={'п1':'home','1':'home','x':'draw','х':'draw','п2':'away','2':'away'}
                prices={aliases[norm(o['name'])]:o['odds'] for o in market['outcomes'] if o.get('state')=='open'}
                if set(prices)!=set(KEYS):continue
                value=make_snapshot('winline',event['competition'],event['home'],event['away'],event['kickoff_at'],
                                    at,[prices[k] for k in KEYS],event['url'],external_id=event.get('source_event_id'))
                # Preserve the full capture bound as well as the individual market time.
                value['_capture_at']=captured
                output.append(value)
            except (ValueError,KeyError,TypeError):continue
    if 'event_analyses' in tables:
        for raw in db.execute('SELECT payload FROM event_analyses'):
            try:
                context=json.loads(raw[0]).get('context') or {}
                neural=context.get('neural_input') or {}
                if isinstance(neural.get('quote'),dict):
                    value=validate_snapshot(neural['quote'])
                    if value['source']=='winline' and neural.get('as_of'):
                        value['_capture_at']=neural['as_of'];output.append(value)
                from_markets(context.get('event') or {},context.get('markets') or [],(context.get('snapshot') or {}).get('observed_at'))
            except (ValueError,KeyError,TypeError):continue
    if 'bookmaker_event_markets' in tables:
        for raw in db.execute("SELECT metadata,payload FROM bookmaker_event_markets WHERE phase='prematch'"):
            try:
                event=json.loads(raw[0]);decoder=zlib.decompressobj()
                body=decoder.decompress(raw[1],2*1024*1024+1)
                if len(body)>2*1024*1024 or not decoder.eof:continue
                document=json.loads(body)
                from_markets(event,document.get('markets',[]),event.get('capture_started_at') or event.get('observed_at'))
            except (ValueError,KeyError,TypeError,zlib.error):continue
    return output


def match_quotes(cases,quotes):
    missing=Counter()
    validated=[]
    from .bookmakers import validate_snapshot
    for row in quotes:
        try:
            value=validate_snapshot(row)
            if row.get('_capture_at'):value['_capture_at']=row['_capture_at']
            if value['source']=='winline':validated.append(value)
        except (ValueError,TypeError,KeyError):continue
    quotes=validated
    for case in cases:
        try:
            start=instant(case['kickoff_at']);as_of=instant(case['forecast_at'])
            if as_of>=start:raise ValueError('no verified prematch as_of')
        except (ValueError,TypeError):missing['missing_prematch_as_of']+=1;continue
        candidates=[]
        for row in quotes:
            if (all(norm(row[k])==norm(case[k]) for k in ('home','away','competition')) and
                    abs((instant(row['kickoff_at'])-start).total_seconds())<=120 and
                    instant(row['observed_at'])<=as_of and
                    instant(row.get('_capture_at') or row['observed_at'])<=as_of):candidates.append(row)
        if not candidates:missing['missing_historical_odds']+=1;continue
        identities={row.get('source_event_id') or row['fixture_key'] for row in candidates}
        if len(identities)!=1:missing['ambiguous_quote_fixture']+=1;continue
        observed=max(instant(row['observed_at']) for row in candidates)
        latest=[row for row in candidates if instant(row['observed_at'])==observed]
        distinct={tuple(row['odds'][k] for k in KEYS) for row in latest}
        if len(distinct)!=1:missing['ambiguous_historical_odds']+=1;continue
        row=latest[0]
        case['market_snapshot']={'source':'winline','observed_at':row['observed_at'],'odds':row['odds']}
        case['predictions'].update(devig(row['odds']))
        if 'odds_shin' not in case['predictions']:missing['shin_underround_not_supported']+=1
    return dict(missing)


def history_predictions(rows,home,away):
    """Fixed Elo settings and league frequencies, using earlier training rows only."""
    rows=sorted((r for r in rows if r.get('same_competition',True)),key=lambda r:r['at'])
    counts=Counter('home' if r['goals'][0]>r['goals'][1] else 'away' if r['goals'][0]<r['goals'][1] else 'draw' for r in rows)
    prior={k:(counts[k]+1)/(len(rows)+3) for k in KEYS}
    output={'league_dirichlet':prior}
    ratings={};seen=Counter()
    for row in rows:
        h,a=row['home'],row['away'];rh,ra=ratings.get(h,1500),ratings.get(a,1500)
        expected=1/(1+10**(-(rh+50-ra)/400))
        gh,ga=row['goals'];actual=1 if gh>ga else .5 if gh==ga else 0
        change=20*(actual-expected)
        ratings[h]=rh+change;ratings[a]=ra-change;seen.update((h,a))
    if seen[home] and seen[away]:
        difference=max(-10,min(10,(ratings[home]+50-ratings[away])*math.log(10)/400))
        strength=math.exp(difference);draw=prior['draw'];nu=2*draw/(1-draw)
        output['elo_davidson']=distribution([strength,nu*math.sqrt(strength),1])
    return output,{'competition_matches':len(rows),'home_matches':seen[home],'away_matches':seen[away]}


def apply_history(cases,db):
    from .chances import football_sample
    from .snapshot_evaluation import ReadOnlyInputs
    if db is None:return {'server_history_not_provided':len(cases)}
    missing=Counter();tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'archive_latest_results' not in tables:return {'archive_history_table_missing':len(cases)}
    for case in cases:
        try:
            as_of=instant(case['forecast_at']);start=instant(case['kickoff_at'])
            if as_of>=start:raise ValueError('No prematch time')
            event={k:case[k] for k in ('home','away','competition','kickoff_at')}
            event['sport_label']='Футбол'
            rows=football_sample(ReadOnlyInputs(db),event,as_of)
            values,counts=history_predictions(rows,norm(case['home']),norm(case['away']))
            case['predictions'].update(values);case['history_training']=counts
            if 'elo_davidson' not in values:missing['elo_participant_history_missing']+=1
        except (ValueError,KeyError,TypeError,sqlite3.Error):missing['history_unavailable_at_forecast']+=1
    return dict(missing)


def compare(report,quotes=(),db=None):
    cases,missing=load_cases(report)
    if not cases:raise ValueError('Нет рассчитанных матчей для парного сравнения.')
    quote_errors=match_quotes(cases,quotes)
    history_errors=apply_history(cases,db)
    summaries=[]
    for key,label in METHODS.items():
        summary=measures(cases,key)
        summaries.append({'method':key,'label':label,'coverage':summary['matches'] if summary else 0,
                          'same_full_cohort':bool(summary and summary['matches']==len(cases)),'metrics':summary})
    market=[]
    for key,label in {**MARKET_METHODS,**HISTORY_METHODS}.items():
        subset=[row for row in cases if key in row['predictions']]
        market.append({'method':key,'label':label,'coverage':len(subset),'state':'available' if subset else 'unavailable',
                       'metrics':measures(subset,key),'paired_current_model':measures(subset,'poisson_v19'),
                       'paired_uniform':measures(subset,'uniform')})
    leaderboard=[s for s in summaries+market if s['coverage']==len(cases)]
    leaderboard.sort(key=lambda s:s['metrics']['log_loss'])
    return {'format':'matchlab.model-comparison.v1','generated_at':datetime.now(timezone.utc).isoformat(),
            'source_mode':report.get('mode'),'source_model':report.get('model_version'),
            'source_selected':len(report['matches']),'common_cases':len(cases),'source_unestimated':missing,
            'models':summaries,'leaderboard_by_log_loss':[s['method'] for s in leaderboard],
            'market_models':[row for row in market if row['method'] in MARKET_METHODS],
            'history_models':[row for row in market if row['method'] in HISTORY_METHODS],
            'market_exclusions':quote_errors,'history_exclusions':history_errors,'matches':cases,
            'production_model_changed':False,'out_of_sample_validated':False,
            'limitations':[
                'Исследовательское сравнение на уже известных результатах; лучшая строка не является подтверждённым победителем на новых матчах.',
                f'Все основные варианты сравниваются на одних и тех же исходно рассчитанных матчах; исходных пропусков {missing}, они не подменяются прогнозами.',
                'Диксон–Коулз и отрицательно-биномиальные варианты используют интенсивности, восстановленные из сохранённого Пуассона. Исходные сезонные показатели не переобучались.',
                'Параметры вариантов фиксированы и не оптимизируются по исходам этих матчей. Поправка Диксона–Коулза ограничивается только условием неотрицательных вероятностей.',
                'Сглаживание с равномерным ориентиром уменьшает уверенность, но сохраняет порядок исходов; процент угаданных фаворитов может не измениться.',
                'Шин и конвертация коэффициентов — рыночные ориентиры, не независимые спортивные прогнозы. Используются только сохранённые до времени прогноза полные коэффициенты 1X2.',
                'Метрики рыночных методов сравниваются с текущей моделью на их собственном общем подмножестве, а не с результатом на всех матчах.',
                'Elo–Davidson и частоты турнира требуют серверную историю, доступную к моменту прогноза. Elo: начальный рейтинг 1500, K=20, поправка хозяев 50; ничьи учитываются отдельно.',
                'Качество текста Qwen и доходность ставок не проверяются. Производственная модель автоматически не меняется.']}


def save_comparison(report,path):
    from .backtest import save_report
    path=Path(path);save_report(report,path)
    with path.with_suffix('.csv').open('w',newline='',encoding='utf-8-sig') as stream:
        writer=csv.writer(stream);writer.writerow(['method','matches','favorite_hits','favorite_matches','accuracy','brier_3way','log_loss'])
        for row in report['models']+report['market_models']+report.get('history_models',[]):
            metrics=row['metrics'] or {}
            writer.writerow([row['method'],row['coverage'],*[metrics.get(k) for k in ('favorite_hits','favorite_matches','accuracy','brier_3way','log_loss')]])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    prices=parser.add_mutually_exclusive_group()
    prices.add_argument('--database',type=Path,help='Read earlier match history and quotes in this SQLite database')
    prices.add_argument('--with-server-data','--with-server-quotes',dest='with_server_data',action='store_true',help='Read earlier match history and historical quotes from the configured server database')
    parser.add_argument('--output',type=Path,default=Path('data/evaluations/model-comparison.json'))
    args=parser.parse_args()
    try:
        raw=args.report.read_bytes()
        if len(raw)>32*1024*1024:raise ValueError('Отчёт больше 32 МиБ.')
        quotes=[]
        if args.with_server_data:
            from .config import Settings
            args.database=Settings.from_env().database
        if args.database:
            with sqlite3.connect('file:'+quote(str(args.database.resolve()))+'?mode=ro',uri=True) as db:
                db.row_factory=sqlite3.Row
                quotes=historical_quotes(db)
                result=compare(json.loads(raw),quotes,db)
        else:result=compare(json.loads(raw),quotes)
        result['input_sha256']=hashlib.sha256(raw).hexdigest()
        save_comparison(result,args.output)
    except (OSError,ValueError,sqlite3.Error) as error:parser.exit(1,f'Ошибка сравнения: {error}\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('matches',)},ensure_ascii=False,indent=2))
    print('JSON:',args.output,'CSV:',args.output.with_suffix('.csv'))


if __name__=='__main__':main()
