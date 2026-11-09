"""A deterministic local JSON protocol fixture; never generates probabilities."""
import json


def reply(payload):
    stage=payload['stage']
    if stage in ('evidence','evidence_repair'):
        records=[r for r in payload['records'] if not isinstance(r['data'],str) or r['data'].strip()]
        if not records:return {'observations':[]}
        record=records[0]
        value=record['data'];literal=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,separators=(',',':'))
        return {'observations':[{'text':'Данные источника сопоставлены с условиями матча и рынка.',
            'quotes':[{'source':record['id'],'quote':literal[:160]}]}]}
    if stage=='reduce':return {'evidence_ids':[e['id'] for e in payload['candidates'][:payload['selection_limit']]]}
    if stage=='synthesis':return {'arguments':[{'text':'Данные источников сопоставлены с условиями матча; оценки требуют учёта доступной истории.',
        'evidence_ids':[payload['evidence'][0]['id']]}]}
    raise AssertionError(stage)


def completion(settings,messages):return json.dumps(reply(json.loads(messages[1]['content'])),ensure_ascii=False)


def leaf_values(value,path=''):
    if isinstance(value,dict) and value:
        result={}
        for key,item in value.items():result.update(leaf_values(item,path+'/'+str(key).replace('~','~0').replace('/','~1')))
        return result
    if isinstance(value,list) and value:
        result={}
        for index,item in enumerate(value):result.update(leaf_values(item,path+'/'+str(index)))
        return result
    return {path:value}


def record_values(records):
    unique={r['id']:r for r in records};result={};pieces={}
    for r in unique.values():
        if 'start' in r:pieces.setdefault(r['path'],[]).append(r)
        else:result.update(leaf_values(r['data'],r['path']))
    for path,parts in pieces.items():
        parts.sort(key=lambda r:r['start']);expected=0
        for part in parts:
            assert part['start']==expected;expected=part['end']
        assert expected==parts[0]['length']
        result[path]=''.join(p['data'] for p in parts)
    return result
