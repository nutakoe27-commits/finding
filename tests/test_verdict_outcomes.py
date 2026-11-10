"""Real saved forecasts, honest skip labels, read-only settlement and admin privacy."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.neural_verdict import encoded
from matchlab.storage import Repository
from matchlab.verdict_outcomes import run,report_files
from account_helpers import signed_in
from outcome_helpers import forecast,archive_result,seed_outcomes


class VerdictOutcomesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=Repository(self.root/'matchlab.sqlite3');self.repo.initialize()
        self.now=datetime.now(timezone.utc)

    def report(self,**kw):return run(self.repo,now=self.now,**kw)

    def update(self,payload):
        with self.repo.connection() as db:db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(payload),payload['id']))

    def test_preferences_bets_and_skip_outcomes_have_separate_denominators(self):
        seed_outcomes(self.repo,self.now);r=self.report();m=r['metrics']
        self.assertEqual((m['fixtures'],m['settled'],m['pending']),(6,5,1))
        self.assertEqual((m['preferences'],m['hits'],m['accuracy']),(4,2,.5))
        self.assertEqual((m['bets'],m['bet_wins'],m['bet_losses']),(2,1,1))
        self.assertEqual((m['skips'],m['skip_avoided_loss'],m['skip_missed_win'],m['skip_unassessed']),(3,1,1,1))
        self.assertEqual((m['priced_bets'],m['paper_profit'],m['paper_bank']),(2,0,100))
        self.assertEqual(r['excluded'],{'duplicate_forecast':1,'system_or_model_skip':1,'not_prematch':1})
        self.assertEqual(r['timezone'],'Europe/Moscow');self.assertEqual(len(r['daily']),30)
        self.assertNotIn('PRIVATE_',json.dumps(r))

    def test_zero_bets_and_no_preference_do_not_become_a_perfect_strategy(self):
        forecast(self.repo,decision='skip',preference=None)
        m=self.report()['metrics']
        self.assertEqual(m['preferences'],0);self.assertIsNone(m['accuracy'])
        self.assertIsNone(m['bet_accuracy']);self.assertIsNone(m['paper_roi'])
        self.assertEqual(m['paper_bank'],100);self.assertEqual(m['skip_unassessed'],1)

    def test_archive_arrival_and_correction_resettle_without_calling_a_model(self):
        saved=forecast(self.repo,score=None)
        with patch('matchlab.qwen._completion',side_effect=AssertionError('called Qwen')),patch('matchlab.chances.fit_goals',side_effect=AssertionError('prediction model')):
            self.assertEqual(self.report()['metrics']['pending'],1)
            archive_result(self.repo,saved['event'],'2:0','one')
            self.assertEqual(self.report()['metrics']['bet_wins'],1)
            archive_result(self.repo,saved['event'],'0:2','one')
            r=self.report();self.assertEqual(r['metrics']['bet_losses'],1);self.assertEqual(r['metrics']['paper_profit'],-.25)

    def test_earliest_forecast_is_kept_across_users_and_tolerated_clock_updates(self):
        first=forecast(self.repo,preference='away')
        later=deepcopy(first['event']);later['kickoff_at']=(datetime.fromisoformat(later['kickoff_at'])+timedelta(seconds=29)).isoformat()
        forecast(self.repo,'later',event=later,at=datetime.fromisoformat(first['generated_at'])+timedelta(minutes=1),preference='home',score=None)
        r=self.report();self.assertEqual(r['metrics']['preferences'],1);self.assertEqual(r['metrics']['hits'],0)
        self.assertEqual(r['excluded']['duplicate_forecast'],1)

    def test_version_filter_has_its_own_first_forecast_but_combined_view_deduplicates(self):
        first=forecast(self.repo,preference='away',version='neural-verdict-v5')
        forecast(self.repo,'later',event=first['event'],at=datetime.fromisoformat(first['generated_at'])+timedelta(seconds=10),version='neural-verdict-v6.1',score=None)
        self.assertEqual(self.report()['metrics']['hits'],0)
        self.assertEqual(self.report(version='neural-verdict-v6.1')['metrics']['hits'],1)

    def test_draw_loses_a_team_winner_bet_and_justifies_only_a_labeled_skip_in_hindsight(self):
        forecast(self.repo,'bet',score='1:1')
        forecast(self.repo,'skip',decision='skip',preference='away',score='0:0')
        m=self.report()['metrics'];self.assertEqual(m['hits'],0);self.assertEqual(m['bet_losses'],1);self.assertEqual(m['skip_avoided_loss'],1)

    def test_late_input_live_stats_and_future_prices_are_excluded(self):
        for kind in ('future_input','live_stats','future_price','guard'):
            p=forecast(self.repo,kind)
            if kind=='future_input':p['context']['neural_input']['as_of']=(datetime.fromisoformat(p['generated_at'])+timedelta(seconds=1)).isoformat()
            elif kind=='live_stats':p['context']['team_statistics']['match_phase']='live'
            elif kind=='future_price':p['context']['neural_input']['quote']['observed_at']=p['generated_at']
            else:p['context']['neural_verdict'].update(engine='timing_guard',decision='skip')
            self.update(p)
        r=self.report();self.assertEqual(r['metrics']['fixtures'],0);self.assertEqual(sum(r['excluded'].values()),4)

    def test_stale_or_mismatched_price_does_not_create_a_financial_result(self):
        p=forecast(self.repo);p['context']['neural_input']['quote']['observed_at']=(datetime.fromisoformat(p['generated_at'])-timedelta(minutes=4)).isoformat();self.update(p)
        m=self.report()['metrics'];self.assertEqual(m['bet_wins'],1);self.assertEqual(m['priced_bets'],0);self.assertIsNone(m['paper_roi'])
        p['context']['neural_input']['quote']['home']='Other team';self.update(p)
        self.assertEqual(self.report()['metrics']['fixtures'],0)

    def test_ambiguous_extra_time_and_missing_scores_are_not_graded_as_losses(self):
        for i,score in enumerate(('2:1 ДВ','2:1 (пен. 5:4)','Отменён','2:1 (1:0 0:0 1:1)')):forecast(self.repo,str(i),score=score)
        r=self.report();self.assertEqual(r['metrics']['settled'],0);self.assertIsNone(r['metrics']['accuracy'])
        self.assertEqual(r['metrics']['pending'],4)

    def test_two_possible_results_and_response_after_actual_kickoff_are_unresolved(self):
        p=forecast(self.repo);e=deepcopy(p['event']);e['kickoff_at']=(datetime.fromisoformat(e['kickoff_at'])+timedelta(seconds=60)).isoformat()
        archive_result(self.repo,e,'0:1','second')
        self.assertEqual(self.report()['metrics']['unresolved'],1)
        p=forecast(self.repo,'near',kickoff=self.now-timedelta(days=1),at=self.now-timedelta(days=1,seconds=30))
        e=deepcopy(p['event']);e['kickoff_at']=(datetime.fromisoformat(e['kickoff_at'])-timedelta(seconds=60)).isoformat()
        with self.repo.connection() as db:db.execute("DELETE FROM archive_latest_results WHERE result_key='near'")
        archive_result(self.repo,e,'2:0','near')
        r=self.report();self.assertEqual(r['metrics']['unresolved'],2);self.assertEqual(r['metrics']['preferences'],0)

    def test_filters_pagination_and_moscow_days_do_not_change_the_topline_cohort(self):
        seed_outcomes(self.repo,self.now);all_rows=self.report();filtered=self.report(state='skip',limit=2,offset=1)
        self.assertEqual(filtered['metrics'],all_rows['metrics']);self.assertEqual(len(filtered['matches']),2)
        self.assertEqual(filtered['total'],4)
        for c in all_rows['matches']:
            day=datetime.fromisoformat(c['event']['kickoff_at']).astimezone(__import__('zoneinfo').ZoneInfo('Europe/Moscow')).date().isoformat()
            self.assertTrue(any(d['day']==day and d['fixtures'] for d in all_rows['daily']))

    def test_retrospective_file_is_separate_and_uses_archive_truth_not_reported_winnings(self):
        saved=forecast(self.repo);directory=self.root/'evaluations';directory.mkdir()
        packet=saved['context']['neural_input'];path=directory/'neural-verdict-debug.json'
        trial={'format':'matchlab.neural-verdict-test.v1','created_at':self.now.isoformat(),'cases':[
            {'input':packet,'input_sha256':hashlib.sha256(encoded(packet).encode()).hexdigest(),
             'verdict':saved['context']['neural_verdict'],'generation':{'state':'ready','elapsed_ms':1000},
             'actual_score':'0:9','paper_profit':9999}]}
        path.write_text(json.dumps(trial))
        r=self.report(source='trials',report_dir=directory)
        self.assertTrue(r['diagnostic_only']);self.assertEqual(r['metrics']['bet_wins'],1);self.assertEqual(r['matches'][0]['actual_score'],'2:0')
        self.assertEqual(r['metrics']['paper_profit'],.25)
        self.assertNotIn('PRIVATE_',json.dumps(r))
        trial['cases'][0]['input_sha256']='tampered';path.write_text(json.dumps(trial))
        self.assertEqual(self.report(source='trials',report_dir=directory)['metrics']['fixtures'],0)

    def test_trial_paths_symlinks_and_oversized_files_are_not_read(self):
        directory=self.root/'evaluations';directory.mkdir()
        (directory/'neural-verdict-symlink.json').symlink_to(self.root/'secret')
        with (directory/'neural-verdict-large.json').open('wb') as f:f.truncate(8*1024*1024+1)
        self.assertEqual(report_files(directory),[])
        for filename in ('../secret','/etc/passwd','neural-verdict-symlink.json'):
            with self.subTest(filename=filename),self.assertRaises(ValueError):self.report(source='trials',report_id=filename,report_dir=directory)

    def test_endpoint_requires_admin_and_never_returns_private_prompt_fields(self):
        seed_outcomes(self.repo,self.now)
        with TestClient(create_app(Settings(self.root))) as client:
            self.assertEqual(client.get('/api/admin/verdict-outcomes').status_code,401)
            signed_in(client,self.repo)
            self.assertEqual(client.get('/api/admin/verdict-outcomes').status_code,403)
            signed_in(client,self.repo,username='admin_fixture',role='admin')
            response=client.get('/api/admin/verdict-outcomes');self.assertEqual(response.status_code,200,response.text)
            self.assertNotIn('PRIVATE_',response.text);self.assertNotIn('private-owner',response.text)
            self.assertEqual(client.get('/api/admin/verdict-outcomes?days=500').status_code,422)
            self.assertEqual(client.get('/api/admin/verdict-outcomes?offset=-1').status_code,422)
            self.assertEqual(client.get('/api/admin/verdict-outcomes?source=trials&report_id=../secret').status_code,422)


if __name__=='__main__':unittest.main()
