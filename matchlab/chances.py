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
    """Competition baselines and both participants' completed results before this fixture."""
    if not norm(event.get('competition')): return []
    cutoff=min(as_of,instant(event['kickoff_at'])) if event.get('kickoff_at') else as_of
    with repo.connection() as db:
        db.create_function('competition_key',1,norm)
        rows=db.execute('''SELECT payload,start_at FROM archive_latest_results
            WHERE sport_id=1 AND competition_key(sport_label)=? AND start_at<?
              AND observed_at<=? AND received_at<=? AND (competition_key(json_extract(payload,'$.championship'))=?
              OR competition_key(member1) IN (?,?) OR competition_key(member2) IN (?,?))
            ORDER BY start_at DESC LIMIT 1500''',
            (norm(event.get('sport_label') or 'Футбол'),cutoff.isoformat(),
             as_of.isoformat(),as_of.isoformat(),norm(event.get('competition')),
             norm(event.get('home')),norm(event.get('away')),norm(event.get('home')),norm(event.get('away')))).fetchall()
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
        same=norm(data.get('championship'))==norm(event.get('competition'))
        result.append({'home':h,'away':a,'goals':pair,'at':at,'same_competition':same,'scope_weight':1 if same else .5})
    return result


def feed_sample(context, side, cutoff):
    from .match_history import normalize
    return normalize(context,cutoff)['samples'][side]


def is_1x2(market):
    """Recognize settlement conditions without reading any prices."""
    return (market.get('outcomes_complete') is True and market.get('parameter') in (None,'') and
            bool(re.search(r'(?<!\w)1\s*[xх]\s*2(?!\w)',norm(market.get('name')))) and
            not re.search(r'фора|гандикап|handicap',norm(market.get('name'))) and
            len(market.get('outcomes',[]))==3 and
            {norm(o.get('name')) for o in market['outcomes']} in ({'1','x','2'},{'1','х','2'},{'п1','x','п2'},{'п1','х','п2'}) and
            all(o.get('parameter') in (None,'') for o in market['outcomes']))


def fit_goals(rows, home, away, at, extras=None, season=None):
    """Sparse empirical-Bayes goal rates; use observed season means as a prior."""
    season=season or {}
    rows=list({(r['home'],r['away'],r['at'],tuple(r['goals'])):r for r in rows if r['at']<at}.values())
    reference=[r for r in rows if r.get('same_competition',True)]
    rates=[v[k] for v in season.values() for k in ('for','against')]
    if not reference and not rates:reference=rows
    weights=[2**(-max(0,(at-r['at']).total_seconds())/(90*86400))*r.get('scope_weight',1) for r in reference]
    if reference and sum(weights)>0:baseline=sum(w*sum(r['goals']) for w,r in zip(weights,reference))/(2*sum(weights))
    elif rates:baseline=sum(rates)/len(rates)
    else:return None
    if not 0<baseline<=15:return None
    # Weak positive regularization prevents a tiny zero-goal sample implying certainty.
    baseline=max(.15,baseline)
    samples=[]
    for side,name in zip(('home','away'),(home,away)):
        values=[]
        for row in rows:
            if name==row['home']:gf,ga=row['goals']
            elif name==row['away']:ga,gf=row['goals']
            else:continue
            values.append({'at':row['at'],'gf':gf,'ga':ga,'scope_weight':row.get('scope_weight',1)})
        values.extend(v for v in (extras or {}).get(side,[]) if v['at']<at)
        unique={(v['at'].replace(second=0,microsecond=0),v['gf'],v['ga']):v for v in values}
        values=sorted(unique.values(),key=lambda r:r['at'],reverse=True)
        prior=season.get(side)
        if not values and not prior:return None
        w=[2**(-max(0,(at-v['at']).total_seconds())/(90*86400))*v.get('scope_weight',1) for v in values]
        attack_prior=prior['for'] if prior else baseline
        defence_prior=prior['against'] if prior else baseline
        strength=min(6,prior.get('matches') or 1) if prior else 6
        # Venue rates supplement the overall prior without counting games twice.
        if prior and prior.get('venue'):
            attack_prior=(6*attack_prior+prior['venue']['for'])/7
            defence_prior=(6*defence_prior+prior['venue']['against'])/7
        attack=(strength*max(.02,attack_prior)+sum(x*v['gf'] for x,v in zip(w,values)))/(strength+sum(w))
        defence=(strength*max(.02,defence_prior)+sum(x*v['ga'] for x,v in zip(w,values)))/(strength+sum(w))
        samples.append((attack,defence,values,prior,sum(w)))
    venue=1.0
    if reference and sum(weights)>0:
        home_rate=(6*baseline+sum(w*r['goals'][0] for w,r in zip(weights,reference)))/(6+sum(weights))
        away_rate=(6*baseline+sum(w*r['goals'][1] for w,r in zip(weights,reference)))/(6+sum(weights))
        venue=math.sqrt(home_rate/away_rate)
    lh=samples[0][0]*samples[1][1]/baseline*venue
    la=samples[1][0]*samples[0][1]/baseline/venue
    if not .001<min(lh,la)<=max(lh,la)<30:return None
    limit=max(12,int(poisson.ppf(1-1e-9,max(lh,la))))
    grid=np.outer(poisson.pmf(np.arange(limit+1),lh),poisson.pmf(np.arange(limit+1),la));grid/=grid.sum()
    facts=[]
    for name,(_,_,values,prior,effective) in zip((home,away),samples):
        facts.append({'team':name,'source':'history' if values else 'season','matches':len(values),
            'wins':sum(v['gf']>v['ga'] for v in values),'draws':sum(v['gf']==v['ga'] for v in values),
            'scored':sum(v['gf'] for v in values),'conceded':sum(v['ga'] for v in values),'season':prior,'effective_matches':effective})
    return grid,facts


def goal_probabilities(market, grid, event=None):
    """Support explicitly named full-match goal markets; no inferred Asian rules."""
    period=norm(market.get('period'))
    if re.fullmatch(r'[12]\s*(?:-?й)?\s*тайм',period):
        indices=np.indices(grid.shape)
        means=[float((indices[i]*grid).sum())/2 for i in (0,1)]
        half=np.outer(poisson.pmf(np.arange(grid.shape[0]),means[0]),poisson.pmf(np.arange(grid.shape[1]),means[1]));half/=half.sum()
        name=re.sub(r'\b[12]\s*(?:-?й)?\s*тайм\s*','',market['name'],flags=re.I).strip()
        return goal_probabilities({**market,'name':name,'period':'осн.время'},half,event)
    if not full_time(market) or norm(market.get('period')) not in ('','осн.время','осн. время','основное время','матч','весь матч','full time','regular time'): return {}
    name=norm(market.get('name')); h,a=np.indices(grid.shape); out={}
    from .market_focus import family,row_kind
    kind=family(market,event);matrix=row_kind(market)
    for item in market.get('outcomes',[]):
        label=norm(item.get('name')); mask=None; push=np.zeros(grid.shape,dtype=bool)
        if is_1x2(market):
            mask=(h>a) if label in ('1','п1') else (h==a) if label in ('x','х') else (h<a) if label in ('2','п2') else None
        elif name=='двойной шанс' and market.get('parameter') in (None,'') and item.get('parameter') in (None,''):
            mask=(h>=a) if label in ('1x','1х') else (h<=a) if label in ('x2','х2') else (h!=a) if label=='12' else None
        elif ('обе забьют' in name or 'обе команды забьют' in name):
            both=(h>0)&(a>0); mask=both if label in ('да','yes') else ~both if label in ('нет','no') else None
        elif name in ('чет/нечет','чёт/нечёт','чет/нечёт','чёт/нечет'):
            mask=(h+a)%2==0 if label in ('чет','чёт','even') else (h+a)%2==1 if label in ('нечет','нечёт','odd') else None
        elif kind in ('total','team_total_home','team_total_away'):
            goals=h if kind=='team_total_home' else a if kind=='team_total_away' else h+a
            value=item.get('parameter') or market.get('parameter')
            match=re.fullmatch(r'(б|м|больше|меньше|тб|тм)\s*([0-9]+(?:[.,][0-9]+)?)?',label)
            if matrix=='total':
                direction=norm(market.get('parameter'))
                match=re.fullmatch(r'(больше|меньше|over|under)\s*([0-9]+(?:[.,][0-9]+)?)',direction+' '+label)
                value=None
            if match:
                try: threshold=float(str(value if value not in (None,'') else match[2]).replace(',','.'))
                except (ValueError,TypeError): continue
                if not 0<=threshold<=40 or threshold*2!=int(threshold*2): continue
                mask=goals>threshold if match[1] in ('б','больше','тб','over') else goals<threshold
                push=goals==threshold
        elif 'точный счет' in name or 'точный счёт' in name:
            pair=score(label)
            if pair: mask=(h==pair[0])&(a==pair[1])
        elif kind=='draw_no_bet' and market.get('parameter') in (None,''):
            mask=h>a if label in ('1','п1') else h<a if label in ('2','п2') else None
            push=h==a
        elif kind=='handicap' and matrix=='handicap' and event:
            side=next((s for s in ('home','away') if norm(event.get(s))==norm(market.get('parameter'))),None)
            try:handicap=float(label.replace(',','.'))
            except (ValueError,TypeError):continue
            if side is None or abs(handicap)%1!=.5:continue
            mask=(h-a+handicap>0) if side=='home' else (a-h+handicap>0)
        elif name=='фора' and len(market.get('outcomes',[]))==2:
            match=re.fullmatch(r'(?:п|ф|фора\s*)?([12])\s*(?:\(([+\-]?[0-9]+(?:[.,][0-9]+)?)\))?',label)
            if not match: continue
            try: handicap=float(str(item.get('parameter') or match[2]).replace(',','.'))
            except (TypeError,ValueError): continue
            # Half-goal handicaps have no ambiguous refund/quarter-line settlement.
            if abs(handicap)%1 not in (0,.5): continue
            mask=(h-a+handicap>0) if match[1]=='1' else (a-h+handicap>0)
            push=(h-a+handicap==0) if match[1]=='1' else (a-h+handicap==0)
        if mask is not None:
            out[item['key']]={'probability':float(grid[mask].sum()),'push_probability':float(grid[push].sum()),'basis':'history'}
    return out


def market_probabilities(market):
    """Legacy descriptive utility; never used by independent match analysis."""
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
    sections=(context.get('team_statistics') or {}).get('sections',[])
    ordered=sorted(sections,key=lambda s:(0 if 'За матч' in s.get('filters',[]) else 1 if 'Итоги' in s.get('filters',[]) else 2,
                                         0 if 'Общее' in s.get('filters',[]) else 1))
    for section in ordered:
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
            if len(facts)==12:return facts
    return facts


def assess(repo, document, context):
    """Public predictions/facts only. Fitted intensities and hyperparameters stay local."""
    event=context['event']; now=instant(context['request_time']['at']); stats=context.get('team_statistics') or {}
    finished=event.get('phase')=='finished' or stats.get('match_phase')=='finished'
    grid=None; facts=[]; limitations=[]; native=False;history_counts={}
    if not finished and event.get('kickoff_at') and event.get('sport')=='football' and event.get('phase')=='prematch' and not context['request_time']['kickoff_passed']:
        rows=football_sample(repo,event,now)
        cutoff=min(now,instant(event['kickoff_at'])) if event.get('kickoff_at') else now
        from .match_history import normalize
        feed=normalize(context,cutoff,rows,broad=True)
        from .season_evidence import season_rates
        rates=season_rates(context)
        history_counts={'archive_matches':len(rows),'widget':feed['counts'],'season_rates_available':list(rates)}
        fit=fit_goals(rows+feed['rows'],norm(event.get('home')),norm(event.get('away')),cutoff,feed['samples'],rates)
        if fit:
            grid,summaries=fit; native=True
            for side,row in zip(('home','away'),summaries):
                if row['source']=='history':
                    facts.append(f"{plain(event.get(side))}: {row['wins']} побед и {row['draws']} ничьих за {row['matches']} прошлых матчей; мячи {row['scored']}:{row['conceded']}")
                else:
                    rate=row['season'];scope='общее' if rate['scope']=='overall' else 'хозяева / гости'
                    facts.append(f"Средние показатели сезона ({scope}): {plain(event.get(side))} — забито за матч {rate['for']:g}, пропущено {rate['against']:g}")
            if any((row['matches'] or (row.get('season') or {}).get('matches') or 0)<5 or (row['source']=='history' and row['effective_matches']<3) for row in summaries):limitations.append('Малая или давняя выборка: оценка предварительная и чувствительна к отдельным результатам.')
            if any(row['source']=='season' for row in summaries):limitations.append('Для части участников расчёт опирается на сезонные средние вместо полной подтверждённой истории встреч.')
            if any(c.get('other_competition_used') for c in feed['counts'].values()) or any(not r.get('same_competition',True) for r in rows):
                limitations.append('Результаты разных турниров учтены с меньшим весом; уровень соперников отдельно не оценён.')
        if any(c.get('unconfirmed_final_status') for c in feed['counts'].values()):
            limitations.append('У части истории виджета не подтверждён финальный статус; эти встречи не включены в расчёт.')
    if finished: limitations.append('Матч уже завершён; прогноз до начала не формируется.')
    if event.get('phase')=='live':
        if event.get('live_score'): facts.append('Счёт в снимке: '+plain(event['live_score'])+(' · '+plain(event.get('live_clock')) if event.get('live_clock') else ''))
        limitations.append('Независимая модель для live пока не поддерживается; вероятности не рассчитаны.')
    if event.get('sport')!='football':limitations.append('Независимая модель для этого вида спорта пока не поддерживается.')
    facts.extend(season_facts(context))
    evaluated=[]; winner=None; unsupported=0
    source_popular=(context.get('market_selection') or {}).get('policy')=='source-popular-v1'
    unavailable_conditions=False
    for market in context['markets']:
        estimated=goal_probabilities(market,grid,event) if grid is not None else {}
        if not estimated: unsupported+=1
        for item in market['outcomes']:
            estimate=estimated.get(item['key'])
            if not estimate:continue
            available=item.get('state')=='open' and not market.get('is_stale')
            if not available and not source_popular:continue
            unavailable_conditions|=not available
            record={'market_key':market['key'],'outcome_key':item['key'],'market':market['name'],'period':market.get('period'),
                    'parameter':item.get('parameter') or market.get('parameter'),'outcome':item['name'],
                    'availability':'observed' if available else 'unconfirmed',**estimate}
            evaluated.append(record)
        if winner is None and event.get('home') and event.get('away') and full_time(market) and is_1x2(market):
            candidates=[r for r in evaluated if r['market_key']==market['key']]
            if candidates: winner=max(candidates,key=lambda r:r['probability'])
    distribution=None
    if grid is not None:
        h,a=np.indices(grid.shape)
        distribution={'home':float(grid[h>a].sum()),'draw':float(grid[h==a].sum()),'away':float(grid[h<a].sum())}
    if grid is not None and winner is None:
        h,a=np.indices(grid.shape)
        outcome,p=max((('1',float(grid[h>a].sum())),('X',float(grid[h==a].sum())),('2',float(grid[h<a].sum()))),key=lambda r:r[1])
        winner={'market_key':None,'outcome_key':None,'market':'Исход 1X2','period':'осн.время',
                'outcome':outcome,'probability':p,'basis':'history'}
    if finished: evaluated=[]; winner=None
    if winner:
        label=norm(winner['outcome']); winner={**winner,'participant':event.get('home') if label in ('1','п1') else event.get('away') if label in ('2','п2') else 'Ничья'}
    if not native and not finished and event.get('phase')!='live' and event.get('sport')=='football': limitations.append('Недостаточно подтверждённой истории для независимого расчёта; вероятности не рассчитаны.')
    if native: limitations.append('Оценка ещё не прошла проверку калибровки на будущих матчах.')
    if native and any(re.fullmatch(r'[12]\s*(?:-?й)?\s*тайм',norm(m.get('period'))) for m in context['markets']):
        limitations.append('Оценки таймов приближённые: отдельная подтверждённая история таймов отсутствует.')
    if unsupported: limitations.append('Для части рынков вероятность не рассчитана: условия или модель не поддерживаются.')
    if unavailable_conditions:limitations.append('Вероятности рассчитаны для сохранённых условий популярных рынков; их текущее наличие у источника не подтверждено.')
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
        if winner: facts.append('Независимая оценка основана на подтверждённых завершённых матчах участников.')
        else: facts.append('В доступных данных нет подтверждённого основания выбрать победителя')
    return {'winner':winner,'markets':evaluated,'facts':facts,'limitations':limitations,'finished':finished,
            'history_diagnostics':history_counts,'independent_only':True,'outcome_probabilities':distribution,
            'coverage':{'received_markets':len(context['markets']),'evaluated_markets':len({r['market_key'] for r in evaluated})}}


def report_markets(assessment):
    candidates=sorted(assessment['markets'],key=lambda r:(r['basis']!='history',-r['probability']))
    chosen=[];seen=set()
    for row in candidates:
        if row['probability']<.55 or row['market_key'] in seen:continue
        chosen.append(row);seen.add(row['market_key'])
        if len(chosen)==3:break
    return chosen


def render_report(assessment, indices=None, extra_limit=None,arguments=None):
    def pct(value): return '>99,9%' if value>.999 else f'{value*100:.1f}'.replace('.',',')+'%'
    def label(record):
        parts=[record['market'],record.get('period'),record.get('parameter'),record['outcome']]
        return ' · '.join(dict.fromkeys(plain(p,70) for p in parts if p))
    winner=assessment.get('winner')
    if winner:
        basis='по линии' if winner['basis']=='market' else 'оценка'
        first=f"Победитель: {plain(winner.get('participant'))} — {pct(winner['probability'])} ({basis}; {plain(winner['market'])}{' · '+plain(winner['period']) if winner.get('period') else ''})."
    else: first='Победитель: '+('прогноз не формируется — матч завершён.' if assessment.get('finished') else 'оценить по доступным данным нельзя.')
    chosen=report_markets(assessment)
    second='Победные рынки (оценка): '+('; '.join(f"{label(r)} — {pct(r['probability'])}{' по линии' if r['basis']=='market' else ''}{' (возврат '+pct(r['push_probability'])+')' if r.get('push_probability') else ''}" for r in chosen) if chosen else 'подходящих вариантов для оценки не найдено')+'.'
    bank=assessment['facts']; indices=list(range(min(3,len(bank)))) if indices is None else indices
    third='Аргументы: '+'; '.join(plain(value,1500).replace(';',',') for value in arguments[:4]) + '.' if arguments else 'Аргументы: '+'; '.join(plain(bank[i],220) for i in dict.fromkeys(indices) if type(i) is int and 0<=i<len(bank)) + '.'
    limits=list(dict.fromkeys(assessment['limitations']+([extra_limit] if extra_limit else [])))
    return '\n\n'.join([first,second,third]+(['Ограничения: '+' '.join(limits)] if limits else []))
