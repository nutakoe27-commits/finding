"""Admin-only token counts. Unknown usage is never estimated from text bytes."""
from collections import defaultdict
from datetime import datetime,timedelta,timezone
import json
import math

from .bookmakers import instant
from .token_usage import count
from .verdict_outcomes import MSK


def usage_summary(value,*,cached=False,model_calls=None):
    if not isinstance(value,dict) and (cached or model_calls==0):
        return {'model_calls':0,'reported_calls':0,'unreported_calls':0,'complete':True,
            **{k:0 for k in ('prompt_tokens','completion_tokens','total_tokens','peak_prompt_tokens','peak_total_tokens','peak_context_tokens')},'calls':[]}
    if not isinstance(value,dict):return None
    fields=('model_calls','reported_calls','unreported_calls')
    if any(count(value.get(k)) is None for k in fields):return None
    if value['reported_calls']+value['unreported_calls']!=value['model_calls']:return None
    if value.get('complete')!=(value['unreported_calls']==0):return None
    result={k:value[k] for k in fields};result['complete']=value['complete']
    for k in ('prompt_tokens','completion_tokens','total_tokens','peak_prompt_tokens','peak_total_tokens','peak_context_tokens'):
        result[k]=count(value.get(k))
    if value['reported_calls'] and any(result[k] is None for k in ('prompt_tokens','completion_tokens','total_tokens')):return None
    if result['total_tokens'] is not None and result['total_tokens']!=(result['prompt_tokens'] or 0)+(result['completion_tokens'] or 0):return None
    result['calls']=[]
    for call in value.get('calls') or []:
        if not isinstance(call,dict):continue
        safe={k:count(call.get(k)) for k in ('prompt_tokens','completion_tokens','total_tokens','max_output_tokens')}
        safe['finish_reason']=call.get('finish_reason') if call.get('finish_reason') in ('stop','length','tool_calls','content_filter') else 'unknown'
        result['calls'].append(safe)
    return result


def percentile(values,p):
    if not values:return None
    values=sorted(values);i=(len(values)-1)*p;lo=math.floor(i);hi=math.ceil(i)
    return round(values[lo]+(values[hi]-values[lo])*(i-lo))


def run(repo,*,days=30,kind='all',offset=0,limit=25,now=None):
    if days not in (7,30,90) or kind not in ('all','event','forecast') or not 0<=offset<=100000 or not 1<=limit<=100:
        raise ValueError('Некорректные фильтры расхода токенов.')
    current=now or datetime.now(timezone.utc);today=current.astimezone(MSK).date()
    dates=[today-timedelta(days=i) for i in range(days-1,-1,-1)];rows=[]
    with repo.connection() as db:
        raw=db.execute('''SELECT a.id,a.at,a.kind,a.status,a.latency_ms,a.details,coalesce(u.username,CASE WHEN a.user_id='__system_featured__' THEN 'Системный анализ' END) username
            FROM ai_usage a LEFT JOIN users u ON u.id=a.user_id
            WHERE julianday(a.at)>=julianday(?) ORDER BY julianday(a.at) DESC,a.id DESC''',
            (datetime.combine(dates[0],datetime.min.time(),MSK).isoformat(),))
        for row in raw:
            if kind!='all' and row['kind']!=kind:continue
            try:
                at=instant(row['at'])
                if at>current:continue
                details=json.loads(row['details']) if row['details'] else {}
                if not isinstance(details,dict):details={}
            except (TypeError,ValueError):continue
            cached=details.get('cache_hit') is True
            usage=usage_summary(details.get('token_usage'),cached=cached,model_calls=details.get('model_calls'))
            state='missing' if usage is None else 'cache' if cached else 'not_called' if usage['model_calls']==0 else 'recorded' if usage['complete'] else 'partial'
            rows.append({k:row[k] for k in ('id','at','kind','status','latency_ms','username')}|
                {'token_usage':usage,'usage_state':state,'model_calls':details.get('model_calls'),'cached':cached})
    known=[r['token_usage'] for r in rows if r['token_usage'] and r['token_usage']['reported_calls']>0]
    complete=[u for u in known if u['complete']]
    totals=[u['total_tokens'] for u in complete];prompts=[u['peak_prompt_tokens'] for u in complete if u['peak_prompt_tokens'] is not None]
    windows=[u['peak_context_tokens'] for u in complete if u['peak_context_tokens'] is not None]
    bounds=[0,4096,8192,16384,32768,65536,131072,float('inf')]
    labels=['до 4К','4–8К','8–16К','16–32К','32–64К','64–128К','128К+']
    histogram=[{'label':label,'calls':0} for label in labels]
    for u in known:
        for c in u['calls']:
            n=c.get('prompt_tokens')
            if n is not None:
                i=next(i for i in range(len(labels)) if bounds[i]<=n<bounds[i+1]);histogram[i]['calls']+=1
    daily=[];grouped=defaultdict(list)
    for r in rows:grouped[instant(r['at']).astimezone(MSK).date()].append(r)
    for day in dates:
        group=grouped[day];usages=[r['token_usage'] for r in group if r['token_usage']]
        daily.append({'day':str(day),'requests':len(group),'prompt_tokens':sum(u['prompt_tokens'] or 0 for u in usages),
            'completion_tokens':sum(u['completion_tokens'] or 0 for u in usages),
            'unreported_requests':sum(r['usage_state'] in ('missing','partial') for r in group)})
    return {'format':'matchlab.token-usage.v1','checked_at':current.isoformat(),'timezone':'Europe/Moscow','days':days,'kind':kind,
        'metrics':{'requests':len(rows),'recorded_requests':len(complete),'partial_requests':sum(r['usage_state']=='partial' for r in rows),
            'missing_requests':sum(r['usage_state']=='missing' for r in rows),'cached_requests':sum(r['cached'] for r in rows),
            'not_called_requests':sum(r['usage_state']=='not_called' for r in rows),
            'prompt_tokens':sum(u['prompt_tokens'] for u in known),'completion_tokens':sum(u['completion_tokens'] for u in known),
            'total_tokens':sum(u['total_tokens'] for u in known),
            'reported_calls':sum(u['reported_calls'] for u in known),'unreported_calls':sum((r['token_usage'] or {}).get('unreported_calls',0) for r in rows),
            'request_tokens_p50':percentile(totals,.5),'request_tokens_p95':percentile(totals,.95),'request_tokens_max':max(totals,default=None),
            'peak_prompt_p50':percentile(prompts,.5),'peak_prompt_p95':percentile(prompts,.95),
            'peak_prompt_max':max((u['peak_prompt_tokens'] for u in known if u['peak_prompt_tokens'] is not None),default=None),
            'context_p95':percentile(windows,.95),'context_max':max((u['peak_context_tokens'] for u in known if u['peak_context_tokens'] is not None),default=None)},
        'daily':daily,'histogram':histogram,'requests':rows[offset:offset+limit],'total':len(rows),'offset':offset,'limit':limit,
        'limitations':['Это usage сервера модели, а не внутренние платёжные токены сервиса.',
            'Расход — сумма всех вызовов, включая отклонённые ответы и исправления. Пик — максимум одного вызова.',
            'Нужное окно включает вход и запрошенный резерв ответа; это не объём фактически сгенерированного текста.',
            'При таймауте без usage расход неизвестен. Частичные суммы — только подтверждённая часть.',
            'Исторические запросы без записанного usage не оцениваются по байтам и не входят в перцентили.']}
