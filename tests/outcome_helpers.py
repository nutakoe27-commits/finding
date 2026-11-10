"""Frozen prematch decisions and archive outcomes for admin settlement tests."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json

from matchlab.bookmakers import make_snapshot
from matchlab.neural_verdict import VERSION


def forecast(repo,key='one',*,kickoff=None,at=None,decision='bet',preference='home',score='2:0',
             engine='qwen',version=VERSION,event=None):
    kickoff=kickoff or datetime.now(timezone.utc)-timedelta(days=2)
    at=at or kickoff-timedelta(minutes=10)
    e=deepcopy(event) if event else {'source_event_id':key,'home':'Альфа '+key,'away':'Бета '+key,
        'competition':'Тестовая лига','sport':'football','phase':'prematch','kickoff_at':kickoff.isoformat()}
    quote=make_snapshot('winline',e['competition'],e['home'],e['away'],e['kickoff_at'],
        min(at-timedelta(seconds=30),datetime.fromisoformat(e['kickoff_at'])-timedelta(seconds=30)).isoformat(),
        [2,3.2,3.5],'https://winline.ru/stavki/sport/futbol/123',external_id=e['source_event_id'])
    packet={'event':e,'as_of':(at-timedelta(seconds=2)).isoformat(),'quote':quote,'policy':{'force_skip':False},
        'unverified_user_notes':'PRIVATE_NOTES_DO_NOT_EXPORT','facts':[]}
    verdict={'version':version,'engine':engine,'preference':preference,'decision':decision,
        'selection':preference if decision=='bet' else None,'stake_percent':.25 if decision=='bet' else 0,
        'odds':quote['odds'].get(preference) if decision=='bet' else None,'data_forced_skip':False,
        'counterargument':{'text':'Встречный довод из сохранённого ответа.'},'market_context':{'text':'Сохранённая линия.'},
        'reason':{'code':'supporting_evidence' if decision=='bet' else 'conflicting_evidence','text':'Причина решения по данным до начала матча.'}}
    payload={'id':key,'generated_at':at.isoformat(),'event':e,'analysis_type':'neural_verdict',
        'context':{'event':e,'neural_input':packet,'neural_verdict':verdict,'ai_generation':{'state':'ready','elapsed_ms':1000},
            'team_statistics':{'observed_at':(at-timedelta(minutes=5)).isoformat(),'match_phase':'prematch'},
            'notes':'PRIVATE_CONTEXT_DO_NOT_EXPORT'},'text':'PRIVATE_FULL_PROMPT_DO_NOT_EXPORT'}
    repo.save_event_analysis(payload,'outcome-'+key,user_id='private-owner')
    if score is not None:archive_result(repo,e,score,key)
    return payload


def archive_result(repo,event,score,key):
    observed=datetime.now(timezone.utc).isoformat()
    value={'date':event['kickoff_at'],'member1':event['home'],'member2':event['away'],
        'championship':event['competition'],'score':score}
    with repo.connection() as db:
        db.execute('INSERT OR REPLACE INTO archive_latest_results VALUES (?,?,?,?,?,?,?,?,?,?)',
            (key,1,'Футбол',event['kickoff_at'],event['home'],event['away'],score,observed,observed,json.dumps(value)))


def seed_outcomes(repo,now=None):
    now=now or datetime.now(timezone.utc);saved=[]
    rows=[('bet-win','bet','home','2:0'),('bet-loss','bet','away','1:0'),
          ('skip-avoided','skip','home','0:0'),('skip-missed','skip','away','0:2'),
          ('no-preference','skip',None,'1:1'),('waiting','skip','home',None)]
    for i,(key,decision,preference,score) in enumerate(rows):
        saved.append(forecast(repo,key,kickoff=now-timedelta(days=6-i),decision=decision,preference=preference,score=score))
    first=saved[0]
    forecast(repo,'duplicate',event=first['event'],at=datetime.fromisoformat(first['generated_at'])+timedelta(seconds=20),score=None,preference='away')
    forecast(repo,'failed',kickoff=now-timedelta(days=2),decision='skip',preference=None,engine='model_error')
    late=now-timedelta(days=1)
    forecast(repo,'late',kickoff=late,at=late+timedelta(minutes=1))
    return saved
