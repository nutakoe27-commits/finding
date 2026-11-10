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


def reply(packet,decision=None):
    decision=decision or ('skip' if packet['policy']['force_skip'] else 'bet')
    support=[f['id'] for f in packet['facts'] if f['kind'] in ('statistics','history')]
    ids=support[:2] or [packet['facts'][0]['id']]
    return {'decision':decision,'selection':'home' if decision=='bet' else None,'stake_level':'small' if decision=='bet' else None,
            'arguments':[{'fact_ids':ids,'text':EXPLANATION}]}


def complete(settings,messages):return json.dumps(reply(json.loads(messages[1]['content'])),ensure_ascii=False)
