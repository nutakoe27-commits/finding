"""Observed season rates with explicit view/scope; totals are never treated as rates."""
import math
import re
import unicodedata

PAIR=re.compile(r'(\d+(?:[.,]\d+)?)\s*(%?)\s+(Победы|Забитые мячи|Голов пропущен[оo]|Сухие матчи)\s+(\d+(?:[.,]\d+)?)\s*(%?)',re.I)


def source_pairs(section):
    """Read labelled home/value/away rows, without guessing metric semantics.

    Percent signs may be in their own DOM cells. Buttons and participant
    abbreviations are not measurements; incomplete/ambiguous rows are ignored.
    """
    if section.get('truncated'):return []
    wording=unicodedata.normalize('NFKC',str(section.get('text') or ''))
    wording=re.sub(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069]','',wording)
    cells=[v.strip() for v in re.split(r'[\r\n\t]+',wording) if v.strip()]
    numeric=re.compile(r'-?[0-9]+(?:[.,][0-9]+)?(?:/[0-9]+)?\s*%?')
    ignored={'статистика сезона','за матч','итоги','серия','общее','хозяева / гости','вся статистика'}
    def value(index):
        if index>=len(cells) or not numeric.fullmatch(cells[index]):return None
        raw=cells[index].replace(' ','');end=index+1
        if not raw.endswith('%') and end<len(cells) and cells[end]=='%':raw+='%';end+=1
        unit='%' if raw.endswith('%') else 'ratio' if '/' in raw else ''
        try:
            numbers=[float(v.replace(',','.')) for v in raw.rstrip('%').split('/')]
            if any(not math.isfinite(v) or abs(v)>1000000 for v in numbers):return None
            if unit=='%' and not 0<=numbers[0]<=100:return None
            if unit=='ratio' and (numbers[1]<=0 or numbers[0]<0):return None
        except (ValueError,OverflowError):return None
        return raw,unit,end
    found={}
    for i in range(len(cells)):
        left=value(i)
        if not left or left[2]>=len(cells):continue
        label=cells[left[2]]
        if (label.casefold() in ignored or label=='%' or numeric.fullmatch(label) or
            not re.search(r'[^\W\d_]',label) or len(label)>100):continue
        right=value(left[2]+1)
        if not right or left[1]!=right[1]:continue
        identity=' '.join(label.casefold().split())
        row={'metric':label,'home':left[0],'away':right[0],'unit':left[1]}
        if identity in found and found[identity]!=row:found[identity]=None
        elif identity not in found:found[identity]=row
    return [row for row in found.values() if row is not None]


def collect_source_season(event,statistics,add,*,typed_views=()):
    """Keep source captions and selected scope for every supported sport."""
    from .verdict_sports import key
    warnings=[]
    for section in (statistics or {}).get('sections',[]):
        if not key(section.get('title')).startswith('статистика сезона'):continue
        pairs=source_pairs(section)
        if not pairs:continue
        filters=[str(v) for v in section.get('filters') or []]
        names={key(v) for v in filters}
        mode='rate' if 'за матч' in names and 'итоги' not in names else 'total' if 'итоги' in names and 'за матч' not in names else 'unknown'
        scope='venue' if 'хозяева / гости' in names else 'overall' if 'общее' in names else 'source_view'
        where=', '.join(filters) if filters else 'выбранные фильтры не подтверждены'
        if mode=='unknown':warnings.append('Для части сезонных показателей не подтверждён режим «За матч» или «Итоги»; их нельзя выдавать за средние или суммы.')
        for row in pairs:
            metric=key(row['metric'])
            if (mode,scope) in typed_views and (metric=='забитые мячи' or re.fullmatch('голов пропущен[оo]',metric)):continue
            for side in ('home','away'):
                literal=f"Статистика сезона ({where}), {event[side]}: {row['metric']} — {row[side]}. Показатель источника, не вероятность будущей победы."
                add('statistics',literal,side=side,scope='source_season_view',metric=row['metric'],
                    measurement=mode,source_value=row[side],source_unit=row['unit'])
    return list(dict.fromkeys(warnings))


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
