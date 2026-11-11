"""Durable, once-per-event preparation for the system AI lane."""
from datetime import datetime,timedelta,timezone
import logging
from threading import Event,Thread

from .bookmakers import instant
from .verdict_sports import supported

SCHEMA='''
CREATE TABLE IF NOT EXISTS featured_analysis_events (
 event_id TEXT PRIMARY KEY,home TEXT,away TEXT,sport TEXT,kickoff_at TEXT,
 state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 statistics_job_id TEXT,analysis_job_id TEXT,analysis_id TEXT,last_error TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS featured_one_preparation ON featured_analysis_events(state) WHERE state='preparing';
'''
LOG=logging.getLogger('uvicorn.error')


def enabled(repo,db=None):
    if db is None:
        with repo.connection() as connection:return enabled(repo,connection)
    row=db.execute("SELECT value FROM site_settings WHERE key='featured_auto_enabled'").fetchone()
    return not row or row[0]=='1'


def status(repo):
    with repo.connection() as db:
        counts={r['state']:r['total'] for r in db.execute('SELECT state,count(*) total FROM featured_analysis_events GROUP BY state')}
        rows=[dict(r) for r in db.execute('''SELECT event_id,home,away,sport,kickoff_at,state,updated_at,last_error,analysis_id
            FROM featured_analysis_events ORDER BY updated_at DESC LIMIT 20''')]
        return {'enabled':enabled(repo,db),'system_concurrency':1,'human_concurrency':1,'counts':counts,'events':rows}


class FeaturedAutomation:
    def __init__(self,repo,settings,jobs,prepare,request_statistics):
        self.repo=repo;self.settings=settings;self.jobs=jobs;self.prepare=prepare;self.request_statistics=request_statistics
        self.stopping=Event();self.wake=Event();self.thread=None

    def start(self):
        self.stopping.clear()
        if self.thread and self.thread.is_alive():return
        self.thread=Thread(target=self.run,name='featured-analysis-preparation',daemon=True);self.thread.start()

    def stop(self):
        self.stopping.set();self.wake.set()
        if self.thread:self.thread.join(timeout=1)

    def update(self,event_id,state,error=None,**fields):
        allowed={'statistics_job_id','analysis_job_id','analysis_id'}
        if set(fields)-allowed:raise ValueError('Неизвестное поле задания.')
        fields.update(state=state,last_error=error,updated_at=datetime.now(timezone.utc).isoformat())
        with self.repo.connection() as db:
            db.execute('UPDATE featured_analysis_events SET '+','.join(k+'=?' for k in fields)+' WHERE event_id=?',(*fields.values(),event_id))

    def tick(self):
        if not enabled(self.repo) or not self.settings.qwen_configured or self.settings.event_analysis_mode!='neural-verdict':return False
        now=datetime.now(timezone.utc)
        from . import archive
        from .featured_events import collection
        with self.repo.connection() as db:
            if db.execute("SELECT 1 FROM ai_analysis_jobs WHERE lane='system' AND state IN ('queued','running')").fetchone():return False
            preparing=db.execute("SELECT * FROM featured_analysis_events WHERE state='preparing'").fetchone()
        if preparing:
            row=dict(preparing)
        else:
            candidates=collection(self.repo,limit=100)['events']
            row=None
            with self.repo.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT 1 FROM featured_analysis_events WHERE state='preparing'").fetchone():return False
                for event in candidates:
                    identifier=event['source_event_id']
                    if event.get('is_stale') or event.get('source_present') is False:continue
                    if db.execute('SELECT 1 FROM featured_analysis_events WHERE event_id=?',(identifier,)).fetchone():continue
                    usable=supported(event) and event.get('phase')=='prematch' and event.get('kickoff_at') and instant(event['kickoff_at'])>now
                    state='preparing' if usable else 'skipped'
                    db.execute('''INSERT INTO featured_analysis_events(event_id,home,away,sport,kickoff_at,state,created_at,updated_at,last_error)
                        VALUES (?,?,?,?,?,?,?,?,?)''',(identifier,event.get('home'),event.get('away'),event.get('sport'),event.get('kickoff_at'),state,
                        now.isoformat(),now.isoformat(),None if usable else 'Событие уже началось или формат не поддерживается.'))
                    if usable:
                        row=dict(db.execute('SELECT * FROM featured_analysis_events WHERE event_id=?',(identifier,)).fetchone());break
            if not row:return False
        identifier=row['event_id'];document=archive.document(self.repo,identifier)
        if not document or document.get('phase')!='prematch' or not document.get('kickoff_at') or instant(document['kickoff_at'])<=now:
            self.update(identifier,'skipped','Предматчевый разбор больше недоступен.');return True
        age=(now-instant(row['created_at'])).total_seconds()
        stats_job=row.get('statistics_job_id')
        if not stats_job:
            try:
                queued=self.request_statistics(identifier)
                stats_job=queued['job_id'];self.update(identifier,'preparing',statistics_job_id=stats_job)
                return True
            except Exception as error:
                if age<180:
                    self.update(identifier,'preparing','Ожидаем доступности статистики.');return False
        job=self.repo.statistics_request(identifier,stats_job) if stats_job else None
        if job and job['state'] not in ('finished','timeout') and age<240:return False
        try:
            snapshot,key=self.prepare(identifier,stats_job if job and job['state']=='finished' else None)
            queued=self.jobs.enqueue_system(identifier,key,snapshot)
            return queued is not None
        except Exception as error:
            # Retrying preparation never reruns a model decision. Once a model
            # job exists its unique event key prevents a second system forecast.
            if age>=300:self.update(identifier,'skipped','Не удалось подготовить подтверждённые предматчевые данные.')
            else:self.update(identifier,'preparing','Ожидаем обновления данных матча.')
            LOG.warning('Featured preparation delayed: event=%s category=%s',identifier,type(error).__name__)
            return False

    def run(self):
        while not self.stopping.is_set():
            try:self.tick()
            except Exception as error:LOG.warning('Featured automation unavailable: category=%s',type(error).__name__)
            self.wake.wait(10);self.wake.clear()
