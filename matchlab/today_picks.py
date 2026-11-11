"""Anonymous system betting forecasts for today's and tomorrow's fixtures (MSK)."""
from datetime import datetime,timedelta,timezone
import math
import re

from fastapi import HTTPException

from .bookmakers import instant
from .verdict_outcomes import MSK,site_records,prepare_record,number
from .verdict_sports import PROFILES,prices_valid


def preview(item):
    # Participant names, selection and answer are fetched only on opening.
    return {key:item[key] for key in ('id','sport_label','forecast_at','quote_observed_at','origin','decision','odds')}|{
        'event':{key:item['event'][key] for key in ('source_event_id','sport','kickoff_at')}}


def collection(repo,*,min_odds=None,max_odds=None,now=None,include_started=False):
    for value in (min_odds,max_odds):
        if value is not None and not number(value,1.0000001,100000):raise ValueError('Коэффициент должен быть больше 1 и не больше 100 000.')
    if min_odds is not None and max_odds is not None and min_odds>max_odds:raise ValueError('Коэффициент «от» должен быть не больше коэффициента «до».')
    current=now or datetime.now(timezone.utc);today=current.astimezone(MSK).date();tomorrow=today+timedelta(days=1);items=[];seen=set()
    with repo.connection() as db:
        for raw in site_records(db):
            if raw.get('origin')!='system' or not re.fullmatch(r'[a-f0-9]{32}',raw['id']):continue
            row,why=prepare_record(raw,current)
            if why:continue
            if row['verdict'].get('decision')!='bet':continue
            if not today<=row['kickoff'].astimezone(MSK).date()<=tomorrow or (not include_started and row['kickoff']<=current):continue
            event=row['event'];quote=row.get('quote') or {};verdict=row['verdict'];side=verdict.get('preference')
            if not include_started:
                source=db.execute('''SELECT r.payload FROM archive_feed_rows r JOIN archive_feed f ON r.cycle=f.cycle
                    WHERE r.event_id=?''',(event['source_event_id'],)).fetchone()
                if source:
                    from .archive import unpacked
                    value=unpacked(source['payload'])
                    if value.get('is_live'):continue
                    if value.get('kickoff_at') and abs((instant(value['kickoff_at'])-row['kickoff']).total_seconds())>120:continue
                legacy=db.execute('SELECT phase FROM bookmaker_event_markets WHERE event_id=?',(event['source_event_id'],)).fetchone()
                if legacy and legacy['phase'] in ('live','finished'):continue
            try:
                if not prices_valid(quote['odds'],event['sport']) or not 0<=(row['at']-instant(quote['observed_at'])).total_seconds()<=180:continue
                price=quote['odds'].get(side) if side else None
                if verdict['decision']=='bet' and (not number(verdict.get('stake_percent'),.000001,.5) or not number(verdict.get('odds'),1.0000001,100000) or not math.isclose(price,verdict['odds'],rel_tol=1e-9)):continue
            except (KeyError,ValueError,TypeError):continue
            identity=(event['sport'],event['source_event_id'])
            if identity in seen:continue
            seen.add(identity)
            items.append({'id':row['id'],'event':event,'sport_label':PROFILES[event['sport']].label,
                'market_label':PROFILES[event['sport']].market_label,'forecast_at':row['at'].isoformat(),
                'quote_observed_at':quote['observed_at'],'origin':'system','decision':verdict['decision'],
                'selection_label':event.get(side) if side else 'Никто','odds':price,'stake_percent':verdict.get('stake_percent'),
                'state':'pending','bet_won':None,'actual_score':None,'today_pick':True,
                'started':row['kickoff']<=current})
    available=len(items)
    items=[item for item in items if
        (min_odds is None or item['odds'] is not None and item['odds']>=min_odds) and
        (max_odds is None or item['odds'] is not None and item['odds']<=max_odds)]
    items.sort(key=lambda item:(instant(item['event']['kickoff_at']),item['id']))
    return {'format':'matchlab.today-picks.v1','date':today.isoformat(),'through_date':tomorrow.isoformat(),'timezone':'Europe/Moscow','checked_at':current.isoformat(),
        'min_odds':min_odds,'max_odds':max_odds,'decision':'bet','available':available,'total':len(items),'picks':items}


def detail(repo,analysis_id,*,now=None):
    if not re.fullmatch(r'[a-f0-9]{32}',analysis_id):raise HTTPException(404,'Прогноз не найден.')
    row=next((item for item in collection(repo,now=now,include_started=True)['picks'] if item['id']==analysis_id),None)
    if row is None:raise HTTPException(404,'Этот ответ не входит в публичную историю или подборку на сегодня и завтра.')
    from .public_results import answer_blocks
    return {**row,**answer_blocks(repo,analysis_id)}
