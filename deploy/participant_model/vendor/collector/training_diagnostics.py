"""Bounded, read-only source evidence for unlabelled training events.

Diagnostics never supply a label or enter the feature matrix. In particular,
a later historical match record is evidence to inspect, not a fallback target.
"""

import json

from .store import digest, instant


def pick(value, keys):
    value=value if isinstance(value,dict) else {}
    return {key:item[:200] if isinstance(item,str) else item for key in keys
            if (item:=value.get(key)) is None or isinstance(item,(str,int,float,bool))}


def source_match(value):
    value=value if isinstance(value,dict) else {}
    teams=value.get('teams') or {}
    status=value.get('status')
    return {**pick(value,('_id','_sid','bestof','numberofperiods','retired','walkover','disqualified','canceled','postponed')),
            'status':pick(status,('_id','name')) if isinstance(status,dict) else status,
            'result':pick(value.get('result'),('home','away','period','winner')),
            'teams':{side:pick(teams.get(side),('_id','uid','name')) for side in ('home','away')},
            'periods':{key:pick(score,('home','away')) for key,score in list((value.get('periods') or {}).items())[:8]}}


def example(db, row, payload, reason, result, cutoff):
    event=payload['event'];widget=payload['widget_data'];target=widget.get('match') or {}
    output={'event_id':row['event_id'],'sport':event['sport'],'reason':reason,'sample_id':row['id'],
            'as_of':row['as_of'],'label_cutoff':cutoff.isoformat(),'statistics_id':widget.get('statistics_id'),
            'capture_id':widget.get('capture_id'),
            'event':pick(event,('home','away','kickoff_at','competition')),
            'widget_match':{**pick(target,('best_of','number_of_periods','kickoff_at')),
                'participants':{side:pick(target.get(side),('id','record_id','name')) for side in ('home','away')},
                'status':pick(target.get('status'),('_id','name'))},
            'format_fields':[field for document in widget.get('documents',[]) if document.get('name')=='match_info_statshub'
                for field in document.get('fields',[]) if field.get('path') in ('match._id','match.bestof','match.numberofperiods')][:6],
            'saved_source_context':[]}
    if result:
        record=db.execute('SELECT payload FROM observations WHERE id=?',(result['observation_id'],)).fetchone()
        value=json.loads(record['payload'])
        output['linked_result']={**pick(value,('event_id','sport_id','radar_id','home','away','kickoff_at','score_text','competition','method')),
                                 'received_at':result['received_at']}
    if reason=='outcome_identity_unconfirmed':
        record=db.execute("SELECT payload,sha256 FROM observations WHERE kind='event' AND entity_id=? "
            "AND julianday(observed_at)<=julianday(?) AND julianday(received_at)<=julianday(?) "
            "ORDER BY julianday(observed_at) DESC,id DESC LIMIT 1",(row['event_id'],row['as_of'],row['as_of'])).fetchone()
        if record:
            value=json.loads(record['payload'])
            if digest(value)==record['sha256']:
                output['native_event_at_sample']={**pick(value,('id','idSport','idRadar','date')),
                    'members':[pick(member,('name',)) if isinstance(member,dict) else str(member)[:200]
                               for member in (value.get('members') or [])[:2]]}
    return output


def saved_context(db, examples):
    """Stream existing packets once; show at most one pre/post record per event."""
    wanted={str(item['statistics_id']):item for item in examples
            if item['sport'] in ('tennis','volleyball') and item['reason']=='best_of_unconfirmed'}
    if not wanted:return
    found=set()
    query="""SELECT id,entity_id,observed_at,received_at,sha256,payload FROM observations WHERE kind='widget_json'
        AND (json_extract(payload,'$.endpoint.path') LIKE '%/match_info_statshub/%'
          OR json_extract(payload,'$.endpoint.path') LIKE '%/stats_match_get/%'
          OR json_extract(payload,'$.endpoint.path') LIKE '%/stats_player_lastx/%'
          OR json_extract(payload,'$.endpoint.path') LIKE '%/stats_team_lastx/%') ORDER BY id DESC"""
    for row in db.execute(query):
        try:
            packet=json.loads(row['payload'])
            if packet.get('format')!='doscbot.widget-json.v1' or packet.get('binding')!='observed_event_link':continue
            route=(packet.get('endpoint') or {}).get('path','')
            history=any('/'+name+'/' in route for name in ('stats_player_lastx','stats_team_lastx'))
            for document in (packet.get('data') or {}).get('doc',[]):
                data=document.get('data')
                if not isinstance(data,dict):continue
                matches=data.get('matches',[]) if history else [data.get('match',data)]
                for match in matches:
                    if not isinstance(match,dict):continue
                    identifier=str(match.get('_id'));item=wanted.get(identifier)
                    kind='later_history' if history else 'frozen_capture'
                    if item is None or (identifier,kind) in found:continue
                    if max(instant(row['observed_at']),instant(row['received_at']))>instant(item['label_cutoff']):continue
                    if history:
                        if instant(row['observed_at'])<=instant(item['event']['kickoff_at']):continue
                    elif (row['entity_id']!=item['event_id'] or not item['capture_id'] or
                          packet.get('capture_id')!=item['capture_id'] or
                          max(instant(row['observed_at']),instant(row['received_at']))>instant(item['as_of'])):continue
                    if digest(packet)!=row['sha256']:continue
                    evidence=source_match(match)
                    before=[item['widget_match']['participants'][side].get('id') for side in ('home','away')]
                    after=[str((match.get('teams',{}).get(side) or {}).get('uid') or
                               (match.get('teams',{}).get(side) or {}).get('_id') or '') for side in ('home','away')]
                    item['saved_source_context'].append({'kind':kind,'observation_id':row['id'],
                        'received_at':row['received_at'],'observed_at':row['observed_at'],
                        'same_ordered_participant_ids':all(before) and before==after,'match':evidence})
                    found.add((identifier,kind))
        except (KeyError,TypeError,ValueError,AttributeError):
            continue
