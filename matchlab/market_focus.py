"""Choose whole, relevant markets for reasoning; retain full inputs in the audit store."""
from copy import deepcopy
from decimal import Decimal,InvalidOperation
import json
import re
import unicodedata

from .markets import headline_market

POLICY='independent-markets-v3'
FAMILIES=('winner','double_chance','both_score','total','handicap','draw_no_bet','team_total_home','team_total_away')
SIDE_MARKETS=re.compile(r'углов|карточ|удар|офсайд|фол|пенальти|эйс|двойн\w*\s+ошиб|брейк|подач|чет.?нечет|ч[её]тност|точн\w*\s+сч[её]т|игрок|player|corner|booking',re.I)
PART=re.compile(r'тайм|четверт|период|половин|half|quarter|period|(?:\b[1-9]\s*(?:-?\s*[йям])?\s*(?:сет|гейм|раунд|set|game|round))|(?:\b[1-9]\s*[тпqh]\b)',re.I)


def normalized(value):return ' '.join(unicodedata.normalize('NFKC',str(value or '')).casefold().split())


def available(market):
    outcomes=market.get('outcomes') or []
    valid=[o.get('state')=='open' for o in outcomes]
    return (not market.get('is_stale') and market.get('outcomes_complete') is True and len(outcomes)>=2 and
            (any(valid) if row_kind(market) else all(valid)))


def family(market,event=None):
    name=normalized(market.get('name'));period=normalized(market.get('period'))
    combined=re.sub(r'\b(?:с\s+)?(?:от|овертайм\w*)\s+и\s+(?:б|буллит\w*)\b','overtime',name)
    if SIDE_MARKETS.search(name) or PART.search(name+' '+period) or re.search(r'\bи\b|\band\b',combined):return None
    if re.search(r'европейск.*гандикап|european handicap',name):return None
    if re.search(r'мин\.?|minute|секунд|second',period):return None
    if re.search(r'\b(?:сет|гейм|раунд|set|game|round)\b',period):return None
    if re.search(r'без\s+ничь|draw\s+no\s+bet',name):return 'draw_no_bet'
    if re.search(r'двойной шанс|double chance',name):return 'double_chance'
    if re.search(r'обе.*забьют|both.*score',name):return 'both_score'
    if re.search(r'индивидуальн|командн|тотал.*команд|team total|\bит\s*[12]\b',name):
        if re.search(r'(?:команд[аы]?|ит|тотал)\s*1(?![\d.,])|хозяев|первой|home',name):return 'team_total_home'
        if re.search(r'(?:команд[аы]?|ит|тотал)\s*2(?![\d.,])|гост|второй|away',name):return 'team_total_away'
        return None
    if re.search(r'тотал|total',name):
        named=[(len(normalized((event or {}).get(side))),side) for side in ('home','away')
               if normalized((event or {}).get(side)) and re.search(r'(?<!\w)'+re.escape(normalized((event or {}).get(side)))+r'(?!\w)',name)]
        if named:
            named.sort(reverse=True)
            if len(named)==1 or named[0][0]>named[1][0]:return 'team_total_'+named[0][1]
            return None
        if re.fullmatch(r'(?:азиатский\s+)?тотал(?:\s+(?:голов|очков|геймов|сетов|раундов|матча|игры|шайб))?\s*(?:\([^)]*\))?|(?:match\s+)?total(?:\s+(?:goals|points|games|sets))?',name):return 'total'
        return None
    if re.search(r'фора|гандикап|handicap|spread',name):return 'handicap'
    if market.get('parameter') in (None,'') and re.fullmatch(r'(?:исход(?:\s+(?:1[xх]2|12))?|победитель(?:\s+матча)?|победа(?:\s+в\s+матче)?|победит|match winner|moneyline)(?:\s*\([^)]*\))?',name):return 'winner'
    return None


def scope(market):
    text=normalized(str(market.get('name') or '')+' '+str(market.get('period') or ''))
    if re.search(r'овертайм|буллит|\bот\b|overtime|extra time',text):return 'overtime'
    if re.search(r'осн\.?\s*врем|основное время|regular time',text):return 'regular'
    return normalized(market.get('period')) or 'unspecified'


def balance(market,assessment):
    values=[r['probability'] for r in assessment.get('markets',[]) if r.get('market_key')==market['key'] and r.get('basis')=='history']
    if values:return min(abs(p-.5) for p in values)
    number=line_number(market.get('parameter'))
    # Deterministic conventional lines when there is no independent estimate.
    if number is not None:return float(abs(number-(Decimal('2.5') if family(market)=='total' else 0)))
    return 0


def line_number(value):
    text=normalized(value).replace('−','-').replace(',','.')
    text=re.sub(r'^(?:т?[бм]|over|under)\s*','',text)
    if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?',text):return None
    try:return Decimal(text)
    except InvalidOperation:return None


def row_kind(market):
    """Some source tables split over/under and home/away into separate markets."""
    name=normalized(market.get('name'));parameter=normalized(market.get('parameter'))
    if re.search(r'тотал|total',name) and parameter in ('больше','меньше','over','under'):
        return 'total'
    if re.search(r'фора|гандикап|handicap|spread',name) and parameter and line_number(parameter) is None and all(line_number(o.get('name')) is not None for o in market.get('outcomes',[])):
        return 'handicap'
    return None


def paired_balance(rows,kind):
    lines=[]
    for index,row in enumerate(rows):
        values={}
        for item in row['outcomes']:
            number=line_number(item.get('name'))
            if number is None or item.get('state')!='open':continue
            values[-number if kind=='handicap' and index else number]=True
        lines.append(values)
    shared=set(lines[0])&set(lines[1])
    return float(min((abs(n-(Decimal('2.5') if kind=='total' else 0)) for n in shared),default=0)) if shared else None


def encoded_size(value):return len(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode())


def focus_context(context,limit=8):
    """Keep all outcomes/metadata of selected markets and all participant evidence."""
    popular=context.get('popular_markets') or []
    if popular:
        result=deepcopy(context);result.pop('popular_markets',None)
        chosen=deepcopy(popular)
        selected=(context.get('selection') or {}).get('market_key')
        extra=next((m for m in context.get('markets',[]) if m['key']==selected and not any(
            (m['name'],m.get('period'),m.get('parameter'))==(p['name'],p.get('period'),p.get('parameter')) for p in popular)),None)
        if extra:chosen.append(deepcopy(extra))
        result['markets']=chosen
        result['primary_market_key']=next((m['key'] for m in chosen if normalized(m['name'])=='исход 1x2' and scope(m)=='regular'),None)
        result['snapshot']={**result.get('snapshot',{}),'complete':bool(context.get('popular_complete')),'scope':'source_popular_markets'}
        coverage=result.setdefault('coverage',{})
        coverage.update(analyzed_markets=len(chosen),analyzed_outcomes=sum(len(m['outcomes']) for m in chosen))
        result['market_selection']={'policy':'source-popular-v1','limit':len(chosen),'available_markets':len(context['markets']),
            'selected_markets':len(chosen),'selected_keys':[m['key'] for m in chosen],'source_groups':list(dict.fromkeys(p['group'] for p in popular)),
            'reasons':{m['key']:m['group'] for m in chosen},'context_only_keys':[],
            'eligible_markets':sum(available(m) for m in chosen),'stale_markets':sum(bool(m.get('is_stale')) for m in chosen)}
        return result
    if limit==0:return context
    if not 2<=limit<=20:raise ValueError('Лимит рынков должен быть 0 или от 2 до 20.')
    existing=context.get('market_selection') or {}
    if existing.get('policy')==POLICY and existing.get('limit')==limit:return context
    markets=context.get('markets') or [];lookup={m['key']:m for m in markets}
    chosen=[];reasons={}
    def add(key,reason,explicit=False):
        if key not in lookup or key in reasons or len(chosen)>=limit:return
        if not explicit and not available(lookup[key]):return
        chosen.append(lookup[key]);reasons[key]=reason
    add((context.get('selection') or {}).get('market_key'),'user_selection',True)
    assessment=context.get('probability_assessment') or {}
    add((assessment.get('winner') or {}).get('market_key'),'winner')
    headline=headline_market(context)
    if headline:add(headline['key'],'headline')
    representatives={}
    tables={}
    for market in markets:
        group=family(market,context.get('event'))
        if group is None or not available(market):continue
        if row_kind(market):
            tables.setdefault((group,scope(market),normalized(market['name'])),[]).append(market)
            continue
        key=(group,scope(market))
        current=representatives.get(key)
        candidate=(balance(market,assessment),[market])
        if current is None or (candidate[0],market['key'])<(current[0],current[1][0]['key']):representatives[key]=candidate
    for (group,period,name),rows in tables.items():
        if len(rows)!=2:continue
        kind=row_kind(rows[0]);parameters={normalized(r.get('parameter')) for r in rows}
        expected=({'больше','меньше'},{'over','under'}) if kind=='total' else (
            {normalized((context.get('event') or {}).get(side)) for side in ('home','away')},)
        if parameters not in expected:continue
        rank=paired_balance(rows,kind)
        if rank is None:continue
        current=representatives.get((group,period))
        if current is None or rank<current[0]:representatives[(group,period)]=(rank,rows)
    ordered=sorted(representatives.items(),key=lambda item:(FAMILIES.index(item[0][0]),
            {'regular':0,'матч':1,'match':1,'overtime':2}.get(item[0][1],3),item[0][1],item[1][0],item[1][1][0]['key']))
    def add_group(group,rows):
        if len(chosen)+sum(m['key'] not in reasons for m in rows)>limit:return
        for market in rows:add(market['key'],group)
    for (group,period),(rank,rows) in ordered:
        if any(m['key'] in reasons for m in rows):add_group(group,rows)
    seen={family(m,context.get('event')) for m in chosen}
    for (group,period),(rank,rows) in ordered:
        if group not in seen:add_group(group,rows);seen.add(group)
    for (group,period),(rank,rows) in ordered:add_group(group,rows)
    if not chosen:
        for market in markets:
            if available(market) and not row_kind(market):add(market['key'],'fallback');break
    context_only=[]
    if not chosen and headline:
        add(headline['key'],'context_only_headline',True)
        context_only=[headline['key']]
    if len(chosen)==len(markets) and not context_only:return context
    result=deepcopy(context)
    result['markets']=deepcopy(chosen)
    kept=set(reasons)
    if context_only:result['market_implied']=None
    if headline and headline['key'] not in kept:
        result['market_implied']=None;result['primary_market_key']=None
    selected_outcomes=sum(len(m.get('outcomes',[])) for m in chosen)
    coverage=result.setdefault('coverage',{})
    coverage.setdefault('stored_markets',len(markets))
    coverage.setdefault('stored_outcomes',sum(len(m.get('outcomes',[])) for m in markets))
    coverage.update(analyzed_markets=len(chosen),analyzed_outcomes=selected_outcomes)
    if result.get('probability_assessment') is not None:
        assessed=result['probability_assessment']
        assessed['markets']=[r for r in assessed.get('markets',[]) if r.get('market_key') in kept and r.get('market_key') not in context_only]
        from .chances import report_markets
        assessed['recommended_markets']=report_markets(assessed)
        winner_key=(assessed.get('winner') or {}).get('market_key')
        if winner_key and (winner_key not in kept or winner_key in context_only):assessed['winner']=None
        evaluated={r['market_key'] for r in assessed['markets']}
        assessed.setdefault('coverage',{}).update(considered_markets=len(markets),received_markets=len(chosen),evaluated_markets=len(evaluated))
        if kept<=evaluated:
            assessed['limitations']=[v for v in assessed.get('limitations',[]) if v!='Для части рынков вероятность не рассчитана: условия или модель не поддерживаются.']
        if context_only:
            assessed.setdefault('limitations',[]).append('Свежих доступных рынков нет; сохранены только их условия. Независимая оценка исхода, если доступна, рассчитана по истории.')
    result['market_selection']={'policy':POLICY,'limit':limit,'available_markets':len(markets),'selected_markets':len(chosen),
        'excluded_markets':len(markets)-len(chosen),'selected_keys':list(reasons),'reasons':reasons,
        'market_bytes_before':encoded_size(markets),'market_bytes_after':encoded_size(chosen),
        'context_only_keys':context_only,'eligible_markets':sum(available(m) for m in markets),
        'stale_markets':sum(bool(m.get('is_stale')) for m in markets)}
    return result
