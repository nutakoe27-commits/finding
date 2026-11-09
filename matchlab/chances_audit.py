"""Read-only forward evaluation of forecasts saved before kickoff, against later results."""
import json
import math
import sqlite3
from urllib.parse import quote

from .bookmakers import instant
from .chances import norm,score,full_time


def evaluate(db):
    observations=[]; seen=set(); pending=0
    for row in db.execute('SELECT payload FROM event_analyses ORDER BY generated_at'):
        saved=json.loads(row[0]); context=saved.get('context') or {}; event=context.get('event') or {}
        assessment=context.get('probability_assessment') or {}
        if not event.get('kickoff_at') or event.get('sport')!='football' or event.get('phase')!='prematch': continue
        if instant(saved['generated_at'])>=instant(event['kickoff_at']): continue
        identity=(event.get('source_event_id'),event['kickoff_at'])
        if identity in seen: continue
        # Only the independent full-match 1X2 estimate is audited here.
        groups={}
        for estimate in assessment.get('markets',[]):
            if estimate.get('basis')!='history' or not full_time({'name':estimate['market'],'period':estimate.get('period')}): continue
            label={'1':'home','п1':'home','x':'draw','х':'draw','2':'away','п2':'away'}.get(norm(estimate['outcome']))
            if label: groups.setdefault(estimate['market_key'],{})[label]=estimate['probability']
        probabilities=next((g for g in groups.values() if set(g)=={'home','draw','away'}),None)
        if not probabilities: continue
        seen.add(identity)
        candidates=db.execute('''SELECT payload FROM archive_latest_results
            WHERE sport_id=1 AND abs(julianday(start_at)-julianday(?))*86400<120''',(event['kickoff_at'],)).fetchall()
        results=[]
        for candidate in candidates:
            result=json.loads(candidate[0])
            if all(norm(result.get(k))==norm(event.get(t)) for k,t in [('member1','home'),('member2','away'),('championship','competition')]):
                pair=score(result.get('score'))
                if pair: results.append(pair)
        if len(results)!=1: pending+=1;continue
        h,a=results[0]; outcome='home' if h>a else 'away' if a>h else 'draw'
        observations.append((probabilities,outcome))
    if not observations:return {'evaluated':0,'pending':pending,'state':'awaiting_finished_forecasts'}
    brier=sum(sum((p[k]-(k==y))**2 for k in ('home','draw','away')) for p,y in observations)/len(observations)
    logloss=-sum(math.log(max(1e-12,p[y])) for p,y in observations)/len(observations)
    bins=[]
    for start in range(0,100,10):
        values=[(max(p.values()),max(p,key=p.get)==y) for p,y in observations if start/100<=max(p.values())<min(1.000001,(start+10)/100)]
        if values: bins.append({'from':start,'to':start+10,'n':len(values),'mean_forecast':sum(v[0] for v in values)/len(values),'observed_frequency':sum(v[1] for v in values)/len(values)})
    return {'evaluated':len(observations),'pending':pending,'brier_3way':brier,'log_loss':logloss,'favorite_calibration':bins,
            'scope':'first saved independent prematch forecast per fixture; no live predictions; no duplicate users or snapshots',
            'state':'small_sample' if len(observations)<100 else 'available'}


def main():
    from .config import Settings
    settings=Settings.from_env()
    uri='file:'+quote(str(settings.database.resolve()))+'?mode=ro'
    with sqlite3.connect(uri,uri=True) as db: report=evaluate(db)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
