"""A transparent editorial carousel, using actual collected Winline events."""
from datetime import datetime,timezone
import re

from .bookmakers import instant

MAJOR=('лига чемпионов','лига европы','премьер-лига','премьер лига','ла лига','серия а','бундеслига','чемпионат мира','кубок мира')


def select(events,*,limit=10,now=None):
    current=now or datetime.now(timezone.utc);candidates={}
    for e in events:
        try:
            if not re.fullmatch(r'\d{1,20}',str(e.get('source_event_id',''))):continue
            if not e.get('home') or not e.get('away') or e.get('event_kind','match')!='match':continue
            if e.get('phase') not in ('prematch','live'):continue
            at=instant(e['kickoff_at']) if e.get('kickoff_at') else None
            if e['phase']=='prematch' and (at is None or at<=current):continue
            competition=e.get('competition','').casefold()
            major=any(term in competition for term in MAJOR) and not any(t in competition for t in ('люб.','кибер','5х5'))
            distance=max(0,(at-current).total_seconds()) if at else float('inf')
            # First freshness and proximity, then named major competitions. This
            # is not a claim about Winline's popularity or recommendation odds.
            rank=(bool(e.get('is_stale')),distance>86400,not major,e['phase']!='live',distance,str(e['source_event_id']))
            key=(e.get('source_kind','legacy'),e['source_event_id'])
            if key not in candidates or rank<candidates[key][0]:candidates[key]=(rank,e)
        except (ValueError,TypeError,AttributeError):continue
    ordered=[e for _,e in sorted(candidates.values(),key=lambda pair:pair[0])]
    chosen=[];counts={}
    for e in ordered:
        key=(e.get('sport'),e.get('competition'))
        if counts.get(key,0)>=2:continue
        chosen.append(e);counts[key]=counts.get(key,0)+1
        if len(chosen)>=limit:return chosen
    used={id(e) for e in chosen}
    return (chosen+[e for e in ordered if id(e) not in used])[:limit]


def collection(repo,*,sport='',phase='',limit=10):
    from . import archive
    query={'include_started':True,'sport':sport,'phase':phase,'limit':10000,'offset':0,'sort':'kickoff'}
    if archive.overview(repo)['available']:data=archive.catalog(repo,**query)
    else:
        data=repo.market_events(**query);current=datetime.now(timezone.utc)
        for e in data['events']:
            e['source_kind']='legacy'
            try:
                e['age_seconds']=max(0,int((current-instant(e['observed_at'])).total_seconds()))
                e['is_stale']=e['age_seconds']>(45 if e['phase']=='live' else 180)
            except (KeyError,TypeError,ValueError):e['is_stale']=True
    events=select(data['events'],limit=limit)
    return {'format':'matchlab.featured-events.v1','events':events,
        'selection':'editorial_fresh_nearby_major_competitions','source_kind':data.get('source_kind','legacy')}
