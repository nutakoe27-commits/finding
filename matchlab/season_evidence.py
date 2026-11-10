"""Observed season rates with explicit view/scope; totals are never treated as rates."""
import math
import re

PAIR=re.compile(r'(\d+(?:[.,]\d+)?)\s*(%?)\s+(Победы|Забитые мячи|Голов пропущен[оo]|Сухие матчи)\s+(\d+(?:[.,]\d+)?)\s*(%?)',re.I)


def season_views(context):
    from .chances import norm
    result={}
    for section in (context.get('team_statistics') or {}).get('sections',[]):
        if not norm(section.get('title')).startswith('статистика сезона') or section.get('truncated'):continue
        filters={norm(f) for f in section.get('filters',[])}
        mode='rate' if 'за матч' in filters else 'total' if 'итоги' in filters else None
        scope='venue' if 'хозяева / гости' in filters else 'overall' if 'общее' in filters else None
        if not mode or not scope:continue
        metrics={}
        for h,hu,name,a,au in PAIR.findall(section.get('text','')):
            if hu!=au:continue
            key='for' if norm(name)=='забитые мячи' else 'against' if norm(name).startswith('голов пропущен') else None
            if key and not hu:metrics[key]=(float(h.replace(',','.')),float(a.replace(',','.')))
        if set(metrics)>={'for','against'}:
            value={s:{k:metrics[k][i] for k in ('for','against')} for i,s in enumerate(('home','away'))}
            if all(math.isfinite(v) and 0<=v<=10000 for row in value.values() for v in row.values()):result[(mode,scope)]=value
    return result


def season_rates(context):
    views=season_views(context);rates=views.get(('rate','overall')) or views.get(('rate','venue'))
    if not rates:return {}
    scope='overall' if ('rate','overall') in views else 'venue'
    totals=views.get(('total',scope)) or {}
    result={}
    for side,values in rates.items():
        if any(v>30 for v in values.values()):continue
        candidates=[n for n in range(1,1001) if any(values[k]>0 for k in values) and
                    all(k in (totals.get(side) or {}) and abs(totals[side][k]/n-values[k])<=.0051 for k in values)]
        count=candidates[0] if len(candidates)==1 else None
        result[side]={**values,'scope':scope,'matches':count,'matches_inferred':bool(count)}
        venue=(views.get(('rate','venue')) or {}).get(side)
        if scope=='overall' and venue and all(0<=v<=30 for v in venue.values()):result[side]['venue']=venue
    return result
