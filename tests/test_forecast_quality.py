"""Outcome evaluation must never use the target result or postmatch statistics."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from matchlab.api import create_app
from matchlab.chances import football_result,football_sample,report_markets
from matchlab.config import Settings
from matchlab.event_reasoning import analyze,verified_observations,trusted_facts
from matchlab.storage import Repository
from matchlab.winline_backtest import run,save,settled_markets
from account_helpers import signed_in
from test_event_reasoning import FakeModel,context_large
from reasoning_helpers import EXPLANATION

NOW=datetime.now(timezone.utc)


def seed(repo,n=20):
    with repo.connection() as db:
        for i in range(n):
            at=NOW-timedelta(days=n-i+2)
            value={'date':at.isoformat(),'member1':'A' if i%2 else 'B','member2':'B' if i%2 else 'A',
                   'championship':'League','score':'2:1 (1:0 1:1)' if i%3 else '0:0 (0:0 0:0)'}
            db.execute('INSERT INTO archive_latest_results VALUES (?,?,?,?,?,?,?,?,?,?)',
                (str(i),1,'Футбол',at.isoformat(),value['member1'],value['member2'],value['score'],NOW.isoformat(),NOW.isoformat(),json.dumps(value)))


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.repo=Repository(Path(self.folder.name)/'matchlab.sqlite3');self.repo.initialize();seed(self.repo)

    def test_archive_scores_with_two_halves_are_used_and_ambiguous_periods_rejected(self):
        self.assertEqual(football_result('2:1 (1:0 1:1)'),{'full':(2,1),'half':(1,0)})
        self.assertEqual(football_result('0:0 (0:0 0:0)')['full'],(0,0))
        self.assertEqual(football_result('2:1')['half'],None)
        for value in ['2:1 (1:0 2:0)','2:1 (1:0 0:0 1:1)','2:1 ДВ','2:1 (пен. 5:4)','Отменён','1:0 (1:0)','-1:0']:
            with self.subTest(value=value):self.assertIsNone(football_result(value))
        event={'competition':'League','sport_label':'Футбол','home':'A','away':'B','kickoff_at':(NOW+timedelta(hours=1)).isoformat()}
        self.assertEqual(len(football_sample(self.repo,event,NOW)),20)

    def test_fixed_ten_match_cohort_and_actual_result_does_not_enter_forecast(self):
        with self.repo.connection() as db:first=run(db,'historical-results',10)
        self.assertEqual(first['selected'],10);self.assertEqual(first['evaluated'],10)
        self.assertFalse(first['calibration_verified']);self.assertFalse(first['profitability_verified'])
        target=first['matches'][-1]
        with self.repo.connection() as db:
            row=json.loads(db.execute('SELECT payload FROM archive_latest_results WHERE result_key=?',(target['result_key'],)).fetchone()[0]);row['score']='0:9 (0:4 0:5)'
            db.execute('UPDATE archive_latest_results SET score=?,payload=? WHERE result_key=?',(row['score'],json.dumps(row),target['result_key']))
            second=run(db,'historical-results',10)
        self.assertEqual(first['matches'][-1]['probabilities'],second['matches'][-1]['probabilities'])
        self.assertNotEqual(first['matches'][-1]['actual_score'],second['matches'][-1]['actual_score'])
        self.assertTrue(all(not row['statistics_snapshot'] and not row['market_results'] for row in first['matches']))

    def test_missing_history_is_not_replaced_with_a_more_favorable_fixture(self):
        with self.repo.connection() as db:
            row=json.loads(db.execute("SELECT payload FROM archive_latest_results WHERE result_key='19'").fetchone()[0]);row['member1']='New team'
            db.execute("UPDATE archive_latest_results SET member1=?,payload=? WHERE result_key='19'",('New team',json.dumps(row)))
            report=run(db,'historical-results',10)
        self.assertEqual(report['selected'],10);self.assertEqual(report['evaluated'],9)
        self.assertIsNone(report['matches'][-1]['probabilities'])
        self.assertEqual(report['excluded_forecasts']['insufficient_prior_history'],1)

    def saved(self):
        kickoff=NOW-timedelta(days=3);generated=kickoff-timedelta(hours=2)
        with self.repo.connection() as db:result=json.loads(db.execute("SELECT payload FROM archive_latest_results WHERE result_key='19'").fetchone()[0])
        return {'generated_at':generated.isoformat(),'context':{
            'event':{'sport':'football','phase':'prematch','kickoff_at':kickoff.isoformat(),'home':result['member1'],'away':result['member2'],'competition':'League'},
            'snapshot':{'observed_at':generated.isoformat()},
            'team_statistics':{'observed_at':generated.isoformat(),'match_phase':'prematch'},
            'probability_assessment':{'independent_only':True,'model_version':'independent-football-v19',
                'outcome_probabilities':{'home':.5,'draw':.2,'away':.3},'markets':[]}}}

    def add_saved(self,value,identifier):
        with self.repo.connection() as db:db.execute('INSERT INTO event_analyses(id,request_key,generated_at,payload) VALUES (?,?,?,?)',
            (identifier,identifier,value['generated_at'],json.dumps(value)))

    def test_saved_audit_deduplicates_users_and_excludes_postmatch_statistics(self):
        value=self.saved();bad=deepcopy(value);bad['context']['team_statistics']['match_phase']='finished'
        self.add_saved(bad,'bad');self.add_saved(value,'one');self.add_saved(value,'two')
        with self.repo.connection() as db:report=run(db,'saved',10)
        self.assertEqual(report['evaluated'],1);self.assertEqual(report['excluded_forecasts']['duplicate_forecast'],1)
        self.assertEqual(report['excluded_forecasts']['statistics_not_prematch'],1)
        self.assertTrue(report['matches'][0]['statistics_snapshot'])
        self.assertEqual(report['metrics']['favorite_hits'],1)
        self.assertAlmostEqual(report['metrics']['brier_3way'],.38)

    def test_saved_mode_does_not_reconstruct_when_no_snapshots_exist(self):
        with self.repo.connection() as db:report=run(db,'saved',10)
        self.assertEqual(report['evaluated'],0);self.assertEqual(report['matches'],[])
        self.assertIsNone(report['metrics'])
        path=Path(self.folder.name)/'reports'/'test.json';save(report,path)
        self.assertEqual(json.loads(path.read_text())['evaluated'],0);self.assertTrue(path.with_suffix('.csv').is_file())

    def test_forecasts_from_an_old_model_are_not_silently_mixed_into_current_metrics(self):
        value=self.saved();value['context']['probability_assessment']['model_version']='old'
        self.add_saved(value,'old')
        with self.repo.connection() as db:report=run(db,'saved',10)
        self.assertEqual(report['evaluated'],0);self.assertEqual(report['excluded_forecasts']['other_model_version'],1)

    def test_summary_diversifies_market_families_instead_of_repeating_a_handicap(self):
        def row(key,name,p,period):return {'market_key':key,'market':name,'basis':'history','probability':p,'period':period}
        assessment={'markets':[row('half','Фора',.96,'1 тайм'),row('full','Фора',.92,'осн.время'),row('total','Тотал',.75,'1 тайм'),row('btts','Обе забьют',.7,'осн.время')]}
        self.assertEqual([r['market_key'] for r in report_markets(assessment)],['half','total','btts'])

    def test_market_explanations_cannot_target_a_fabricated_market(self):
        facts=trusted_facts(context_large());reply={'arguments':[{'fact_ids':['F1'],'explanation':EXPLANATION,'market_keys':['known']}]}
        with self.assertRaises(ValueError):verified_observations(reply,[],facts,market_keys={'other'})
        accepted=verified_observations(reply,[],facts,market_keys={'known'})
        self.assertEqual(accepted[0]['market_keys'],['known'])

    def test_actual_model_protocol_keeps_market_explanations_and_never_changes_probability(self):
        context=context_large();context['probability_assessment']['markets']=[
            {'market_key':'0','outcome_key':'0-1','market':'Тотал','period':'осн.время','parameter':'2.5',
             'outcome':'Меньше','basis':'history','probability':.75}]
        before=deepcopy(context['probability_assessment']);seen=[]
        def complete(settings,messages):
            payload=json.loads(messages[1]['content']);seen.append(payload)
            return json.dumps({'arguments':[{'fact_ids':['F1'],'explanation':EXPLANATION},
                {'fact_ids':['F1'],'market_keys':['0'],'explanation':EXPLANATION}]},ensure_ascii=False)
        model=FakeModel(limit=131072)
        settings=Settings(Path(self.folder.name),qwen_model='fixture',qwen_batch_tokens=128000)
        arguments,diagnostics=analyze(settings,context,complete,model.request)
        self.assertEqual(diagnostics['state'],'ready');self.assertEqual(diagnostics['model_calls'],1)
        self.assertIn(EXPLANATION,diagnostics['market_explanations']['0'][0])
        self.assertEqual(seen[0]['forecast']['markets'][0]['market_key'],'0')
        self.assertEqual(context['probability_assessment'],before)

    def test_half_time_settlement_uses_actual_half_not_half_of_the_final_score(self):
        prediction={'market_key':'half','outcome_key':'away','market':'Фора','period':'1 тайм',
            'parameter':'+1.5','outcome':'П2','basis':'history','probability':.9}
        market={'key':'half','name':'Фора','period':'1 тайм','outcomes_complete':True,'outcomes':[
            {'key':'home','name':'П1','parameter':'-1.5'},{'key':'away','name':'П2','parameter':'+1.5'}]}
        context={'event':{'home':'A','away':'B'},'markets':[market]}
        report=settled_markets({'recommended_markets':[prediction]}, {'goals':(4,0),'half':(1,0)},context)
        self.assertEqual(report[0]['state'],'win')
        report=settled_markets({'recommended_markets':[prediction]}, {'goals':(4,0),'half':None},context)
        self.assertEqual(report[0]['state'],'not_scored')

    def test_quality_endpoint_is_admin_only_and_has_bounded_selection(self):
        app=create_app(Settings(data_dir=Path(self.folder.name)))
        with TestClient(app) as client:
            self.assertIn(client.get('/api/admin/forecast-quality').status_code,(401,403))
            signed_in(client,self.repo)
            self.assertEqual(client.get('/api/admin/forecast-quality').status_code,403)
            signed_in(client,self.repo,'quality_admin','admin')
            self.assertEqual(client.get('/api/admin/forecast-quality?limit=101').status_code,422)
            self.assertEqual(client.get('/api/admin/forecast-quality?mode=historical-results').json()['evaluated'],10)
            self.assertEqual(client.get('/api/admin/forecast-quality').json()['evaluated'],0)


if __name__=='__main__':unittest.main()
