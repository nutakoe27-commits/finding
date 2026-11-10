"""Price invariance, real provider field shapes and conservative final-result binding."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from matchlab.archive import project
from matchlab.chances import assess,goal_probabilities,report_markets
from matchlab.event_analysis import analysis_context
from matchlab.independent_context import independent_context
from matchlab.market_focus import focus_context
from matchlab.match_history import normalize
from matchlab.config import Settings
from matchlab import qwen
from test_archive import header
from test_chances import market,rows
from test_market_focus import pool
from reasoning_helpers import completion

NOW=datetime.now(timezone.utc)


def provider_context():
    row={'_id':88,'_utid':1072,'_tid':6573,'time':{'uts':int((NOW-timedelta(days=2)).timestamp())},
         'status':{'_id':100},'result':{'home':2,'away':1,'period':'nt'},
         'teams':{'home':{'_id':10,'uid':50,'name':'Alpha'},'away':{'_id':20,'name':'Beta'}}}
    return {'event':{'home':'Alpha','away':'Beta','competition':'League'},'team_statistics':{'structured_feed':{
        'statistics_id':'999','match':{'_utid':1072,'_tid':6573,
          'teams':{'home':{'_id':10,'uid':50},'away':{'_id':20}}},
        'participants':{'home':[{'verified_matches':[row],'unverified_matches':[]}],'away':[]}}}}


class IndependentTests(unittest.TestCase):
    def context(self):
        doc=project(header());doc['home']='A';doc['away']='B'
        return doc,analysis_context(doc)

    def test_price_mutation_and_removal_leave_fitted_chances_identical(self):
        doc,ctx=self.context()
        with patch('matchlab.chances.football_sample',return_value=rows()):
            first=assess(None,doc,ctx)
            changed=deepcopy(ctx)
            for m in changed['markets']:
                for i,o in enumerate(m['outcomes']):o['odds']=1.01 if i==0 else 10000
            second=assess(None,doc,changed)
            for m in changed['markets']:
                for o in m['outcomes']:o.pop('odds')
            third=assess(None,doc,changed)
        self.assertIsNotNone(first['winner'])
        self.assertEqual(first,second);self.assertEqual(first,third)
        self.assertAlmostEqual(sum(first['outcome_probabilities'].values()),1)
        self.assertTrue(all(r['basis']=='history' and 'odds' not in r for r in first['markets']))

    def test_missing_history_never_falls_back_to_implied_chances(self):
        doc,ctx=self.context()
        for o in ctx['markets'][0]['outcomes']:o['odds']=1.00001
        with patch('matchlab.chances.football_sample',return_value=[]):result=assess(None,doc,ctx)
        self.assertIsNone(result['winner']);self.assertEqual(result['markets'],[])
        self.assertIsNone(result['outcome_probabilities'])
        self.assertIn('вероятности не рассчитаны',' '.join(result['limitations']))

    def test_prediction_survives_old_or_absent_quotes_when_history_is_sufficient(self):
        doc,ctx=self.context()
        with patch('matchlab.chances.football_sample',return_value=rows()):
            reference=assess(None,doc,ctx)
            ctx['snapshot']['is_stale']=True
            for m in ctx['markets']:m['is_stale']=True
            old=assess(None,doc,ctx)
            ctx['markets']=[]
            absent=assess(None,doc,ctx)
        self.assertEqual(reference['outcome_probabilities'],old['outcome_probabilities'])
        self.assertEqual(old['outcome_probabilities'],absent['outcome_probabilities'])
        self.assertIsNone(absent['winner']['market_key'])
        ctx['probability_assessment']=absent
        self.assertEqual(focus_context(ctx)['probability_assessment']['winner'],absent['winner'])

    def test_market_shortlist_does_not_depend_on_bookmaker_prices(self):
        ctx=pool();first=focus_context(ctx)['market_selection']['selected_keys']
        for m in ctx['markets']:
            for i,o in enumerate(m['outcomes']):o['odds']=999 if i==0 else 1.0001
        self.assertEqual(first,focus_context(ctx)['market_selection']['selected_keys'])
        for m in ctx['markets']:
            for o in m['outcomes']:del o['odds']
        self.assertEqual(first,focus_context(ctx)['market_selection']['selected_keys'])

    def test_boundary_removes_nested_prices_and_legacy_implied_assessments(self):
        value=pool();value['market_implied']={'probabilities':{'1':.99}}
        value['team_statistics']['structured_feed']={'odds':{'home':1.0001},'result':{'home':2,'away':1}}
        clean=independent_context(value)
        self.assertIsNone(clean['market_implied']);self.assertIsNone(clean['probability_assessment']['winner'])
        self.assertEqual(clean['probability_assessment']['markets'],[])
        self.assertNotIn('"odds"',json.dumps(clean));self.assertNotIn('1.0001',json.dumps(clean))
        self.assertEqual(clean['team_statistics']['structured_feed']['result'],{'home':2,'away':1})
        self.assertIn('odds',value['team_statistics']['structured_feed'])

    def test_actual_qwen_boundary_never_sends_prices(self):
        doc,ctx=self.context();ctx['team_statistics']={'state':'ok','odds':1.77777}
        with patch('matchlab.chances.football_sample',return_value=rows()):ctx['probability_assessment']=assess(None,doc,ctx)
        requests=[]
        def complete(settings,messages):
            requests.append(messages);return completion(settings,messages)
        with patch('matchlab.qwen._request',side_effect=ValueError('HTTP 404')),patch('matchlab.qwen._completion',side_effect=complete):
            text=qwen.explain_event(Settings(Path('/tmp'),'http://localhost:9/v1','fixture',qwen_batch_tokens=128000),ctx)
        self.assertTrue(requests);self.assertNotIn('по линии',text)
        for messages in requests:
            self.assertNotIn('"odds"',messages[1]['content']);self.assertNotIn('1.77777',messages[1]['content'])
        self.assertTrue(ctx['probability_assessment']['independent_only'])

    def test_market_recommendations_ignore_odds_threshold(self):
        row={'market_key':'m','basis':'history','probability':.7,'odds':1.01}
        self.assertEqual(report_markets({'markets':[row]}),[row])
        del row['odds'];self.assertEqual(report_markets({'markets':[row]}),[row])

    def test_source_total_rows_use_conditions_only_and_include_refunds(self):
        grid=np.array([[.1,.1,.1],[.1,.1,.1],[.1,.1,.2]])
        over=market('Тотал',['2','2.5'],parameter='Больше')
        under=market('Тотал',['2','2.5'],parameter='Меньше')
        for m in (over,under):
            for o in m['outcomes']:del o['odds']
        a,b=goal_probabilities(over,grid),goal_probabilities(under,grid)
        self.assertAlmostEqual(a['2']['probability']+b['2']['probability']+a['2']['push_probability'],1)
        self.assertAlmostEqual(a['2.5']['probability']+b['2.5']['probability'],1)


class ProviderHistoryTests(unittest.TestCase):
    def row(self,context):return context['team_statistics']['structured_feed']['participants']['home'][0]['verified_matches'][0]

    def test_provider_time_and_tournament_ids_are_understood(self):
        ctx=provider_context();result=normalize(ctx,NOW)
        self.assertEqual(result['samples']['home'][0]['gf'],2)
        self.assertEqual(result['counts']['home']['accepted'],1)
        self.assertEqual(result['rows'][0]['home'],'alpha')

    def test_absent_status_requires_matching_archived_final_result(self):
        ctx=provider_context();r=self.row(ctx);r['status']=None
        self.assertEqual(normalize(ctx,NOW)['samples']['home'],[])
        at=datetime.fromtimestamp(r['time']['uts'],tz=timezone.utc)
        final={'home':'alpha','away':'beta','at':at+timedelta(seconds=20),'goals':(2,1)}
        accepted=normalize(ctx,NOW,[final])
        self.assertEqual(accepted['counts']['home']['archive_confirmed'],1)
        self.assertEqual(accepted['rows'][0]['at'],final['at'])
        wrong={**final,'goals':(3,1)}
        self.assertEqual(normalize(ctx,NOW,[wrong])['samples']['home'],[])
        r['status']='live'
        self.assertEqual(normalize(ctx,NOW,[final])['samples']['home'],[])

    def test_future_other_competition_cancelled_and_unlinked_rows_are_excluded(self):
        variants=({'_utid':999},{'_sid':2},{'time':{'uts':int((NOW+timedelta(days=1)).timestamp())}},
                  {'time':{'uts':int((NOW-timedelta(days=366)).timestamp())}},
                  {'canceled':True},{'retired':True},{'result':{'home':2,'away':1,'period':'ot'}},
                  {'result':['invalid']},{'teams':['invalid']},
                  {'teams':{'home':{'_id':999},'away':{'_id':998}}})
        for changes in variants:
            ctx=provider_context();self.row(ctx).update(changes)
            with self.subTest(changes=changes):self.assertEqual(normalize(ctx,NOW)['samples']['home'],[])
        ctx=provider_context();g=ctx['team_statistics']['structured_feed']['participants']['home'][0]
        g['unverified_matches']=g['verified_matches'];g['verified_matches']=[]
        self.assertEqual(normalize(ctx,NOW)['samples']['home'],[])

    def test_alias_uses_same_id_namespace_and_duplicate_groups_count_once(self):
        ctx=provider_context();r=self.row(ctx);r['teams']['home']['_id']=11
        groups=ctx['team_statistics']['structured_feed']['participants']['home']
        groups.append(deepcopy(groups[0]))
        result=normalize(ctx,NOW)
        self.assertEqual(len(result['samples']['home']),1)
        self.assertEqual(result['counts']['home']['duplicate'],1)


if __name__=='__main__':unittest.main()
