"""Persistent background analyses, ownership, quotas and snapshot isolation."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from threading import Event,Thread
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from account_helpers import signed_in
from test_product import event
from matchlab import accounts
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from matchlab.analysis_jobs import AnalysisJobs


class AnalysisJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.settings=Settings(Path(self.tmp.name),'http://localhost:9/v1','fixture',ai_daily_limit=1)
        self.app=create_app(self.settings);self.client=TestClient(self.app);self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.jobs=self.app.state.analysis_jobs;self.jobs.stop()
        self.repo=Repository(self.settings.database)
        self.user=signed_in(self.client,self.repo)
        self.repo.import_event_markets(event())
        self.url='/api/bookmakers/events/123/analysis-jobs'

    def submit(self,**body):
        response=self.client.post(self.url,json=body)
        self.assertEqual(response.status_code,202,response.text)
        return response.json()

    def status(self,job):return self.client.get('/api/analysis-jobs/'+job['id']).json()

    def test_submit_is_fast_and_duplicate_does_not_reserve_another_credit(self):
        with patch('matchlab.api.qwen.explain_event',side_effect=AssertionError('Submission must not call model')):
            first=self.submit();second=self.submit()
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(first['state'],'queued')
        self.assertNotIn('snapshot',first)
        with self.repo.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ai_usage').fetchone()[0],1)
        self.assertEqual(self.client.get('/api/analysis-jobs/current').json()['job']['id'],first['id'])

    def test_background_timeout_exceeds_http_budget_and_result_is_in_private_history(self):
        job=self.submit();entered=Event();release=Event();seen=[]
        def generate(settings,context):
            seen.append(settings);entered.set();release.wait(5)
            context['ai_generation']={'state':'ready','model_calls':1,'parts_total':1,'parts_completed':1}
            return 'Проверенный разбор'
        with patch('matchlab.api.qwen.explain_event',side_effect=generate):
            worker=Thread(target=self.jobs.run_one);worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.status(job)['state'],'running')
                self.assertEqual(self.client.get('/api/health').status_code,503)  # No EPL fixture, API remains responsive.
            finally:release.set();worker.join(5)
        result=self.status(job)
        self.assertEqual(result['state'],'completed',result)
        self.assertEqual(result['result']['text'],'Проверенный разбор')
        self.assertGreater(seen[0].qwen_timeout,140)
        self.assertLessEqual(seen[0].qwen_timeout,self.settings.qwen_job_timeout)
        history=self.client.get('/api/analyses').json()['analyses']
        self.assertEqual(history[0]['id'],result['analysis_id'])
        with self.repo.connection() as db:
            usage=db.execute('SELECT status,details FROM ai_usage').fetchone()
            self.assertEqual(usage['status'],'success');self.assertIn('"model_calls": 1',usage['details'])
            self.assertIsNone(db.execute('SELECT snapshot FROM ai_analysis_jobs').fetchone()[0])

    def test_saved_snapshot_is_used_even_after_line_and_teams_change(self):
        job=self.submit(notes='Личная заметка')
        self.repo.import_event_markets(event(home='Другой участник'))
        with patch('matchlab.api.qwen.explain_event',return_value='Разбор') as model:
            self.jobs.run_one()
        self.assertEqual(model.call_args.args[1]['event']['home'],'Спартак')
        self.assertEqual(model.call_args.args[1]['unverified_user_notes'],'Личная заметка')
        self.assertEqual(self.status(job)['result']['event']['home'],'Спартак')

    def test_configured_search_is_not_contacted_by_background_analysis(self):
        object.__setattr__(self.settings,'wigolo_mcp_url','http://127.0.0.1:13333/mcp')
        self.submit()
        with patch('matchlab.web_research.research',side_effect=AssertionError('Search forbidden')) as search, \
             patch('matchlab.api.qwen.explain_event',return_value='Разбор') as model:
            self.jobs.run_one()
        search.assert_not_called()
        self.assertEqual(model.call_args.args[1]['web_research'],{'state':'disabled','sources':[]})

    def test_private_jobs_require_login_csrf_current_agreements_and_owner(self):
        job=self.submit()
        other=TestClient(self.app)
        try:
            self.assertEqual(other.get('/api/analysis-jobs/'+job['id']).status_code,401)
            signed_in(other,self.repo,'other_user')
            self.assertEqual(other.get('/api/analysis-jobs/'+job['id']).status_code,404)
            self.assertIsNone(other.get('/api/analysis-jobs/current').json()['job'])
            del other.headers['X-CSRF-Token']
            self.assertEqual(other.post(self.url,json={}).status_code,403)
        finally:other.close()
        with self.repo.connection() as db:db.execute('DELETE FROM registration_agreements WHERE user_id=?',(self.user,))
        self.assertEqual(self.client.post(self.url,json={}).status_code,403)

    def test_restart_preserves_queued_snapshot_and_database_serializes_workers(self):
        job=self.submit()
        second=AnalysisJobs(self.repo,self.settings,self.jobs.execute)
        claimed=self.jobs.claim()
        self.assertEqual(claimed['id'],job['id'])
        self.assertIsNone(second.claim())
        self.assertEqual(second.current(self.user)['id'],job['id'])
        second.finish(claimed,error='Проверка прерывания')
        self.assertEqual(self.status(job)['state'],'failed')

    def test_failure_and_expired_lease_refund_bonus_exactly_once(self):
        with self.repo.connection() as db:
            db.execute("INSERT INTO ai_usage(id,user_id,at,kind,target,status,used_bonus) VALUES ('prior',?,?,'event','old','success',0)",(self.user,accounts.now()))
            db.execute('UPDATE users SET bonus_analyses=1 WHERE id=?',(self.user,))
        job=self.submit();claimed=self.jobs.claim()
        with self.repo.connection() as db:
            self.assertEqual(db.execute('SELECT bonus_analyses FROM users WHERE id=?',(self.user,)).fetchone()[0],0)
            db.execute("UPDATE ai_analysis_jobs SET deadline='2000-01-01T00:00:00+00:00' WHERE id=?",(job['id'],))
        self.assertIsNone(self.jobs.claim())
        self.jobs.finish(claimed,error='Late failure')
        self.assertEqual(self.status(job)['state'],'failed')
        with self.repo.connection() as db:
            self.assertEqual(db.execute('SELECT bonus_analyses FROM users WHERE id=?',(self.user,)).fetchone()[0],1)

    def test_long_active_job_is_not_reclaimed_by_ten_minute_usage_cleanup(self):
        self.submit();self.jobs.claim()
        with self.repo.connection() as db:
            db.execute('UPDATE ai_usage SET at=?',((datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat(),))
        with self.assertRaises(HTTPException) as caught:
            accounts.start_usage(self.repo,{'id':self.user},'event','another',self.settings)
        self.assertEqual(caught.exception.status_code,429)
        with self.repo.connection() as db:self.assertEqual(db.execute('SELECT status FROM ai_usage').fetchone()[0],'pending')

    def test_model_failure_is_stored_as_safe_error_without_leaking_provider_data(self):
        job=self.submit()
        with patch('matchlab.api.qwen.explain_event',side_effect=RuntimeError('private-key secret raw context')):
            self.jobs.run_one()
        status=self.status(job)
        self.assertEqual(status['state'],'failed')
        self.assertNotIn('secret',str(status));self.assertNotIn('private-key',str(status))
        with self.repo.connection() as db:self.assertEqual(db.execute('SELECT status FROM ai_usage').fetchone()[0],'failed')

    def test_unfinished_statistics_or_stale_snapshot_are_rejected_before_usage(self):
        self.assertEqual(self.client.post(self.url,json={'observed_at':'2000-01-01T00:00:00Z'}).status_code,409)
        self.assertEqual(self.client.post(self.url,json={'statistics_job_id':'a'*32}).status_code,409)
        with self.repo.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM ai_usage').fetchone()[0],0)

    def test_new_snapshot_cannot_create_second_active_job_for_same_user(self):
        self.submit()
        self.repo.import_event_markets(event(home='Новый участник'))
        self.assertEqual(self.client.post(self.url,json={}).status_code,409)
        with self.repo.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM ai_usage').fetchone()[0],1)


if __name__=='__main__':unittest.main()
