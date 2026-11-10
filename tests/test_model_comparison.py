"""Distribution invariants, odds provenance and fixed paired evaluation cohorts."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zlib

from matchlab.bookmakers import make_snapshot
from matchlab.model_comparison import (compare,devig,dixon_coles,historical_quotes,negative_binomial,
                                      poisson_outcomes,recover_rates,variants,history_predictions)
from matchlab.winline_backtest import KEYS,valid_probabilities
from matchlab.storage import Repository

NOW=datetime.now(timezone.utc)


def sample_report():
    rows=[]
    for i,(h,a,score,truth) in enumerate([(1.4,.9,'2:1','home'),(.8,1.6,'1:1','draw'),(1.1,1.4,'0:2','away')]):
        rows.append({'result_key':str(i),'home':'A'+str(i),'away':'B'+str(i),'competition':'League',
                     'kickoff_at':NOW.isoformat(),'forecast_at':(NOW-timedelta(hours=2)).isoformat(),
                     'actual_score':score,'actual_outcome':truth,'probabilities':poisson_outcomes(h,a)})
    return {'format':'matchlab.winline-evaluation.v1','matches':rows,'model_version':'independent-football-v19','mode':'recorded-statistics'}


def quote_for(case,minutes_before=1,prices=(2,3.4,3.8)):
    return make_snapshot('winline',case['competition'],case['home'],case['away'],case['kickoff_at'],
                         (datetime.fromisoformat(case['forecast_at'])-timedelta(minutes=minutes_before)).isoformat(),
                         list(prices),'https://winline.ru/stavki/sport/futbol/123')


class ComparisonTests(unittest.TestCase):
    def test_poisson_rates_recover_the_original_distribution(self):
        for h,a in [(.05,.3),(1.4,.9),(3,4),(9,1),(20,20)]:
            with self.subTest(home=h,away=a):
                p=poisson_outcomes(h,a);means=recover_rates(p);recreated=poisson_outcomes(*means)
                self.assertLess(max(abs(p[k]-recreated[k]) for k in KEYS),1e-7)

    def test_zero_dixon_coles_correction_and_large_shape_match_poisson(self):
        p=poisson_outcomes(1.4,.9)
        for corrected,tolerance in [(dixon_coles(1.4,.9,0),1e-10),(negative_binomial(1.4,.9,100000),1e-5)]:
            self.assertLess(max(abs(p[k]-corrected[k]) for k in KEYS),tolerance)

    def test_all_models_are_normalized_symmetric_under_team_swap_and_bounded(self):
        for h,a in [(1.4,.9),(25,20),(.01,.03)]:
            for model in (lambda h,a:dixon_coles(h,a,-.1),lambda h,a:negative_binomial(h,a,5)):
                direct=model(h,a);reverse=model(a,h)
                self.assertTrue(valid_probabilities(direct))
                self.assertAlmostEqual(direct['home'],reverse['away'])
                self.assertAlmostEqual(direct['draw'],reverse['draw'])

    def test_smoothing_reduces_confidence_without_changing_the_favorite(self):
        p=poisson_outcomes(2,.6);values,_=variants(p)
        for key in ('shrink_25','shrink_50','temperature_150'):
            self.assertEqual(max(values[key],key=values[key].get),max(p,key=p.get))
            self.assertLess(max(values[key].values()),max(p.values()))

    def test_non_poisson_input_is_not_silently_used_as_a_goal_model(self):
        values,errors=variants({'home':0,'draw':1,'away':0})
        self.assertNotIn('dixon_coles_010',values)
        self.assertEqual(errors['dixon_coles_010'],'poisson_reconstruction_failed')

    def test_zero_margin_prices_are_unchanged_by_devig(self):
        methods=devig({'home':2,'draw':4,'away':4})
        for p in methods.values():
            self.assertAlmostEqual(p['home'],.5)
            self.assertAlmostEqual(p['draw'],.25)
            self.assertAlmostEqual(p['away'],.25)

    def test_shin_solves_one_common_insider_parameter_not_simple_normalization(self):
        odds={'home':2.6,'draw':2.4,'away':4.3};values=devig(odds)
        q={k:1/odds[k] for k in KEYS};total=sum(q.values());p=values['odds_shin']
        z=[(q[k]**2/total-p[k]**2)/(p[k]-p[k]**2) for k in KEYS]
        self.assertTrue(0<z[0]<1);self.assertLess(max(z)-min(z),1e-10)
        self.assertTrue(valid_probabilities(p))
        self.assertLess(p['away'],values['odds_proportional']['away'])
        self.assertGreater(p['draw'],values['odds_proportional']['draw'])

    def test_underround_does_not_fabricate_a_shin_solution(self):
        values=devig(dict.fromkeys(KEYS,4))
        self.assertNotIn('odds_shin',values)
        self.assertTrue(valid_probabilities(values['odds_power']))
        for bad in ({'home':2,'draw':3},{'home':True,'draw':3,'away':4},{'home':float('inf'),'draw':3,'away':4}):
            with self.subTest(bad=bad),self.assertRaises(ValueError):devig(bad)

    def test_very_short_valid_prices_do_not_break_root_bracketing(self):
        for price in (1.001,1.00001,math.nextafter(1,2)):
            for values in devig(dict.fromkeys(KEYS,price)).values():
                self.assertTrue(valid_probabilities(values))
                self.assertAlmostEqual(values['home'],1/3)

    def test_missing_prices_are_reported_without_substitution(self):
        result=compare(sample_report())
        self.assertEqual(result['common_cases'],3)
        self.assertTrue(all(row['coverage']==0 and row['metrics'] is None for row in result['market_models']))
        self.assertEqual(result['market_exclusions']['missing_historical_odds'],3)

    def test_quote_after_the_forecast_is_rejected_even_if_still_prematch(self):
        source=sample_report();case=source['matches'][0]
        result=compare(source,[quote_for(case,minutes_before=-1)])
        self.assertEqual(result['market_models'][0]['coverage'],0)

    def test_market_and_model_metrics_use_the_exact_same_matched_subset(self):
        source=sample_report();result=compare(source,[quote_for(source['matches'][0])])
        for row in result['market_models']:
            self.assertEqual(row['coverage'],1)
            self.assertEqual(row['metrics']['matches'],1)
            self.assertEqual(row['paired_current_model']['matches'],1)
        self.assertEqual(result['models'][0]['metrics']['matches'],3)

    def test_conflicting_prices_at_the_same_timestamp_are_not_cherry_picked(self):
        source=sample_report();case=source['matches'][0]
        result=compare(source,[quote_for(case),quote_for(case,prices=(2.5,3,3))])
        self.assertEqual(result['market_models'][0]['coverage'],0)
        self.assertEqual(result['market_exclusions']['ambiguous_historical_odds'],1)

    def test_outcome_labels_never_change_candidate_probabilities(self):
        source=sample_report();first=compare(source)
        changed=deepcopy(source);changed['matches'][0].update(actual_score='0:1',actual_outcome='away')
        second=compare(changed)
        self.assertEqual([c['predictions'] for c in first['matches']],[c['predictions'] for c in second['matches']])
        self.assertNotEqual(first['models'][0]['metrics'],second['models'][0]['metrics'])

    def test_null_original_forecasts_are_excluded_from_every_shared_cohort(self):
        source=sample_report();source['matches'][0]['probabilities']=None
        result=compare(source)
        self.assertEqual(result['source_unestimated'],1);self.assertEqual(result['common_cases'],2)
        self.assertTrue(all(row['coverage']==2 for row in result['models']))
        uniform=next(row for row in result['models'] if row['method']=='uniform')
        self.assertIsNone(uniform['metrics']['accuracy'])
        self.assertAlmostEqual(uniform['metrics']['brier_3way'],2/3)

    def test_duplicates_inconsistent_scores_and_private_notes_are_not_accepted_as_input_features(self):
        source=sample_report();source['matches'][0]['notes']='PRIVATE_NOTE_MARKER'
        self.assertNotIn('PRIVATE_NOTE_MARKER',json.dumps(compare(source)))
        source['matches'].append(deepcopy(source['matches'][0]))
        with self.assertRaises(ValueError):compare(source)
        source=sample_report();source['matches'][0]['actual_outcome']='draw'
        with self.assertRaises(ValueError):compare(source)

    def test_historical_quote_reader_rejects_non_90_minute_and_live_data(self):
        good=quote_for(sample_report()['matches'][0])
        late=deepcopy(good);late['observed_at']=(NOW+timedelta(seconds=1)).isoformat()
        half=deepcopy(good);half['market']='1x2_first_half'
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE bookmaker_quotes(source TEXT,payload TEXT)')
            for row in [good,late,half]:db.execute('INSERT INTO bookmaker_quotes VALUES (?,?)',('winline',json.dumps(row)))
            self.assertEqual(len(historical_quotes(db)),1)

    def test_elo_davidson_has_a_draw_outcome_and_handles_unknown_participants(self):
        rows=[{'home':'a','away':'b','goals':(2,0),'at':NOW-timedelta(days=i+1)} for i in range(10)]
        values,counts=history_predictions(rows,'a','b')
        self.assertGreater(values['elo_davidson']['home'],values['elo_davidson']['away'])
        self.assertGreater(values['elo_davidson']['draw'],0)
        self.assertTrue(valid_probabilities(values['elo_davidson']))
        self.assertEqual(counts['competition_matches'],10)
        self.assertNotIn('elo_davidson',history_predictions(rows,'a','unknown')[0])

    def test_server_history_rejects_results_received_after_the_forecast(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Path(folder)/'matchlab.sqlite3');repo.initialize()
            at=(NOW-timedelta(days=2)).isoformat();source=sample_report();source['matches'][0].update(home='A',away='B')
            data={'member1':'A','member2':'B','championship':'League','score':'2:0','date':at}
            with repo.connection() as db:
                db.execute('INSERT INTO archive_latest_results VALUES (?,?,?,?,?,?,?,?,?,?)',
                    ('old',1,'Футбол',at,'A','B','2:0',NOW.isoformat(),NOW.isoformat(),json.dumps(data)))
                late=compare(source,db=db)
                self.assertEqual(late['matches'][0]['history_training']['competition_matches'],0)
                self.assertNotIn('elo_davidson',late['matches'][0]['predictions'])
                earlier=(NOW-timedelta(hours=3)).isoformat()
                db.execute('UPDATE archive_latest_results SET received_at=?,observed_at=?',(earlier,earlier))
                early=compare(source,db=db)
                self.assertEqual(early['matches'][0]['history_training']['competition_matches'],1)
                self.assertIn('elo_davidson',early['matches'][0]['predictions'])
                elo=next(row for row in early['history_models'] if row['method']=='elo_davidson')
                self.assertEqual(elo['coverage'],1)
                self.assertEqual(elo['paired_current_model']['matches'],1)
                self.assertEqual(late['matches'][0]['predictions']['poisson_v19'],early['matches'][0]['predictions']['poisson_v19'])

    def test_quotes_from_another_fixture_are_not_selected_by_more_recent_time(self):
        source=sample_report();first=quote_for(source['matches'][0]);first['source_event_id']='111'
        second=quote_for(source['matches'][0]);second['source_event_id']='222'
        result=compare(source,[first,second])
        self.assertEqual(result['market_models'][0]['coverage'],0)
        self.assertEqual(result['market_exclusions']['ambiguous_quote_fixture'],1)

    def test_immutable_market_snapshots_are_usable_only_before_the_forecast(self):
        source=sample_report();case=source['matches'][0]
        at=(datetime.fromisoformat(case['forecast_at'])-timedelta(minutes=1)).isoformat()
        context={'event':{**{k:case[k] for k in ('home','away','competition','kickoff_at')},'sport':'football','phase':'prematch',
                          'url':'https://winline.ru/stavki/sport/futbol/123'},'snapshot':{'observed_at':at},
                 'markets':[{'name':'Исход 1X2','period':'осн.время','outcomes_complete':True,
                             'outcomes':[{'name':name,'odds':price,'state':'open'} for name,price in [('П1',2),('X',3.4),('П2',3.8)]]}]}
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE event_analyses(payload TEXT)')
            db.execute('INSERT INTO event_analyses VALUES (?)',(json.dumps({'context':context,'notes':'PRIVATE_NOTE'}),))
            quotes=historical_quotes(db)
            self.assertEqual(len(quotes),1)
            self.assertEqual(compare(source,quotes)['market_models'][0]['coverage'],1)
            context['snapshot']['observed_at']=(NOW-timedelta(hours=1)).isoformat()
            context['markets'][0]['observed_at']=at
            db.execute('DELETE FROM event_analyses')
            db.execute('INSERT INTO event_analyses VALUES (?)',(json.dumps({'context':context}),))
            self.assertEqual(compare(source,historical_quotes(db))['market_models'][0]['coverage'],0)

    def test_retained_marketbook_can_supply_a_timed_complete_1x2_snapshot(self):
        source=sample_report();case=source['matches'][0]
        at=(datetime.fromisoformat(case['forecast_at'])-timedelta(minutes=1)).isoformat()
        event={**{k:case[k] for k in ('home','away','competition','kickoff_at')},'sport':'football','phase':'prematch',
               'capture_started_at':at,'url':'https://winline.ru/stavki/sport/futbol/123'}
        document={'markets':[{'name':'Исход 1X2','period':'осн.время',
                              'outcomes':[{'name':name,'odds':price,'state':'open'} for name,price in [('П1',2),('X',3.4),('П2',3.8)]]}]}
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE bookmaker_event_markets(phase TEXT,metadata TEXT,payload BLOB)')
            db.execute('INSERT INTO bookmaker_event_markets VALUES (?,?,?)',('prematch',json.dumps(event),zlib.compress(json.dumps(document).encode())))
            result=compare(source,historical_quotes(db))
            self.assertEqual(next(row for row in result['market_models'] if row['method']=='odds_shin')['coverage'],1)


if __name__=='__main__':unittest.main()
