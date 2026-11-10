"""Admin-only, read-only settlement of frozen neural decisions against the archive."""
from collections import Counter,defaultdict
from datetime import datetime,timedelta,timezone
import hashlib
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .bookmakers import instant
from .chances import norm

MSK=ZoneInfo('Europe/Moscow')
REPORT_NAME=re.compile(r'neural-verdict[\w.-]{0,160}\.json',re.ASCII)
MAX_REPORT_BYTES=8*1024*1024


def number(value,low,high):
    return type(value) in (int,float) and math.isfinite(value) and low<=value<=high


def report_files(directory):
    directory=Path(directory)
    if not directory.exists():return []
    files=[]
    for p in directory.glob('neural-verdict*.json'):
        try:
            if REPORT_NAME.fullmatch(p.name) and not p.is_symlink() and p.is_file() and 0<p.stat().st_size<=MAX_REPORT_BYTES:
                files.append({'id':p.name,'modified_at':datetime.fromtimestamp(p.stat().st_mtime,timezone.utc).isoformat()})
        except OSError:continue
    return sorted(files,key=lambda f:f['modified_at'],reverse=True)[:64]


def site_records(db):
    # Project only the public fixture and verdict. Notes and the full prompt are
    # never returned or parsed into the dashboard.
    for row in db.execute('''SELECT id,generated_at,json_extract(payload,
        '$.context.neural_input.event','$.context.event','$.event',
        '$.context.neural_verdict','$.context.ai_generation',
        '$.context.neural_input.as_of','$.context.neural_input.quote',
        '$.context.team_statistics.observed_at','$.context.team_statistics.match_phase',
        '$.context.neural_input.policy.force_skip') fields
        FROM event_analyses WHERE json_valid(payload)
        AND json_type(payload,'$.context.neural_verdict')='object'
        ORDER BY julianday(generated_at),id'''):
        try:
            e,context_event,public_event,v,g,as_of,q,stats_at,phase,forced=json.loads(row['fields'])
            yield {'id':row['id'],'event':e or context_event or public_event,'verdict':v,'generation':g or {},
                'forecast_at':row['generated_at'],'input_at':as_of,'quote':q,'statistics_at':stats_at,
                'statistics_phase':phase,'force_skip':forced,'recorded_at':row['generated_at']}
        except (TypeError,ValueError):continue


def trial_records(directory,filename):
    # The caller can select only a regular file from the fixed inventory.
    p=Path(directory)/filename
    if p.is_symlink() or not REPORT_NAME.fullmatch(filename) or p.parent.resolve()!=Path(directory).resolve():
        raise ValueError('Недоступный файл теста.')
    try:
        if p.stat().st_size>MAX_REPORT_BYTES:raise ValueError('Слишком большой файл теста.')
        report=json.loads(p.read_text())
    except (OSError,UnicodeError,json.JSONDecodeError) as exc:raise ValueError('Не удалось прочитать отчёт теста.') from exc
    if not isinstance(report,dict) or report.get('format')!='matchlab.neural-verdict-test.v1' or not isinstance(report.get('cases'),list):
        raise ValueError('Неподдерживаемый формат теста.')
    if len(report['cases'])>100:raise ValueError('В отчёте больше 100 матчей.')
    records=[]
    for i,c in enumerate(report['cases']):
        try:
            p=c['input'];g=c.get('generation') or {};v=c.get('verdict') or {}
            raw=json.dumps(p,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
            if hashlib.sha256(raw).hexdigest()!=c.get('input_sha256'):continue
            began=instant(p['as_of']);elapsed=g.get('elapsed_ms')
            if not number(elapsed,0,24*3600*1000):continue
            records.append({'id':f'trial-{i}','event':p['event'],'verdict':v,'generation':g,
                'forecast_at':(began+timedelta(milliseconds=elapsed)).isoformat(),'input_at':p['as_of'],
                'quote':p.get('quote'),'force_skip':p.get('policy',{}).get('force_skip'),
                'recorded_at':report.get('created_at')})
        except (KeyError,TypeError,ValueError,OverflowError):continue
    return records


def clean_reason(verdict):
    blocks=[]
    for key,label in (('counterargument','Встречный довод'),('market_context','Линия'),('reason','Причина решения')):
        value=verdict.get(key)
        if isinstance(value,dict) and isinstance(value.get('text'),str):blocks.append({'label':label,'text':value['text'][:600]})
    return blocks


def prepare_record(record,current):
    e=record['event'];v=record['verdict'];g=record['generation']
    if not isinstance(e,dict) or not isinstance(v,dict) or not isinstance(g,dict):return None,'invalid_record'
    try:
        kickoff=instant(e['kickoff_at']);at=instant(record['forecast_at']);input_at=instant(record['input_at'])
        if not all(isinstance(e.get(k),str) and e[k].strip() for k in ('home','away','competition')):return None,'invalid_record'
        if e.get('sport')!='football':return None,'other_sport'
        if e.get('phase')!='prematch' or at>=kickoff:return None,'not_prematch'
        if input_at>at or at>current:return None,'invalid_time'
        q=record.get('quote')
        if q:
            from .neural_verdict import quote_matches_event
            if not isinstance(q,dict) or not quote_matches_event(q,e):return None,'quote_event_mismatch'
            if instant(q['observed_at'])>input_at:return None,'future_price'
        if record.get('statistics_phase') in ('live','finished'):return None,'not_prematch'
        if record.get('statistics_at') and instant(record['statistics_at'])>at:return None,'future_statistics'
        if v.get('engine')!='qwen' or record.get('force_skip') or v.get('data_forced_skip'):
            return None,'system_or_model_skip'
        if g.get('state')!='ready':return None,'model_not_ready'
        if v.get('decision') not in ('bet','skip'):return None,'invalid_record'
        preference=v.get('preference')
        if preference not in ('home','away',None):return None,'invalid_record'
        if v['decision']=='bet' and (preference is None or v.get('selection')!=preference):return None,'invalid_record'
        identity=(norm(e['home']),norm(e['away']),norm(e['competition']),kickoff)
        if identity[0]==identity[1]:return None,'invalid_record'
        return {**record,'event':{k:e.get(k) for k in ('source_event_id','home','away','competition','kickoff_at')},
            'at':at,'kickoff':kickoff,'identity':identity,'version':str(v.get('version') or 'legacy')[:80]},None
    except (KeyError,ValueError,TypeError,OverflowError):return None,'invalid_record'


def paper_result(record,won):
    """Fixed initial bank 100, never compounded; skip has no virtual stake."""
    from .neural_verdict import quote_matches_event
    v=record['verdict'];q=record.get('quote') or {};s=v.get('selection');stake=v.get('stake_percent')
    try:
        price=q['odds'][s];quoted=instant(q['observed_at'])
        if not (quote_matches_event(q,record['event']) and 0<=(record['at']-quoted).total_seconds()<=180 and
            number(price,1.0000001,100000) and number(stake,.000001,.5) and number(v.get('odds'),1.0000001,100000) and
            math.isclose(price,v['odds'],rel_tol=1e-9)):
            return None,None,'invalid_or_expired_price'
        return stake,stake*(price-1) if won else -stake,None
    except (KeyError,ValueError,TypeError):return None,None,'missing_price'


def summarize(cases):
    completed=[c for c in cases if c['state']=='settled']
    predictions=[c for c in completed if c['preference_hit'] is not None]
    bets=[c for c in completed if c['decision']=='bet'];skips=[c for c in completed if c['decision']=='skip']
    priced=[c for c in bets if c['paper_profit'] is not None]
    staked=sum(c['paper_stake'] for c in priced);profit=sum(c['paper_profit'] for c in priced)
    hits=sum(c['preference_hit'] for c in predictions);wins=sum(c['bet_won'] for c in bets)
    return {'fixtures':len(cases),'settled':len(completed),'pending':sum(c['state']=='pending' for c in cases),
        'unresolved':sum(c['state']=='unresolved' for c in cases),'preferences':len(predictions),'hits':hits,
        'accuracy':hits/len(predictions) if predictions else None,'bets':len(bets),'bet_wins':wins,'bet_losses':len(bets)-wins,
        'bet_accuracy':wins/len(bets) if bets else None,'skips':len(skips),
        'skip_avoided_loss':sum(c['decision_result']=='avoided_loss' for c in skips),
        'skip_missed_win':sum(c['decision_result']=='missed_win' for c in skips),
        'skip_unassessed':sum(c['decision_result']=='no_preference' for c in skips),
        'bet_coverage':len(bets)/len(completed) if completed else None,
        'priced_bets':len(priced),'paper_staked':staked,'paper_profit':profit,'paper_bank':100+profit,
        'paper_roi':profit/staked if staked else None}


def run(repo,*,days=30,source='site',version='',report_id='',state='all',offset=0,limit=25,report_dir=None,now=None):
    if days not in (7,30,90) or source not in ('site','trials') or state not in ('all','settled','pending','unresolved','bet','skip'):
        raise ValueError('Некорректные фильтры результатов.')
    if not isinstance(version,str) or len(version)>80 or type(offset) is not int or not 0<=offset<=100000 or type(limit) is not int or not 1<=limit<=100:
        raise ValueError('Некорректная страница результатов.')
    current=now or datetime.now(timezone.utc);today=current.astimezone(MSK).date()
    dates=[today-timedelta(days=i) for i in range(days-1,-1,-1)]
    inventory=report_files(report_dir) if report_dir else [];selected_report=''
    if source=='trials' and inventory:
        selected_report=report_id or inventory[0]['id']
        if selected_report not in {f['id'] for f in inventory}:raise ValueError('Файл теста не найден.')
    elif source=='trials' and report_id:raise ValueError('Файл теста не найден.')
    excluded=Counter();prepared=[]
    with repo.connection() as db:
        records=site_records(db) if source=='site' else trial_records(report_dir,selected_report) if selected_report else []
        for raw in records:
            try:
                # Count exclusions only for fixtures in this visible period.
                date=instant(raw['event']['kickoff_at']).astimezone(MSK).date()
                if not dates[0]<=date<=today:continue
                row,why=prepare_record(raw,current)
                if why:excluded[why]+=1
                else:prepared.append(row)
            except (KeyError,TypeError,ValueError,OverflowError):excluded['invalid_record']+=1
        versions=sorted({r['version'] for r in prepared},reverse=True)
        cohort={}
        by_fixture=defaultdict(list)
        for row in sorted(prepared,key=lambda r:(r['at'],r['id'])):
            if version and row['version']!=version:continue
            if any(abs((old['kickoff']-row['kickoff']).total_seconds())<=120 for old in by_fixture[row['identity'][:3]]):
                excluded['duplicate_forecast']+=1;continue
            cohort[row['identity']]=row
            by_fixture[row['identity'][:3]].append(row)
        from .winline_backtest import results
        try:truth,invalid_results=results(db)
        except ValueError:truth=[];invalid_results={}
    by_identity=defaultdict(list)
    for t in truth:by_identity[(norm(t['home']),norm(t['away']),norm(t['competition']))].append(t)
    cases=[]
    for row in cohort.values():
        v=row['verdict'];e=row['event'];preference=v.get('preference')
        matches=[r for r in by_identity[row['identity'][:3]] if abs((r['at']-row['kickoff']).total_seconds())<=120]
        c={'id':row['id'],'event':e,'forecast_at':row['at'].isoformat(),'recorded_at':row.get('recorded_at'),
            'version':row['version'],'preference':preference,'preference_label':e.get(preference) if preference else None,
            'decision':v['decision'],'state':'pending','actual_score':None,'actual_outcome':None,
            'preference_hit':None,'bet_won':None,'decision_result':'pending','paper_stake':None,'paper_profit':None,
            'reason':clean_reason(v),'odds':v.get('odds') if v['decision']=='bet' else None}
        from .token_analytics import usage_summary
        c['token_usage']=usage_summary(row['generation'].get('token_usage'),model_calls=row['generation'].get('model_calls'))
        if len(matches)>1:c.update(state='unresolved',decision_result='ambiguous_result')
        elif len(matches)==1:
            result=matches[0];h,a=result['goals'];actual='home' if h>a else 'away' if a>h else 'draw'
            if row['at']>=result['at']:
                c.update(state='unresolved',decision_result='forecast_after_actual_kickoff');cases.append(c);continue
            c.update(state='settled',actual_score=f'{h}:{a}',actual_outcome=actual,
                preference_hit=preference==actual if preference else None,result_key=result['key'])
            if v['decision']=='bet':
                won=v['selection']==actual;c.update(bet_won=won,decision_result='bet_won' if won else 'bet_lost')
                stake,profit,error=paper_result(row,won)
                c.update(paper_stake=stake,paper_profit=profit,price_limitation=error)
            else:c['decision_result']='no_preference' if preference is None else 'missed_win' if preference==actual else 'avoided_loss'
        cases.append(c)
    cases.sort(key=lambda c:(instant(c['event']['kickoff_at']),c['id']))
    daily=[];cumulative=[];bank=100
    for date in dates:
        group=[c for c in cases if instant(c['event']['kickoff_at']).astimezone(MSK).date()==date]
        cumulative.extend(group);m=summarize(group);cm=summarize(cumulative);bank+=m['paper_profit']
        daily.append({'day':str(date),**m,'cumulative_accuracy':cm['accuracy'],'cumulative_preferences':cm['preferences'],
            'cumulative_bank':bank})
    filtered=[c for c in reversed(cases) if state=='all' or c['state']==state or c['decision']==state]
    return {'format':'matchlab.verdict-outcomes.v1','checked_at':current.isoformat(),'timezone':'Europe/Moscow','days':days,
        'source':source,'diagnostic_only':source=='trials','selection':'first_valid_prematch_forecast_per_fixture_in_selected_version',
        'version':version,'versions':versions,'reports':inventory,'report_id':selected_report,
        'metrics':summarize(cases),'daily':daily,'excluded':dict(excluded),'excluded_archive_results':invalid_results,
        'matches':filtered[offset:offset+limit],'total':len(filtered),'offset':offset,'limit':limit,
        'limitations':['Пропущенная победа — оценка по известному исходу, а не доказательство ошибки пропуска.',
            'Повторные разборы не увеличивают выборку: учитывается первый допустимый разбор матча в выбранной версии.',
            'Виртуальный банк начинается со 100; ставки фиксированы относительно начального банка, без реальных денег.',
            'Даты матчей и графики относятся к московскому времени. Неподтверждённые итоги не считаются попаданиями.']}
