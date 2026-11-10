"""Deterministic protocol replies and public statistics for verdict tests."""
from datetime import datetime,timezone
import json

EXPLANATION='У хозяев более надёжная защита по доступным средним показателям, но результативность гостей служит встречным доводом. Ограниченный объём подтверждённой истории требует осторожного выбора.'


def statistics(identifier='123',stats_id='72334410',at=None):
    at=at or datetime.now(timezone.utc).isoformat()
    return {'format':'matchlab.event-statistics.v1','state':'ok','source_event_id':identifier,'statistics_id':stats_id,
        'event_url':f'https://winline.ru/stavki/sport/futbol/{identifier}',
        'statistics_url':'https://winline.ru/stats/match/'+stats_id,'provider_url':'https://statshub.yunai.tech/winline/ru/match/'+stats_id,
        'attempted_at':at,'observed_at':at,'match_phase':'prematch','header':'Спартак\nЗенит\nЗавтра',
        'sections':[{'title':'Статистика сезона','filters':['За матч','Общее'],
            'text':'Статистика сезона\n1.2\nЗабитые мячи\n1.5\n1.0\nГолов пропущено\n1.4'}]}


def reply(packet,decision=None,preference='home'):
    decision=decision or ('skip' if packet['policy']['force_skip'] else 'bet')
    support={s:[f['id'] for f in packet['facts'] if f['kind'] in ('statistics','history') and f.get('side')==s] for s in ('home','away')}
    ids=[(support[s] or [packet['facts'][0]['id']])[0] for s in support]
    markets=[f['id'] for f in packet['facts'] if f['kind']=='market']
    return {'preference':preference,'decision':decision,'stake_level':'small' if decision=='bet' else None,
        'comparison':{'home':{'fact_ids':[ids[0]],'text':EXPLANATION},
                      'away':{'fact_ids':[ids[1]],'text':'Гости забивают чаще по доступным показателям, однако пропускают больше хозяев. Это встречный довод, требующий учёта при выборе стороны.'}},
        'counterargument':{'fact_ids':[ids[1]],'text':'Атака гостей служит встречным доводом к предпочтению хозяев: выбранная сторона может столкнуться с сопротивлением результативного соперника.'},
        'market_context':{'fact_ids':markets[:1],'text':'В сохранённой линии коэффициент хозяев ниже коэффициента гостей. Это согласуется с предпочтением хозяев, но само по себе не подтверждает качество выбора.'},
        'reason':{'code':'supporting_evidence' if decision=='bet' else 'conflicting_evidence','fact_ids':ids,
                  'text':'Более надёжная защита хозяев даёт основание для их предпочтения с учётом встречного довода об атаке гостей.' if decision=='bet' else 'Преимущество хозяев в защите сталкивается с более результативной атакой гостей. Этого сопоставления недостаточно для решения о ставке.'}}


def complete(settings,messages):return json.dumps(reply(json.loads(messages[1]['content'])),ensure_ascii=False)
