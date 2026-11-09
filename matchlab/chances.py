"""Server-side probability estimates. Never expose fitted parameters in public reports."""
from datetime import datetime, timedelta, timezone
import json
import math
import re
import unicodedata

import numpy as np
from scipy.stats import poisson

from .bookmakers import instant
from .market_math import normalized_1x2


def norm(value):
    return ' '.join(unicodedata.normalize('NFKC',str(value or '')).casefold().split())


def plain(value, size=160):
    return re.sub(r'[\x00-\x1f\x7f*_`\\<>]', ' ', str(value or '')).strip()[:size]


def score(value):
    match=re.fullmatch(r'\s*(\d{1,2})\s*[:\-]\s*(\d{1,2})\s*',str(value or ''))
    if not match: return None
    pair=tuple(map(int,match.groups()))
    return pair if max(pair)<=30 else None


def full_time(market):
    text=norm(str(market.get('name') or '')+' '+str(market.get('period') or ''))
    return not re.search(r'тайм|период|четверт|сет|гейм|раунд|половин|half|quarter|period|set|углов|карточ|удар|офсайд|фол|пенальти|доп\.?\s*врем|овертайм|буллит|с учетом|с учётом|(?<!\w)[1-9]\s*[тпqh](?!\w)',text)


def football_sample(repo, event, as_of):
    """Same-competition completed plain football scores known before this fixture."""
    if not norm(event.get('competition')): return []
    cutoff=min(as_of,instant(event['kickoff_at'])) if event.get('kickoff_at') else as_of
    with repo.connection() as db:
        db.create_function('competition_key',1,norm)
        rows=db.execute('''SELECT payload,start_at FROM archive_latest_results
            WHERE sport_id=1 AND competition_key(sport_label)=? AND start_at<? AND start_at>=?
              AND observed_at<=? AND received_at<=? AND competition_key(json_extract(payload,'$.championship'))=?
            ORDER BY start_at DESC LIMIT 1500''',
            (norm(event.get('sport_label') or 'Футбол'),cutoff.isoformat(),(cutoff-timedelta(days=365)).isoformat(),
             as_of.isoformat(),as_of.isoformat(),norm(event.get('competition')))).fetchall()
    result=[]; seen=set()
    for row in rows:
        data=json.loads(row['payload']); pair=score(data.get('score'))
        if not pair: continue
        at=instant(row['start_at'])
        if at>=cutoff: continue
        h,a=norm(data.get('member1')),norm(data.get('member2'))
        key=(h,a,at.isoformat(),pair)
        if not h or not a or h==a or key in seen: continue
        seen.add(key)
        result.append({'home':h,'away':a,'goals':pair,'at':at})
    return result


def feed_sample(context, side, cutoff):
    """Only explicitly completed, ID-verified rows with documented score/time fields."""
    feed=(context.get('team_statistics') or {}).get('structured_feed') or {}
    target=((feed.get('match') or {}).get('teams') or {}).get(side) or {}
    team_id=str(target.get('_id',target.get('id','')))
    result=[]
    if not team_id: return result
    for group in (feed.get('participants') or {}).get(side,[]):
        for row in group.get('verified_matches',[]):
            state=row.get('status'); state=state.get('_id',state.get('id')) if isinstance(state,dict) else state
            if norm(state) not in ('finished','ended','closed','100'): continue
            identifier=str(row.get('_id',row.get('id','')))
            if identifier==str(feed.get('statistics_id')): continue
            raw_at=row.get('date') or row.get('start_at') or row.get('_dt')
            if isinstance(raw_at,dict): raw_at=raw_at.get('uts')
            try:
                at=datetime.fromtimestamp(raw_at,tz=timezone.utc) if type(raw_at) in (int,float) else instant(raw_at)
            except (TypeError,ValueError,OverflowError,OSError): continue
            if at>=cutoff: continue
            competition=row.get('competition') or row.get('tournament')
            if isinstance(competition,dict): competition=competition.get('name')
            if norm(competition)!=norm(context['event'].get('competition')): continue
            values=row.get('result')
            if not isinstance(values,dict): continue
            h,a=values.get('home'),values.get('away')
            if type(h) is not int or type(a) is not int or not 0<=min(h,a)<=max(h,a)<=30: continue
            teams=row.get('teams') or {}; positions=[]
            for position in ('home','away'):
                team=teams.get(position) or {}
                if str(team.get('_id',team.get('id','')))==team_id: positions.append(position)
            if len(positions)!=1: continue
            gf,ga=(h,a) if positions[0]=='home' else (a,h)
            result.append({'at':at,'gf':gf,'ga':ga})
    return result


def fit_goals(rows, home, away, at, extras=None):
    """Empirical-Bayes Gamma shrinkage of decayed attack/defence goal rates."""
    rows=list({(r['home'],r['away'],r['at'],tuple(r['goals'])):r for r in rows if r['at']<at}.values())
    if len(rows)<30: return None
    weights=[2**(-max(0,(at-r['at']).total_seconds())/(90*86400)) for r in rows]
    baseline=sum(w*sum(r['goals']) for w,r in zip(weights,rows))/(2*sum(weights))
    if not .15<=baseline<=8: return None
    samples=[]
    for side,name in zip(('home','away'),(home,away)):
        values=[]
        for row in rows:
            if name==row['home']: gf,ga=row['goals']
            elif name==row['away']: ga,gf=row['goals']
            else: continue
            values.append({'at':row['at'],'gf':gf,'ga':ga})
        values.extend(v for v in (extras or {}).get(side,[]) if v['at']<at)
        # A match observed through both archive and widget counts once.
        unique={(v['at'].replace(second=0,microsecond=0),v['gf'],v['ga']):v for v in values}
        values=sorted(unique.values(),key=lambda r:r['at'],reverse=True)[:30]
        if len(values)<5: return None
        w=[2**(-max(0,(at-v['at']).total_seconds())/(90*86400)) for v in values]
        if sum(w)<3: return None
        attack=(6*baseline+sum(x*v['gf'] for x,v in zip(w,values)))/(6+sum(w))
        defence=(6*baseline+sum(x*v['ga'] for x,v in zip(w,values)))/(6+sum(w))
        samples.append((attack,defence,values))
    home_rate=(6*baseline+sum(w*r['goals'][0] for w,r in zip(weights,rows)))/(6+sum(weights))
    away_rate=(6*baseline+sum(w*r['goals'][1] for w,r in zip(weights,rows)))/(6+sum(weights))
    venue=math.sqrt(home_rate/away_rate)
    lh=samples[0][0]*samples[1][1]/baseline*venue
    la=samples[1][0]*samples[0][1]/baseline/venue
    if not .02<min(lh,la)<=max(lh,la)<15: return None
    # Venue effect is estimated in this competition, not borrowed from another format.
    limit=max(12,int(poisson.ppf(1-1e-9,max(lh,la))))
    grid=np.outer(poisson.pmf(np.arange(limit+1),lh),poisson.pmf(np.arange(limit+1),la))
    grid/=grid.sum()
    facts=[]
    for name,(_,_,values) in zip((home,away),samples):
        wins=sum(v['gf']>v['ga'] for v in values); draws=sum(v['gf']==v['ga'] for v in values)
        facts.append({'team':name,'matches':len(values),'wins':wins,'draws':draws,'scored':sum(v['gf'] for v in values),'conceded':sum(v['ga'] for v in values)})
    return grid,facts


def goal_probabilities(market, grid):
    """Support explicitly named full-match goal markets; no inferred Asian rules."""
    if not full_time(market) or norm(market.get('period')) not in ('','осн.время','осн. время','основное время','матч','весь матч','full time','regular time'): return {}
    name=norm(market.get('name')); h,a=np.indices(grid.shape); out={}
    for item in market.get('outcomes',[]):
        label=norm(item.get('name')); mask=None; push=np.zeros(grid.shape,dtype=bool)
        if normalized_1x2(market):
            mask=(h>a) if label in ('1','п1') else (h==a) if label in ('x','х') else (h<a) if label in ('2','п2') else None
        elif name=='двойной шанс' and market.get('parameter') in (None,'') and item.get('parameter') in (None,''):
            mask=(h>=a) if label in ('1x','1х') else (h<=a) if label in ('x2','х2') else (h!=a) if label=='12' else None
        elif ('обе забьют' in name or 'обе команды забьют' in name):
            both=(h>0)&(a>0); mask=both if label in ('да','yes') else ~both if label in ('нет','no') else None
        elif re.fullmatch(r'тотал(?:\s*\(осн\.?\s*время\))?',name):
            value=item.get('parameter') or market.get('parameter')
            match=re.fullmatch(r'(б|м|больше|меньше|тб|тм)\s*([0-9]+(?:[.,][0-9]+)?)?',label)
            if match:
                try: threshold=float(str(value if value not in (None,'') else match[2]).replace(',','.'))
                except (ValueError,TypeError): continue
                if not 0<=threshold<=40 or threshold*2!=int(threshold*2): continue
                mask=h+a>threshold if match[1] in ('б','больше','тб') else h+a<threshold
                push=h+a==threshold
        elif 'точный счет' in name or 'точный счёт' in name:
            pair=score(label)
            if pair: mask=(h==pair[0])&(a==pair[1])
        elif name=='фора' and len(market.get('outcomes',[]))==2:
            match=re.fullmatch(r'(?:ф|фора\s*)?([12])\s*(?:\(([+\-]?[0-9]+(?:[.,][0-9]+)?)\))?',label)
            if not match: continue
            try: handicap=float(str(item.get('parameter') or match[2]).replace(',','.'))
            except (TypeError,ValueError): continue
            # Half-goal handicaps have no ambiguous refund/quarter-line settlement.
            if abs(handicap)%1!=.5: continue
            mask=(h-a+handicap>0) if match[1]=='1' else (a-h+handicap>0)
        if mask is not None:
            out[item['key']]={'probability':float(grid[mask].sum()),'push_probability':float(grid[push].sum()),'basis':'history'}
    return out


def market_probabilities(market):
    """Normalize only complete mutually exclusive sets; never double-chance triples."""
    distribution=normalized_1x2(market)
    if distribution:
        aliases={'1':'1','п1':'1','x':'X','х':'X','2':'2','п2':'2'}
        return {item['key']:{'probability':distribution['weights'][aliases[norm(item['name'])]],'basis':'market'} for item in market['outcomes']}
    outcomes=market.get('outcomes') or []
    if market.get('outcomes_complete') is not True or len(outcomes)!=2: return {}
    name=norm(market.get('name')); labels={norm(o.get('name')) for o in outcomes}
    winner=('победитель' in name or 'исход 12' in name) and labels in ({'п1','п2'},{'1','2'}) and market.get('parameter') in ('',None) and all(o.get('parameter') in ('',None) for o in outcomes)
    btts=('обе забьют' in name or 'обе команды забьют' in name) and labels in ({'да','нет'},{'yes','no'})
    if not (winner or btts): return {}
    if any(o.get('state')!='open' or type(o.get('odds')) not in (int,float) or not math.isfinite(o['odds']) or o['odds']<=1 for o in outcomes): return {}
    total=sum(1/o['odds'] for o in outcomes)
    return {o['key']:{'probability':(1/o['odds'])/total,'basis':'market'} for o in outcomes}


def season_facts(context):
    """Quote paired observed metrics; preserve filter scope and units verbatim."""
    event=context['event']; facts=[]; seen=set()
    for section in (context.get('team_statistics') or {}).get('sections',[]):
        if not norm(section.get('title')).startswith('статистика сезона') or section.get('truncated'): continue
        pattern=r'(\d+(?:[.,]\d+)?)\s*(%?)\s+(Победы|Забитые мячи|Голов пропущен[оo]|Сухие матчи)\s+(\d+(?:[.,]\d+)?)\s*(%?)'
        filters=' · '.join(plain(f,35) for f in section.get('filters',[]))
        for match in re.finditer(pattern,section.get('text',''),re.I):
            h,hu,label,a,au=match.groups()
            if hu!=au: continue
            identity=(norm(label),filters,h,a)
            if identity in seen: continue
            seen.add(identity)
            facts.append(f"Статистика сезона{(' ['+filters+']') if filters else ''}: {plain(label).lower()} — {plain(event.get('home'),55)} {h}{hu}, {plain(event.get('away'),55)} {a}{au}")
            if len(facts)==4:return facts
    return facts


def assess(repo, document, context):
    """Public predictions/facts only. Fitted intensities and hyperparameters stay local."""
    event=context['event']; now=instant(context['request_time']['at']); stats=context.get('team_statistics') or {}
    finished=event.get('phase')=='finished' or stats.get('match_phase')=='finished'
    grid=None; facts=[]; limitations=[]; native=False
    if not finished and event.get('kickoff_at') and event.get('sport')=='football' and event.get('phase')=='prematch' and not context['request_time']['kickoff_passed'] and not context['snapshot']['is_stale']:
        rows=football_sample(repo,event,now)
        cutoff=min(now,instant(event['kickoff_at'])) if event.get('kickoff_at') else now
        fit=fit_goals(rows,norm(event.get('home')),norm(event.get('away')),cutoff,
                      {s:feed_sample(context,s,cutoff) for s in ('home','away')})
        if fit:
            grid,summaries=fit; native=True
            for side,row in zip(('home','away'),summaries):
                facts.append(f"{plain(event.get(side))}: {row['wins']} побед и {row['draws']} ничьих за {row['matches']} прошлых матчей; мячи {row['scored']}:{row['conceded']}")
    if finished: limitations.append('Матч уже завершён; прогноз до начала не формируется.')
    if event.get('phase')=='live':
        if event.get('live_score'): facts.append('Счёт в снимке: '+plain(event['live_score'])+(' · '+plain(event.get('live_clock')) if event.get('live_clock') else ''))
        limitations.append('Для live независимая оценка пока не применяется; указаны оценки по линии.')
    facts.extend(season_facts(context))
    evaluated=[]; winner=None; unsupported=0
    for market in context['markets']:
        independent=goal_probabilities(market,grid) if grid is not None else {}
        estimated=independent or market_probabilities(market)
        if not estimated: unsupported+=1
        for item in market['outcomes']:
            estimate=estimated.get(item['key'])
            if not estimate or item.get('state')!='open' or market.get('is_stale'): continue
            record={'market_key':market['key'],'outcome_key':item['key'],'market':market['name'],'period':market.get('period'),
                    'parameter':item.get('parameter') or market.get('parameter'),'outcome':item['name'],'odds':item.get('odds'),**estimate}
            evaluated.append(record)
        if winner is None and event.get('home') and event.get('away') and full_time(market) and (normalized_1x2(market) or ('победитель' in norm(market['name']) and len(market['outcomes'])==2)):
            candidates=[r for r in evaluated if r['market_key']==market['key']]
            if candidates: winner=max(candidates,key=lambda r:r['probability'])
    if finished: evaluated=[]; winner=None
    if winner:
        label=norm(winner['outcome']); winner={**winner,'participant':event.get('home') if label in ('1','п1') else event.get('away') if label in ('2','п2') else 'Ничья'}
    if not native and not finished and event.get('phase')!='live': limitations.append('Недостаточно подходящей истории для независимого расчёта; проценты рассчитаны по линии.')
    if native: limitations.append('Оценка ещё не прошла проверку калибровки на будущих матчах.')
    if unsupported: limitations.append('Для части рынков вероятность не рассчитана: условия или модель не поддерживаются.')
    if context['snapshot']['is_stale']: limitations.append('Снимок матча устарел.')
    if not context['snapshot']['complete']: limitations.append('Источник передал неполный набор рынков.')
    if stats.get('capture_kind')=='fast' or stats.get('sections_truncated') or stats.get('season_filters_complete') is False:
        limitations.append('Подробная статистика получена частично.')
    if stats.get('is_stale'): limitations.append('Сохранённая статистика участников устарела.')
    preparation=context.get('statistics_preparation') or {}
    if preparation and preparation.get('state')!='ok': limitations.append('Обновить статистику не удалось; использованы сохранённые данные.')
    if not facts:
        history=context.get('archive_history') or {}
        if history.get('state')=='ready':
            seen=set()
            for team in history.get('teams',[]):
                for row in team.get('recent_results',[])[:1]:
                    key=row.get('result_key')
                    if not key or key in seen: continue
                    seen.add(key)
                    facts.append('Архив: '+' — '.join(plain(n,60) for n in row.get('members',[]))+' · '+plain(row.get('score_text'),30))
    if not facts:
        if winner: facts.append(f"По котировкам наиболее вероятен исход «{plain(winner['outcome'])}» в рынке «{plain(winner['market'])}»")
        else: facts.append('В доступных данных нет подтверждённого основания выбрать победителя')
    return {'winner':winner,'markets':evaluated,'facts':facts,'limitations':limitations,'finished':finished,
            'coverage':{'received_markets':len(context['markets']),'evaluated_markets':len({r['market_key'] for r in evaluated})}}


def render_report(assessment, indices=None, extra_limit=None):
    def pct(value): return '>99,9%' if value>.999 else f'{value*100:.1f}'.replace('.',',')+'%'
    def label(record):
        parts=[record['market'],record.get('period'),record.get('parameter'),record['outcome']]
        return ' · '.join(dict.fromkeys(plain(p,70) for p in parts if p))
    winner=assessment.get('winner')
    if winner:
        basis='по линии' if winner['basis']=='market' else 'оценка'
        first=f"Победитель: {plain(winner.get('participant'))} — {pct(winner['probability'])} ({basis}; {plain(winner['market'])}{' · '+plain(winner['period']) if winner.get('period') else ''})."
    else: first='Победитель: '+('прогноз не формируется — матч завершён.' if assessment.get('finished') else 'оценить по доступным данным нельзя.')
    candidates=sorted(assessment['markets'],key=lambda r:(r['basis']!='history',-r['probability']))
    chosen=[]; seen=set()
    for row in candidates:
        if row['probability']<.55 or (row.get('odds') or 0)<1.15 or row['market_key'] in seen: continue
        chosen.append(row); seen.add(row['market_key'])
        if len(chosen)==3: break
    second='Победные рынки (оценка): '+('; '.join(f"{label(r)} — {pct(r['probability'])}{' по линии' if r['basis']=='market' else ''}{' (возврат '+pct(r['push_probability'])+')' if r.get('push_probability') else ''}" for r in chosen) if chosen else 'подходящих вариантов для оценки не найдено')+'.'
    bank=assessment['facts']; indices=list(range(min(3,len(bank)))) if indices is None else indices
    third='Аргументы: '+'; '.join(plain(bank[i],220) for i in dict.fromkeys(indices) if type(i) is int and 0<=i<len(bank)) + '.'
    limits=list(dict.fromkeys(assessment['limitations']+([extra_limit] if extra_limit else [])))
    return '\n\n'.join([first,second,third]+(['Ограничения: '+' '.join(limits)] if limits else []))
