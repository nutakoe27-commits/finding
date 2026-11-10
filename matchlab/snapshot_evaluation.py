"""Replay the independent model on retained prematch statistics, without generation."""
from collections import Counter,defaultdict
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3

from .bookmakers import instant
from .chances import assess,INDEPENDENT_MODEL_VERSION
from .match_history import identifier


class ReadOnlyInputs:
    def __init__(self,db):self.db=db

    @contextmanager
    def connection(self):
        # The caller opens SQLite in mode=ro. assess reads data bounded by as_of.
        yield self.db


def recorded_cases(db,results,limit):
    """Use the earliest retained input per fixture; never replace a failed forecast.

    Standalone statistics are bound by the bookmaker's stored statistics ID to
    its archived idRadar. Analyses also have a frozen fixture identity to check.
    Only public match fields enter reports; user notes and accounts stay local.
    """
    from .statistics import analysis_statistics
    from .winline_backtest import match_result
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    by_statistics=defaultdict(list)
    for result in results:
        sid=identifier(result.get('statistics_id'))
        if sid:by_statistics[sid].append(result)
    selected={};rejected=Counter();inventory=Counter();pending=0

    def include(statistics,origin,*,event=None,raw=False,stored_event_id=None,available_at=None):
        nonlocal pending
        inventory['snapshots_examined']+=1
        try:
            if not isinstance(statistics,dict) or not statistics.get('observed_at'):
                rejected['missing_statistics_time']+=1;return
            sid=identifier(statistics.get('statistics_id'))
            if not sid:rejected['missing_statistics_identity']+=1;return
            if stored_event_id is not None and str(statistics.get('source_event_id'))!=str(stored_event_id):
                rejected['statistics_event_mismatch']+=1;return
            if raw and statistics.get('state')!='ok':rejected['statistics_not_successful']+=1;return
            if statistics.get('match_phase') not in ('prematch','unknown'):
                rejected['statistics_live_finished_or_unknown_phase']+=1;return
            target=match_result(event,results) if event else None
            if event:
                if event.get('sport')!='football':rejected['other_sport']+=1;return
                if target is None:pending+=1;rejected['no_unique_finished_result']+=1;return
                if target.get('statistics_id') and identifier(target['statistics_id'])!=sid:
                    rejected['statistics_result_mismatch']+=1;return
            else:
                candidates=by_statistics.get(sid,[])
                if len(candidates)!=1:
                    pending+=1;rejected['no_unique_finished_football_result']+=1;return
                target=candidates[0]
            times=[instant(statistics['observed_at'])]
            for section in statistics.get('sections',[]):
                if section.get('observed_at'):times.append(instant(section['observed_at']))
            feed=statistics.get('structured_feed') or {}
            if feed.get('observed_at'):times.append(instant(feed['observed_at']))
            if statistics.get('attempted_at'):times.append(instant(statistics['attempted_at']))
            if available_at:times.append(instant(available_at))
            as_of=max(times)
            cutoff=target['at']
            if event and event.get('kickoff_at'):cutoff=min(cutoff,instant(event['kickoff_at']))
            if as_of>=cutoff:rejected['statistics_not_before_kickoff']+=1;return
            projected=analysis_statistics(statistics) if raw else deepcopy(statistics)
            if not projected:rejected['statistics_not_successful']+=1;return
            if projected.get('structured_feed'):
                anchor=identifier((projected['structured_feed'].get('match') or {}).get('_id'))
                feed_sid=identifier(projected['structured_feed'].get('statistics_id'))
                if (anchor and anchor!=sid) or (feed_sid and feed_sid!=sid):
                    rejected['statistics_anchor_mismatch']+=1;return
            if event:
                event_id=event.get('source_event_id')
            else:event_id=stored_event_id or statistics.get('source_event_id')
            candidate={'result':target,'as_of':as_of,'statistics':projected,'origin':origin,'event_id':event_id}
            previous=selected.get(target['key'])
            if previous:
                inventory['duplicate_fixture_snapshots']+=1
                if previous['as_of']<=as_of:return
            selected[target['key']]=candidate
        except (ValueError,TypeError,KeyError,OverflowError):rejected['invalid_statistics_metadata']+=1

    if 'event_analyses' in tables:
        for row in db.execute('SELECT payload FROM event_analyses ORDER BY generated_at'):
            try:
                saved=json.loads(row[0]);context=(saved.get('context') or {})
                if context.get('team_statistics'):
                    inventory['analysis_snapshots']+=1
                    include(context['team_statistics'],'saved_analysis',event=context.get('event') or {},available_at=saved.get('generated_at'))
            except (ValueError,TypeError):rejected['invalid_analysis_payload']+=1
    if 'event_statistics' in tables:
        for row in db.execute('SELECT event_id,payload FROM event_statistics WHERE payload IS NOT NULL'):
            inventory['standalone_snapshots']+=1
            try:include(json.loads(row[1]),'event_statistics',raw=True,stored_event_id=row[0])
            except (ValueError,TypeError):rejected['invalid_statistics_payload']+=1
    inventory['eligible_prematch_fixtures']=len(selected)
    cohort=sorted(selected.values(),key=lambda c:(c['result']['at'],c['result']['key']))[-limit:]
    cases=[]
    for candidate in cohort:
        target=candidate['result'];as_of=candidate['as_of'];statistics=candidate['statistics']
        statistics.update(age_seconds=max(0,int((as_of-instant(statistics['observed_at'])).total_seconds())))
        statistics['is_stale']=statistics['age_seconds']>3600
        # Outcome/score/current live fields never enter the simulated input.
        event={'source_event_id':candidate['event_id'],'home':target['home'],'away':target['away'],
               'competition':target['competition'],'sport':'football','sport_label':'Футбол',
               'phase':'prematch','kickoff_at':target['at'].isoformat()}
        context={'event':event,'request_time':{'at':as_of.isoformat(),'kickoff_passed':False},
                 'team_statistics':statistics,'markets':[],
                 'snapshot':{'observed_at':as_of.isoformat(),'is_stale':False,'complete':True}}
        case={'result':target,'forecast_at':as_of.isoformat(),'model_version':INDEPENDENT_MODEL_VERSION,
              'market_results':[],'statistics_snapshot':True,'popular_markets_observed':False,
              'replayed_input':candidate['origin'],'statistics_observed_at':statistics['observed_at']}
        try:
            assessment=assess(ReadOnlyInputs(db),{},context)
            case['probabilities']=assessment['outcome_probabilities']
            case['sample']=assessment['data_quality']['participants']
            case['history_diagnostics']=assessment['history_diagnostics']
            case['limitations']=assessment['limitations']
            if case['probabilities'] is None:rejected['insufficient_snapshot_evidence']+=1
        except (ValueError,TypeError,KeyError,sqlite3.Error):
            rejected['snapshot_evaluation_failed']+=1;case.update(probabilities=None,sample=[])
        cases.append(case)
    return cases,dict(rejected),pending,dict(inventory)
