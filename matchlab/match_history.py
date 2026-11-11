"""Normalize participant-linked provider history without inferring finality from age."""
from collections import Counter
from datetime import datetime, timedelta, timezone

from .bookmakers import instant


def identifier(value):
    if type(value) not in (str,int):return None
    text=str(value)
    return str(int(text)) if len(text)<=20 and text.isdecimal() and int(text)>0 else None


def same_team(left,right):
    if not isinstance(left,dict) or not isinstance(right,dict):return False
    for field in ('_id','id','uid'):
        a,b=identifier(left.get(field)),identifier(right.get(field))
        if a and b and a==b:return True
    return False


def row_time(row):
    value=row.get('start_at') or row.get('_dt') or row.get('time') or row.get('date')
    if isinstance(value,dict):value=value.get('uts')
    try:
        return datetime.fromtimestamp(value,tz=timezone.utc) if type(value) in (int,float) else instant(value)
    except (TypeError,ValueError,OverflowError,OSError):return None


def normalize(context,cutoff,archive_rows=(),broad=False,allow_reported=False):
    from .chances import norm
    feed=(context.get('team_statistics') or {}).get('structured_feed') or {}
    anchor=feed.get('match') or {};targets=anchor.get('teams') or {};event=context['event']
    result={'rows':[],'samples':{'home':[],'away':[]},'counts':{}}
    if allow_reported:
        expected=identifier((context.get('team_statistics') or {}).get('statistics_id'))
        team_ids=[identifier((targets.get(s) or {}).get('_id')) for s in ('home','away')]
        if (not expected or identifier(feed.get('statistics_id'))!=expected or identifier(anchor.get('_id'))!=expected or
            not all(team_ids) or team_ids[0]==team_ids[1]):return result
    unique={}
    for side in ('home','away'):
        counts=Counter();seen=set();target=targets.get(side) or {}
        for group in (feed.get('participants') or {}).get(side,[]):
            # analysis_statistics has already verified these participant indices.
            for row in group.get('verified_matches',[]):
                counts['received']+=1
                if not isinstance(row,dict):counts['malformed_row']+=1;continue
                if any(value not in (None,1,'1') for value in (row.get('_sid'),anchor.get('_sid'))):
                    counts['other_sport']+=1;continue
                if identifier(row.get('_id',row.get('id')))==identifier(feed.get('statistics_id')):
                    counts['current_fixture']+=1;continue
                teams=row.get('teams') or {}
                if not isinstance(teams,dict):counts['participant_unverified']+=1;continue
                positions=[p for p in ('home','away') if same_team(teams.get(p),target)]
                if len(positions)!=1:counts['participant_unverified']+=1;continue
                at=row_time(row)
                if at is None:counts['invalid_time']+=1;continue
                if at>=cutoff:counts['current_or_future']+=1;continue
                if not broad and at<cutoff-timedelta(days=365):counts['too_old']+=1;continue
                named=row.get('competition') or row.get('tournament')
                if isinstance(named,dict):named=named.get('name')
                same_comp=norm(named)==norm(event.get('competition')) if named else False
                if not named:
                    for field in ('_utid','_tid'):
                        a,b=identifier(row.get(field)),identifier(anchor.get(field))
                        if a and b:
                            same_comp=a==b;break
                if not same_comp and not broad:counts['other_competition']+=1;continue
                values=row.get('result') or {}
                if not isinstance(values,dict):counts['invalid_score']+=1;continue
                h,a=values.get('home'),values.get('away')
                if type(h) is not int or type(a) is not int or not 0<=min(h,a)<=max(h,a)<=30:
                    counts['invalid_score']+=1;continue
                if any(row.get(k) for k in ('postponed','cancelled','canceled','walkover','retired','disqualified','tobeannounced')):
                    counts['nonstandard_result']+=1;continue
                if values.get('period') not in (None,'nt','regular','regular_time'):
                    counts['nonstandard_period']+=1;continue
                names={}
                for p in ('home','away'):
                    mapped=[s for s in ('home','away') if same_team(teams.get(p),targets.get(s))]
                    names[p]=norm(event.get(mapped[0])) if len(mapped)==1 else norm((teams.get(p) or {}).get('name'))
                state=row.get('status')
                if isinstance(state,dict):state=state.get('_id',state.get('id',state.get('name')))
                final=norm(state) in ('finished','ended','closed','100','ft')
                candidates=[r for r in archive_rows if r['home']==names['home'] and r['away']==names['away']
                            and tuple(r['goals'])==(h,a) and abs((r['at']-at).total_seconds())<=120]
                corroborated=False
                if state in (None,''):
                    corroborated=len(candidates)==1
                    final=corroborated
                reported=False
                if not final and allow_reported:
                    from .widget_results import reported_full_time
                    reported=reported_full_time(row,'football') is not None
                    final=reported
                if not final:counts['unconfirmed_final_status']+=1;continue
                if len(candidates)==1:at=candidates[0]['at']
                key=(at,names['home'],names['away'],h,a)
                if key in seen:counts['duplicate']+=1;continue
                seen.add(key);counts['accepted']+=1
                if not same_comp:counts['other_competition_used']+=1
                if corroborated:counts['archive_confirmed']+=1
                if reported:counts['source_full_time']+=1
                gf,ga=(h,a) if positions[0]=='home' else (a,h)
                result['samples'][side].append({'at':at,'gf':gf,'ga':ga,'scope_weight':1 if same_comp else .5})
                if all(names.values()) and names['home']!=names['away']:
                    unique[key]={'home':names['home'],'away':names['away'],'goals':(h,a),'at':at,'same_competition':same_comp,'scope_weight':1 if same_comp else .5,
                                 'evidence_status':'source_full_time' if reported else 'archive_confirmed' if corroborated else 'explicit_status'}
        result['counts'][side]=dict(counts)
    result['rows']=list(unique.values())
    return result
