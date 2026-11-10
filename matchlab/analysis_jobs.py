"""Owner-scoped, durable event analyses; one model job at a time per database."""
from datetime import datetime, timedelta, timezone
import json
import logging
from threading import Event, Thread
from uuid import uuid4
import zlib

from fastapi import HTTPException

from . import accounts

SCHEMA = '''
CREATE TABLE IF NOT EXISTS ai_analysis_jobs (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,request_key TEXT NOT NULL,
 event_id TEXT NOT NULL,source TEXT NOT NULL,state TEXT NOT NULL,
 created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,deadline TEXT,
 usage_id TEXT NOT NULL,snapshot BLOB,analysis_id TEXT,cached INTEGER NOT NULL DEFAULT 0,
 error TEXT,lease TEXT);
CREATE INDEX IF NOT EXISTS analysis_jobs_owner ON ai_analysis_jobs(user_id,created_at);
CREATE INDEX IF NOT EXISTS analysis_jobs_queue ON ai_analysis_jobs(state,created_at);
'''
LOG=logging.getLogger('uvicorn.error')


def stamp():return datetime.now(timezone.utc).isoformat()


class AnalysisJobs:
    def __init__(self,repo,settings,execute):
        self.repo=repo;self.settings=settings;self.execute=execute
        self.stopping=Event();self.wake=Event();self.thread=None

    def start(self):
        self.stopping.clear()
        if self.thread and self.thread.is_alive():return
        self.thread=Thread(target=self.run,name='event-analysis-jobs',daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set();self.wake.set()
        if self.thread:self.thread.join(timeout=1)

    def enqueue(self,user,event_id,source,key,snapshot):
        raw=json.dumps(snapshot,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
        if len(raw)>8*1024*1024:raise HTTPException(422,'Данные матча слишком велики для разбора.')
        with self.repo.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            active=db.execute("SELECT * FROM ai_analysis_jobs WHERE user_id=? AND state IN ('queued','running')",(user['id'],)).fetchone()
            if active:
                if active['request_key']==key:return self.public(active)
                raise HTTPException(409,'Для вашего аккаунта уже готовится разбор. Дождитесь результата в истории.')
            if db.execute("SELECT count(*) FROM ai_analysis_jobs WHERE state IN ('queued','running')").fetchone()[0]>=8:
                raise HTTPException(429,'Очередь разборов заполнена. Попробуйте позже.')
            usage=accounts.start_usage(self.repo,user,'event',f'/api/{source}/events/{event_id}/explain',self.settings,db=db)
            job_id=uuid4().hex
            db.execute("INSERT INTO ai_analysis_jobs(id,user_id,request_key,event_id,source,state,created_at,usage_id,snapshot) "
                       "VALUES (?,?,?,?,?,'queued',?,?,?)",(job_id,user['id'],key,event_id,source,stamp(),usage,zlib.compress(raw)))
            row=db.execute('SELECT * FROM ai_analysis_jobs WHERE id=?',(job_id,)).fetchone()
        LOG.info('Analysis job queued: job=%s event=%s',job_id,event_id)
        self.wake.set()
        return self.public(row)

    @staticmethod
    def public(row):
        return {key:row[key] for key in ('id','event_id','source','state','created_at','started_at','finished_at','analysis_id','error')}

    def current(self,user_id):
        with self.repo.connection() as db:
            row=db.execute("SELECT * FROM ai_analysis_jobs WHERE user_id=? AND state IN ('queued','running') ORDER BY created_at DESC LIMIT 1",(user_id,)).fetchone()
        return self.public(row) if row else None

    def get(self,job_id,user_id):
        with self.repo.connection() as db:
            row=db.execute('SELECT * FROM ai_analysis_jobs WHERE id=? AND user_id=?',(job_id,user_id)).fetchone()
        if not row:raise HTTPException(404,'Задание не найдено.')
        result=self.public(row)
        if row['analysis_id']:
            payload=self.repo.event_analysis(analysis_id=row['analysis_id'],user_id=user_id)
            if payload:result['result']={**payload,'cached':bool(row['cached'])}
        return result

    def claim(self):
        now=stamp()
        with self.repo.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            expired=db.execute("SELECT * FROM ai_analysis_jobs WHERE (state='running' AND deadline<?) OR "
                               "(state='queued' AND created_at<?)",(now,(datetime.now(timezone.utc)-timedelta(minutes=30)).isoformat())).fetchall()
            for row in expired:
                self.finish(row,error='Ожидание разбора истекло. Предыдущая попытка не списана.',db=db)
            if db.execute("SELECT 1 FROM ai_analysis_jobs WHERE state='running' LIMIT 1").fetchone():return None
            row=db.execute("SELECT * FROM ai_analysis_jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row:return None
            user=db.execute('SELECT enabled FROM users WHERE id=?',(row['user_id'],)).fetchone()
            if not user or not user['enabled'] or not accounts.agreements_current(self.repo,row['user_id']):
                self.finish(row,error='Для разбора требуется действующий аккаунт и согласия.',db=db)
                return None
            deadline=(datetime.now(timezone.utc)+timedelta(seconds=self.settings.qwen_job_timeout+90)).isoformat()
            db.execute("UPDATE ai_analysis_jobs SET state='running',started_at=?,deadline=?,lease=? WHERE id=?",
                       (now,deadline,uuid4().hex,row['id']))
            return dict(db.execute('SELECT * FROM ai_analysis_jobs WHERE id=?',(row['id'],)).fetchone())

    def finish(self,job,result=None,error=None,db=None,token_usage=None):
        if db is None:
            with self.repo.connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                return self.finish(job,result,error,db=connection,token_usage=token_usage)
        row=db.execute('SELECT * FROM ai_analysis_jobs WHERE id=?',(job['id'],)).fetchone()
        if not row or row['state'] not in ('queued','running') or row['lease']!=job['lease']:return
        latency=int((datetime.now(timezone.utc)-datetime.fromisoformat(row['started_at'] or row['created_at'])).total_seconds()*1000)
        details=None;status='failed'
        if result:
            gen=result.get('context',{}).get('ai_generation') or {}
            details={key:gen.get(key) for key in ('state','reason','model_calls','parts_total','parts_completed','markets_total','markets_completed')}
            details.update(analysis_id=result['id'],cache_hit=bool(result.get('cached')))
            if result.get('cached'):details['model_calls']=0
            status='limited' if result.get('analysis_type') in ('limited_summary','statistical_report','partial_model') else 'success'
        if token_usage is not None:details={**(details or {}),'token_usage':token_usage}
        accounts.finish_usage(self.repo,row['usage_id'],status,latency,details,db=db)
        db.execute('UPDATE ai_analysis_jobs SET state=?,finished_at=?,analysis_id=?,cached=?,error=?,snapshot=NULL WHERE id=?',
                   ('failed' if error else 'completed',stamp(),result.get('id') if result else None,
                    int(bool(result and result.get('cached'))),error,row['id']))
        LOG.info('Analysis job finished: job=%s state=%s',row['id'],'failed' if error else 'completed')

    def run_one(self):
        job=self.claim()
        if not job:return False
        LOG.info('Analysis job started: job=%s event=%s',job['id'],job['event_id'])
        from .token_usage import capture_usage
        capture=None
        try:
            snapshot=json.loads(zlib.decompress(job['snapshot']))
            with capture_usage() as capture:result=self.execute(job,snapshot)
            expected=0 if result.get('cached') else (result.get('context',{}).get('ai_generation') or {}).get('model_calls',0)
            self.finish(job,result=result,token_usage=capture.summary(expected))
        except HTTPException as error:
            self.finish(job,error=error.detail if isinstance(error.detail,str) else 'Не удалось выполнить разбор.',token_usage=capture.summary() if capture else None)
        except Exception as error:
            LOG.warning('Analysis job failed: job=%s category=%s',job['id'],type(error).__name__)
            self.finish(job,error='Не удалось завершить разбор. Попытка не списана.',token_usage=capture.summary() if capture else None)
        return True

    def run(self):
        while not self.stopping.is_set():
            try:
                if self.run_one():continue
            except Exception as error:
                LOG.warning('Analysis queue unavailable: category=%s',type(error).__name__)
            self.wake.wait(1);self.wake.clear()
