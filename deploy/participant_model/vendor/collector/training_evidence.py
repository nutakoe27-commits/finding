"""Source-bound pre-match format and observed schedule-revision evidence.

All reads use the caller's owned collector connection. Later schedule records
only validate label identity; they never replace frozen features or fold times.
"""

import json
import re

from .outcomes import name_key
from .store import digest, instant


def participant_key(value):
    # NFKC maps the nonbreaking hyphen U+2011 to U+2010. The UI substitutes
    # this glyph for the native ASCII hyphen; retain all other punctuation.
    return name_key(value).replace('\u2010','-')


def identifier(value):
    text=str(value)
    return int(text) if not isinstance(value,bool) and re.fullmatch(r'[0-9]{1,20}',text) and int(text)>0 else None


def native_at(db, event_id, cutoff):
    row=db.execute("SELECT id,payload,sha256,observed_at,received_at FROM observations WHERE kind='event' AND entity_id=? "
        "AND julianday(observed_at)<=julianday(?) AND julianday(received_at)<=julianday(?) "
        "ORDER BY julianday(observed_at) DESC,id DESC LIMIT 1",(event_id,cutoff.isoformat(),cutoff.isoformat())).fetchone()
    if not row:return None
    value=json.loads(row['payload'])
    return (row,value) if (isinstance(value,dict) and digest(value)==row['sha256'] and
        max(instant(row['observed_at']),instant(row['received_at']))<=cutoff) else None


def schedule_revision(db, event, widget, outcome, as_of, received_at):
    """Allow a documented delay only with exact native/provider ID continuity."""
    if outcome.get('method')!='sport_radar_names_time':return None
    radar=identifier(outcome.get('radar_id'))
    if not radar or radar!=identifier(widget.get('statistics_id')):return None
    if instant(outcome['kickoff_at'])<=instant(event['kickoff_at']):return None
    before=native_at(db,event['source_event_id'],instant(as_of))
    after=native_at(db,event['source_event_id'],instant(received_at))
    if not before or not after or before[0]['id']==after[0]['id']:return None
    expected=[participant_key(event.get(side)) for side in ('home','away')]
    for (row,value),kickoff in ((before,event['kickoff_at']),(after,outcome['kickoff_at'])):
        if (identifier(value.get('id'))!=identifier(event['source_event_id']) or
            type(value.get('idSport')) is not int or value.get('idSport')!=outcome['sport_id'] or
            identifier(value.get('idRadar'))!=radar or
            not isinstance(value.get('members'),list) or
            [participant_key(member) for member in value['members']]!=expected or
            # UI timestamps have minute precision; native values include seconds.
            abs((instant(value['date'])-instant(kickoff)).total_seconds())>60):return None
    return {'method':'observed_schedule_revision','original_kickoff_at':event['kickoff_at'],
            'revised_kickoff_at':outcome['kickoff_at'],
            'observations':[{'id':row['id'],'sha256':row['sha256'],'received_at':row['received_at']}
                            for row,_ in (before,after)]}


def frozen_format_documents(db, event, widget, as_of):
    """Recover only target replies from the SAME capture available by as-of."""
    capture=widget.get('capture_id');statistics_id=str(widget.get('statistics_id') or '')
    if not capture:return [],[],None
    # These provider IDs are independently visible in the user's raw set-sport
    # records. They are not derived from native ResultService IDs.
    provider_id={'tennis':5,'volleyball':23}.get(event['sport'])
    documents=[];provenance=[]
    rows=db.execute("SELECT id,payload,sha256,observed_at,received_at FROM observations WHERE kind='widget_json' AND entity_id=? "
        "AND julianday(observed_at)<=julianday(?) AND julianday(received_at)<=julianday(?) "
        "AND json_extract(payload,'$.capture_id')=? AND CAST(json_extract(payload,'$.statistics_id') AS TEXT)=? "
        "ORDER BY id",(event['source_event_id'],as_of,as_of,capture,statistics_id))
    for row in rows:
        packet=json.loads(row['payload'])
        if max(instant(row['observed_at']),instant(row['received_at']))>instant(as_of):continue
        if not isinstance(packet,dict):return [],[],'format_source_integrity_mismatch'
        parts=(packet.get('endpoint') or {}).get('path','').split('/gismo/')
        route=parts[-1].split('/') if len(parts)==2 else []
        if (len(route)<2 or route[0] not in ('match_info_statshub','stats_match_get') or
                route[1]!=statistics_id):continue
        if (digest(packet)!=row['sha256'] or packet.get('format')!='doscbot.widget-json.v1' or
                packet.get('binding')!='observed_event_link'):return [],[],'format_source_integrity_mismatch'
        try:
            if instant(packet['observed_at'])!=instant(row['observed_at']):return [],[],'format_source_integrity_mismatch'
        except (KeyError,TypeError,ValueError):
            return [],[],'format_source_integrity_mismatch'
        if packet.get('http_status')!=200 or packet.get('truncated') is True:return [],[],'format_source_unusable'
        body=packet.get('data');docs=body.get('doc') if isinstance(body,dict) else None
        if not isinstance(docs,list):continue
        for document in docs:
            if not isinstance(document,dict):continue
            data=document.get('data')
            if not isinstance(data,dict):continue
            match=data.get('match') if route[0]=='match_info_statshub' else data
            if not isinstance(match,dict) or str(match.get('_id'))!=statistics_id:continue
            teams=match.get('teams') or {};target=widget.get('match') or {}
            before=[identifier((target.get(side) or {}).get('id')) for side in ('home','away')]
            after=[identifier((teams.get(side) or {}).get('uid') or (teams.get(side) or {}).get('_id')) for side in ('home','away')]
            if not all(before) or before!=after or type(match.get('_sid')) is not int or match['_sid']!=provider_id:
                return [],[],'format_source_identity_unconfirmed'
            status=match.get('status');status=status.get('_id') if isinstance(status,dict) else status
            if (isinstance(status,bool) or status not in (None,0,'0') or any((match.get('result') or {}).get(key) is not None for key in ('home','away','winner')) or
                any(match.get(key) is True for key in ('retired','walkover','disqualified','canceled','postponed'))):
                return [],[],'format_source_not_prematch'
            prefix='match.' if route[0]=='match_info_statshub' else ''
            fields=[{'path':prefix+key,'value':match.get(key)} for key in ('_id','bestof','numberofperiods')]
            documents.append({'name':route[0],'fields':fields})
            provenance.append({'observation_id':row['id'],'sha256':row['sha256'],
                               'observed_at':row['observed_at'],'received_at':row['received_at']})
    return documents,provenance,None
