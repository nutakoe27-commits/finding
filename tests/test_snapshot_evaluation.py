"""Prematch recorded statistics can be replayed; postmatch updates cannot."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from matchlab.statistics import analysis_statistics
from matchlab.storage import Repository
from matchlab.winline_backtest import run,evaluate_path
from test_forecast_quality import seed,NOW


class SnapshotEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.repo=Repository(Path(self.folder.name)/'matchlab.sqlite3');self.repo.initialize();seed(self.repo)
        self.kickoff=NOW-timedelta(days=3)
        with self.repo.connection() as db:
            payload=json.loads(db.execute("SELECT payload FROM archive_latest_results WHERE result_key='19'").fetchone()[0])
            payload['idRadar']=12345
            db.execute("UPDATE archive_latest_results SET payload=? WHERE result_key='19'",(json.dumps(payload),))

    def snapshot(self):
        at=(self.kickoff-timedelta(hours=2)).isoformat()
        return {'state':'ok','source_event_id':'100','statistics_id':'12345','statistics_url':'https://winline.ru/stats/match/12345',
                'observed_at':at,'attempted_at':at,'match_phase':'prematch','header':'A — B',
                'sections':[{'title':'Статистика сезона','filters':['За матч','Общее'],
                             'text':'1.2\nЗабитые мячи\n1.5\n1.0\nГолов пропущено\n1.4'}]}

    def standalone(self,value):
        with self.repo.connection() as db:
            db.execute('INSERT OR REPLACE INTO event_statistics VALUES (?,?,?,?,?)',
                ('100','12345',value['state'],value['attempted_at'],json.dumps(value)))

    def analysis(self,value,identifier='old'):
        context={'event':{'source_event_id':'100','sport':'football','home':'A','away':'B',
                         'competition':'League','kickoff_at':self.kickoff.isoformat(),'phase':'prematch'},
                 'team_statistics':analysis_statistics(value),
                 'probability_assessment':{'model_version':'old','outcome_probabilities':{'home':1,'draw':0,'away':0}}}
        saved={'context':context,'generated_at':value['attempted_at']}
        with self.repo.connection() as db:
            db.execute('INSERT INTO event_analyses(id,request_key,generated_at,payload) VALUES (?,?,?,?)',
                (identifier,identifier,saved['generated_at'],json.dumps(saved)))

    def report(self):
        return evaluate_path(self.repo.path,'recorded-statistics',100)

    def test_standalone_snapshot_replays_even_without_an_old_ai_forecast(self):
        self.standalone(self.snapshot());report=self.report()
        self.assertEqual(report['selected'],1);self.assertEqual(report['evaluated'],1)
        case=report['matches'][0]
        self.assertTrue(case['statistics_snapshot']);self.assertEqual(case['replayed_input'],'event_statistics')
        self.assertAlmostEqual(sum(case['probabilities'].values()),1)
        self.assertEqual(case['history_diagnostics']['archive_matches'],0)
        self.assertEqual(case['history_diagnostics']['season_rates_available'],['home','away'])
        self.assertEqual(report['snapshot_inventory']['standalone_snapshots'],1)
        self.assertFalse(report['calibration_verified'])

    def test_later_result_does_not_change_replayed_probabilities(self):
        self.standalone(self.snapshot());first=self.report()
        with self.repo.connection() as db:
            data=json.loads(db.execute("SELECT payload FROM archive_latest_results WHERE result_key='19'").fetchone()[0])
            data['score']='0:9 (0:4 0:5)';db.execute("UPDATE archive_latest_results SET payload=? WHERE result_key='19'",(json.dumps(data),))
        second=self.report()
        self.assertEqual(first['matches'][0]['probabilities'],second['matches'][0]['probabilities'])
        self.assertNotEqual(first['matches'][0]['actual_score'],second['matches'][0]['actual_score'])

    def test_after_kickoff_snapshot_is_excluded_even_with_prematch_label(self):
        value=self.snapshot();value['observed_at']=value['attempted_at']=(self.kickoff+timedelta(minutes=1)).isoformat()
        self.standalone(value);report=self.report()
        self.assertEqual(report['evaluated'],0)
        self.assertEqual(report['excluded_forecasts']['statistics_not_before_kickoff'],1)

    def test_live_and_finished_snapshots_are_not_relabelled_as_prematch(self):
        for phase in ('live','finished'):
            with self.subTest(phase=phase):
                value=self.snapshot();value['match_phase']=phase;self.standalone(value)
                self.assertEqual(self.report()['evaluated'],0)

    def test_late_section_or_feed_observation_invalidates_the_snapshot(self):
        for field in ('sections','structured_feed'):
            value=self.snapshot();at=(self.kickoff+timedelta(minutes=1)).isoformat()
            if field=='sections':value['sections'][0]['observed_at']=at
            else:value['structured_feed']={'observed_at':at}
            self.standalone(value)
            with self.subTest(field=field):self.assertEqual(self.report()['evaluated'],0)

    def test_earliest_input_survives_later_live_overwrite_and_old_model_is_recomputed(self):
        original=self.snapshot();self.analysis(original)
        later=deepcopy(original);later['match_phase']='finished';later['observed_at']=later['attempted_at']=(self.kickoff+timedelta(hours=3)).isoformat()
        self.standalone(later);report=self.report()
        self.assertEqual(report['evaluated'],1)
        case=report['matches'][0]
        self.assertEqual(case['replayed_input'],'saved_analysis')
        self.assertNotEqual(case['probabilities'],{'home':1,'draw':0,'away':0})
        self.assertEqual(case['model_version'],'independent-football-v19')

    def test_duplicate_users_and_standalone_copies_do_not_inflate_match_count(self):
        original=self.snapshot();self.analysis(original,'one');self.analysis(original,'two');self.standalone(original)
        report=self.report()
        self.assertEqual(report['evaluated'],1)
        self.assertEqual(report['snapshot_inventory']['duplicate_fixture_snapshots'],2)

    def test_provider_id_mismatch_is_not_matched_by_nearby_time(self):
        value=self.snapshot();value['statistics_id']='99999';self.analysis(value)
        self.assertEqual(self.report()['evaluated'],0)

    def test_model_cannot_use_a_result_received_after_the_simulated_request(self):
        self.standalone(self.snapshot());first=self.report()
        # All seeded history was imported NOW, after this old statistics snapshot.
        with self.repo.connection() as db:
            for row in db.execute("SELECT result_key,payload FROM archive_latest_results WHERE result_key!='19'").fetchall():
                data=json.loads(row['payload']);data['score']='9:0 (4:0 5:0)'
                db.execute('UPDATE archive_latest_results SET payload=? WHERE result_key=?',(json.dumps(data),row['result_key']))
        self.assertEqual(first['matches'][0]['probabilities'],self.report()['matches'][0]['probabilities'])


if __name__=='__main__':unittest.main()
