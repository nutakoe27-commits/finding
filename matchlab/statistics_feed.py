"""Bounded, read-only extraction of data already delivered to a match widget.

The observer does not issue requests; validated snapshots can be sent to the receiver.
"""
from __future__ import annotations

import json
import re
import unicodedata
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlsplit

PRIVATE_KEYS=re.compile(r'password|passwd|token|secret|authorization|cookie|session|api.?key|signature|email|phone|address',re.I)
URL_VALUE=re.compile(r'https?://|^www\.',re.I)
MAX_TOTAL_BYTES=100_000
MAX_RESPONSE_BYTES=1024*1024
MAX_CAPTURE_BYTES=4*1024*1024


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


def _team_id(team):
    if not isinstance(team,dict): return None
    return _id(team.get('_id')) or _id(team.get('id'))


def _anchored_teams(documents,target,expected):
    """Map external team IDs only when an observed list contains this exact match."""
    anchors={'home':set(),'away':set()}
    for data in documents:
        if not isinstance(data,dict) or not isinstance(data.get('team'),dict) or not isinstance(data.get('matches'),list): continue
        team=data['team']; team_id=_team_id(team); name=_name(team.get('name'))
        if not team_id or len(name)<3: continue
        sides=[side for side in anchors if _name(target['teams'][side].get('name'))==name]
        if len(sides)!=1: continue
        for row in data['matches'][:100]:
            if not isinstance(row,dict) or _id(row.get('_id'))!=expected: continue
            teams=row.get('teams')
            if not isinstance(teams,dict): continue
            if all(_team_id(target['teams'][side]) and _team_id(target['teams'][side])==_team_id(teams.get(side)) for side in anchors):
                anchors[sides[0]].add(team_id)
                break
    # A shared or ambiguous ID cannot establish a reliable mapping.
    shared=anchors['home'] & anchors['away']
    for side in anchors:
        if len(anchors[side])!=1 or anchors[side] & shared: anchors[side].clear()
    return anchors


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
    methods={'home':set(),'away':set()}
    anchors=_anchored_teams(documents,target,expected)
    truncated=False
    for data in documents:
        if not isinstance(data,dict) or not isinstance(data.get('team'),dict) or not isinstance(data.get('matches'),list): continue
        matches=[side for side in sides if _same_team(target['teams'][side],data['team'])]
        method='team_id_or_exact_name'
        if not matches:
            team_id=_team_id(data['team']); name=_name(data['team'].get('name'))
            matches=[side for side in sides if team_id in anchors[side] and name==_name(target['teams'][side].get('name'))]
            method='same_match_anchor'
        if len(matches)!=1: continue
        side=matches[0]
        budget=[15000,False]
        safe=_sanitize({'team':data['team'],'matches':data['matches']},budget)
        target_team_id=_team_id(target['teams'][side])
        verified=[]
        for index,row in enumerate(safe['matches']):
            teams=row.get('teams') if isinstance(row,dict) else None
            if target_team_id and isinstance(teams,dict) and any(
                    _team_id(teams.get(position))==target_team_id for position in ('home','away')):
                verified.append(index)
        safe['verified_participant_indices']=verified
        safe['unverified_participant_count']=len(safe['matches'])-len(verified)
        encoded=json.dumps(safe,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        if encoded in seen[side]: continue
        if len(sides[side])>=4 or len(encoded.encode())>MAX_TOTAL_BYTES:
            truncated=True
            continue
        seen[side].add(encoded)
        sides[side].append(safe)
        methods[side].add(method)
        truncated|=budget[1]
    match_budget=[10000,False]
    result={'binding':'statistics_id','statistics_id':expected,'match':_sanitize(target,match_budget),
            'participants':sides,'team_bindings':{side:sorted(methods[side]) for side in sides},
            'scope':'source match lists may include finished and future fixtures; only verified participant indices establish participant identity',
            'truncated':truncated or match_budget[1]}
    if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode())>MAX_TOTAL_BYTES:
        return None
    return result


def feed_has_other_matches(feed):
    """Require another identifiable event for each side, beyond the anchor match."""
    if not isinstance(feed,dict): return False
    expected=_id(feed.get('statistics_id'))
    participants=feed.get('participants')
    if not expected or not isinstance(participants,dict): return False
    for side in ('home','away'):
        lists=participants.get(side)
        if not isinstance(lists,list): return False
        found=False
        for item in lists:
            if not isinstance(item,dict) or not isinstance(item.get('matches'),list): continue
            verified=item.get('verified_participant_indices')
            if not isinstance(verified,list): continue
            for index in verified:
                if type(index) is not int or not 0<=index<len(item['matches']): continue
                row=item['matches'][index]
                if not isinstance(row,dict): continue
                event_id=_id(row.get('_id')) or _id(row.get('id'))
                if event_id and event_id!=expected: found=True; break
            if found: break
        if not found: return False
    return True


class PassiveFeedObserver:
    """Observe existing widget responses only; never issue network requests."""
    def __init__(self,page,statistics_id):
        self.page=page; self.statistics_id=_id(statistics_id); self.documents=[]
        self.feed=None; self.ready=asyncio.Event(); self.blocked=False; self.blocked_event=asyncio.Event()
        self.pending=set(); self.bytes_seen=0; self.response_count=0
        self._handler=self._on_response

    def start(self):
        self.page.on('response',self._handler)

    def _on_response(self,response):
        try:
            source=urlsplit(response.url); frame=urlsplit(response.request.frame.url)
            valid=(self.statistics_id and source.scheme=='https' and source.hostname=='sh.fn.yunai.tech'
                   and source.port in (None,443) and not source.username and not source.password
                   and frame.scheme=='https' and frame.hostname in ('statshub.yunai.tech','statshub.sportradar.com')
                   and re.fullmatch(rf'/winline/(?:ru|en)/match/{re.escape(self.statistics_id)}(?:/(?:report|statistics))?/?',frame.path)
                   and response.request.resource_type in ('fetch','xhr'))
            if not valid or self.response_count>=80: return
            self.response_count+=1
            if response.status in (401,403,429):
                self.blocked=True; self.blocked_event.set(); self.ready.set(); return
            if response.status!=200 or len(self.pending)>=40: return
            task=asyncio.create_task(self._read(response))
            self.pending.add(task); task.add_done_callback(self.pending.discard)
        except Exception:
            return

    async def _read(self,response):
        try:
            media=(await response.header_value('content-type') or '').lower()
            length=await response.header_value('content-length')
            if 'json' not in media or not length or not length.isdecimal() or int(length)>MAX_RESPONSE_BYTES: return
            body=await asyncio.wait_for(response.body(),timeout=5)
            if len(body)>MAX_RESPONSE_BYTES or self.bytes_seen+len(body)>MAX_CAPTURE_BYTES: return
            self.bytes_seen+=len(body)
            value=json.loads(body)
            if not isinstance(value,dict) or not isinstance(value.get('doc'),list): return
            for item in value['doc'][:20]:
                data=item.get('data') if isinstance(item,dict) else None
                if not isinstance(data,dict): continue
                if item.get('event')=='exception' or data.get('_doc')=='exception':
                    if data.get('code') in (401,403,429):
                        self.blocked=True; self.blocked_event.set(); self.ready.set()
                    continue
                if len(self.documents)<80: self.documents.append(data)
            feed=extract_passive_feed(self.documents,self.statistics_id)
            if feed and feed_has_other_matches(feed):
                previous={key:value for key,value in self.feed.items() if key!='observed_at'} if self.feed else None
                if previous!=feed:
                    feed['observed_at']=datetime.now(timezone.utc).isoformat()
                    if len(json.dumps(feed,ensure_ascii=False,allow_nan=False).encode())<=MAX_TOTAL_BYTES:
                        self.feed=feed
                self.ready.set()
        except (asyncio.CancelledError,KeyboardInterrupt):
            raise
        except Exception:
            return

    async def close(self):
        self.page.remove_listener('response',self._handler)
        pending=list(self.pending)
        for task in pending: task.cancel()
        if pending: await asyncio.gather(*pending,return_exceptions=True)
