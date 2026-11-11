"""Two independent bounded lanes and durable, once-only main-event automation."""
from dataclasses import replace
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
from threading import Event,Thread,Lock
import unittest
from unittest.mock import patch
from uuid import uuid4
import zlib

from fastapi.testclient import TestClient
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from matchlab.analysis_jobs import AnalysisJobs,SYSTEM_OWNER
from matchlab.featured_automation import FeaturedAutomation,enabled
from matchlab import archive,accounts
from account_helpers import signed_in
from browser_archive_smoke import feed
from test_archive import header
from test_product import event


class FeaturedAutomationTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.settings=Settings(Path(folder.name),'http://localhost:9/v1','fixture',event_analysis_mode='neural-verdict')
        self.app=create_app(self.settings);self.client=TestClient(self.app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.app.state.featured_automation.stop();self.app.state.analysis_jobs.stop()
        self.repo=Repository(self.settings.database);self.user=signed_in(self.client,self.repo)

    def setting(self,value):
        with self.repo.connection() as db:db.execute('INSERT OR REPLACE INTO site_settings VALUES (?,?,?)',('featured_auto_enabled','1' if value else '0',datetime.now(timezone.utc).isoformat()))

    def test_human_and_system_model_executions_overlap_but_each_lane_has_one_slot(self):
        self.repo.import_event_markets(event())
        response=self.client.post('/api/bookmakers/events/123/analysis-jobs',json={});self.assertEqual(response.status_code,202,response.text)
        jobs=self.app.state.analysis_jobs
        with self.repo.connection() as db:snapshot=json.loads(zlib.decompress(db.execute('SELECT snapshot FROM ai_analysis_jobs WHERE id=?',(response.json()['id'],)).fetchone()[0]))
        system=jobs.enqueue_system('123','system-key',snapshot);self.assertIsNotNone(system)
        entered={'human':Event(),'system':Event()};release=Event();calls=[];guard=Lock()
        def model(opts,context):
            lane=context['analysis_origin']
            with guard:calls.append(lane)
            entered[lane].set();release.wait(5)
            context['ai_generation']={'state':'ready','model_calls':1};return 'Ответ тестового движка'
        with patch('matchlab.api.qwen.explain_event',side_effect=model):
            workers=[Thread(target=jobs.run_one,args=(lane,)) for lane in ('human','system')]
            for worker in workers:worker.start()
            try:
                self.assertTrue(entered['human'].wait(3));self.assertTrue(entered['system'].wait(3),'system execution blocked the human lane')
                self.assertIsNone(jobs.claim('human'));self.assertIsNone(jobs.claim('system'))
            finally:
                release.set()
                for worker in workers:worker.join(5)
        self.assertEqual(sorted(calls),['human','system'])
        with self.repo.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM ai_usage WHERE user_id=?",(self.user,)).fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM ai_usage WHERE user_id=?",(SYSTEM_OWNER,)).fetchone()[0],1)
        from matchlab.token_analytics import run
        usage=run(self.repo)
        self.assertIn('Системный анализ',{row['username'] for row in usage['requests']})

    def test_existing_job_database_gains_human_default_and_unique_system_event_index(self):
        import sqlite3
        path=self.settings.data_dir/'old.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE ai_analysis_jobs(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,request_key TEXT NOT NULL,
                event_id TEXT NOT NULL,source TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,
                started_at TEXT,finished_at TEXT,deadline TEXT,usage_id TEXT NOT NULL,snapshot BLOB,analysis_id TEXT,
                cached INTEGER NOT NULL DEFAULT 0,error TEXT,lease TEXT)''')
            db.execute("INSERT INTO ai_analysis_jobs(id,user_id,request_key,event_id,source,state,created_at,usage_id) VALUES ('old','user','k','1','archive','completed',?,'usage')",(accounts.now(),))
        repo=Repository(path);repo.initialize()
        with repo.connection() as db:
            self.assertEqual(db.execute("SELECT lane FROM ai_analysis_jobs WHERE id='old'").fetchone()[0],'human')
            self.assertIn('system_event_once',{r['name'] for r in db.execute('PRAGMA index_list(ai_analysis_jobs)')})

    def test_main_event_is_prepared_once_and_is_not_rerun_after_completion_or_restart(self):
        value=header('101');value['featured_rank']=0;other=header('102');feed(self.repo,[value,other])
        jobs=AnalysisJobs(self.repo,self.settings,lambda job,snapshot:{'id':uuid4().hex,'analysis_type':'neural_verdict','context':{'ai_generation':{'state':'ready','model_calls':1}}})
        def prepare(identifier,stats_id):return {'request':{},'inputs':{}},'key-'+identifier
        auto=FeaturedAutomation(self.repo,self.settings,jobs,prepare,lambda identifier:self.repo.request_statistics(archive.document(self.repo,identifier)))
        self.assertTrue(auto.tick())
        stats=self.repo.claim_statistics_request();self.assertEqual(stats['event']['source_event_id'],'101')
        self.repo.finish_statistics_request(stats['job_id'],stats['lease_id'],'ok')
        self.assertTrue(auto.tick());self.assertTrue(jobs.run_one('system'))
        self.assertFalse(auto.tick())
        restarted=FeaturedAutomation(self.repo,self.settings,jobs,prepare,lambda _:self.fail('statistics requested again'))
        self.assertFalse(restarted.tick())
        with self.repo.connection() as db:
            rows=db.execute("SELECT event_id,state FROM featured_analysis_events").fetchall()
            self.assertEqual([(r['event_id'],r['state']) for r in rows],[('101','completed')])
            self.assertEqual(db.execute("SELECT count(*) FROM ai_analysis_jobs WHERE lane='system'").fetchone()[0],1)

    def test_disabled_system_lane_does_not_block_a_human_or_consume_their_quota(self):
        self.setting(False);self.assertFalse(enabled(self.repo))
        jobs=self.app.state.analysis_jobs;jobs.enqueue_system('123','system',{'request':{},'inputs':{}})
        self.assertIsNone(jobs.claim('system'))
        self.repo.import_event_markets(event())
        response=self.client.post('/api/bookmakers/events/123/analysis-jobs',json={})
        self.assertEqual(response.status_code,202,response.text);self.assertIsNotNone(jobs.claim('human'))

    def test_paused_preparation_and_unsupported_live_events_never_call_the_model(self):
        value=header('101',live=True);value['featured_rank']=0;feed(self.repo,[value])
        auto=FeaturedAutomation(self.repo,self.settings,self.app.state.analysis_jobs,lambda *_:self.fail('model prepared'),lambda *_:self.fail('statistics requested'))
        auto.tick()
        with self.repo.connection() as db:self.assertEqual(db.execute('SELECT state FROM featured_analysis_events').fetchone()[0],'skipped')
        self.setting(False);self.assertFalse(auto.tick())

    def test_automation_settings_require_admin_csrf_and_recent_password_confirmation(self):
        self.assertEqual(self.client.get('/api/admin/featured-automation').status_code,403)
        with self.repo.connection() as db:db.execute("UPDATE users SET role='admin' WHERE id=?",(self.user,))
        self.assertEqual(self.client.get('/api/admin/featured-automation').status_code,200)
        self.assertEqual(self.client.put('/api/admin/featured-automation',json={'enabled':False}).status_code,403)
        with self.repo.connection() as db:db.execute('UPDATE user_sessions SET elevated_at=? WHERE user_id=?',(accounts.now(),self.user))
        response=self.client.put('/api/admin/featured-automation',json={'enabled':False})
        self.assertEqual(response.status_code,200,response.text);self.assertFalse(response.json()['enabled'])
        self.assertEqual(response.json()['human_concurrency'],1)
