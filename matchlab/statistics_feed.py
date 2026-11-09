"""Bounded, read-only extraction of data already delivered to a match widget.

This module does not make requests and is not connected to production ingestion.
"""
from __future__ import annotations

import json
import re
import unicodedata

PRIVATE_KEYS=re.compile(r'password|passwd|token|secret|authorization|cookie|session|api.?key|signature|email|phone|address',re.I)
URL_VALUE=re.compile(r'https?://|^www\.',re.I)
MAX_TOTAL_BYTES=100_000


def _name(value):
    if not isinstance(value,str): return ''
    return ''.join(c for c in unicodedata.normalize('NFKC',value).casefold() if c.isalnum())


def _id(value):
    if type(value) not in (str,int): return None
    text=str(value).rsplit(':',1)[-1]
    return text if re.fullmatch(r'\d{1,20}',text) else None


def _same_team(left,right):
    if not isinstance(left,dict) or not isinstance(right,dict): return False
    a=_id(left.get('_id')) or _id(left.get('id'))
    b=_id(right.get('_id')) or _id(right.get('id'))
    if a and b: return a==b
    a,b=_name(left.get('name')),_name(right.get('name'))
    return bool(len(a)>=3 and a==b)


def _sanitize(value, budget, depth=0):
    if budget[0]<=0 or depth>10:
        budget[1]=True
        return None
    budget[0]-=1
    if value is None or type(value) in (bool,int,float): return value
    if isinstance(value,str):
        if URL_VALUE.search(value) or '@' in value: return None
        if len(value)>1000:
            budget[1]=True
            return value[:1000]
        return value
    if isinstance(value,list):
        if len(value)>100: budget[1]=True
        return [_sanitize(item,budget,depth+1) for item in value[:100]]
    if isinstance(value,dict):
        result={}
        for key,item in list(value.items())[:100]:
            if not isinstance(key,str) or len(key)>80 or PRIVATE_KEYS.search(key) or '@' in key or URL_VALUE.search(key): continue
            result[key]=_sanitize(item,budget,depth+1)
        if len(value)>100: budget[1]=True
        return result
    budget[1]=True
    return None


def extract_passive_feed(documents, statistics_id):
    """Return match-bound public match lists, preserving unknown source fields.

    Callers must independently enforce provider origin and widget-frame identity.
    A list can contain finished and scheduled events; it is not classified as history.
    """
    expected=_id(statistics_id)
    if not expected or not isinstance(documents,list): return None
    target=None
    for data in documents:
        if not isinstance(data,dict): continue
        match=data.get('match') if isinstance(data.get('match'),dict) else data
        if _id(match.get('_id'))!=expected: continue
        teams=match.get('teams')
        if not isinstance(teams,dict) or not isinstance(teams.get('home'),dict) or not isinstance(teams.get('away'),dict): continue
        if not _name(teams['home'].get('name')) or not _name(teams['away'].get('name')) or _same_team(teams['home'],teams['away']): continue
        if target and (not _same_team(target['teams']['home'],teams['home']) or not _same_team(target['teams']['away'],teams['away'])):
            return None
        target=match
    if not target: return None
    sides={'home':[],'away':[]}
    seen={'home':set(),'away':set()}
    truncated=False
    for data in documents:
        if not isinstance(data,dict) or not isinstance(data.get('team'),dict) or not isinstance(data.get('matches'),list): continue
        matches=[side for side in sides if _same_team(target['teams'][side],data['team'])]
        if len(matches)!=1: continue
        side=matches[0]
        budget=[15000,False]
        safe=_sanitize({'team':data['team'],'matches':data['matches']},budget)
        encoded=json.dumps(safe,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        if encoded in seen[side]: continue
        if len(sides[side])>=4 or len(encoded.encode())>MAX_TOTAL_BYTES:
            truncated=True
            continue
        seen[side].add(encoded)
        sides[side].append(safe)
        truncated|=budget[1]
    match_budget=[10000,False]
    result={'binding':'statistics_id','statistics_id':expected,'match':_sanitize(target,match_budget),
            'participants':sides,'scope':'source match lists may include finished and future fixtures',
            'truncated':truncated or match_budget[1]}
    if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode())>MAX_TOTAL_BYTES:
        return None
    return result
