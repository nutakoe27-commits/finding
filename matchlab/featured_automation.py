"""Durable, once-per-event preparation for the system AI lane."""
from datetime import datetime,timedelta,timezone
import logging
from threading import Event,Thread

from .bookmakers import instant
from .verdict_sports import supported,capabilities
from .verdict_outcomes import MSK

PICK_TARGET=5

SCHEMA='''
CREATE TABLE IF NOT EXISTS featured_analysis_events (
 event_id TEXT PRIMARY KEY,home TEXT,away TEXT,sport TEXT,kickoff_at TEXT,
 state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 statistics_job_id TEXT,analysis_job_id TEXT,analysis_id TEXT,last_error TEXT,
 selection_source TEXT NOT NULL DEFAULT 'featured');
CREATE UNIQUE INDEX IF NOT EXISTS featured_one_preparation ON featured_analysis_events(state) WHERE state='preparing';
'''
LOG=logging.getLogger('uvicorn.error')


def enabled(repo,db=None):
    if db is None:
        with repo.connection() as connection:return enabled(repo,connection)
    row=db.execute("SELECT value FROM site_settings WHERE key='featured_auto_enabled'").fetchone()
    return not row or row[0]=='1'


def status(repo):
    from .today_picks import collection
    ready=collection(repo)['total']
    with repo.connection() as db:
        counts={r['state']:r['total'] for r in db.execute('SELECT state,count(*) total FROM featured_analysis_events GROUP BY state')}
        rows=[dict(r) for r in db.execute('''SELECT event_id,home,away,sport,kickoff_at,state,updated_at,last_error,analysis_id,selection_source
            FROM featured_analysis_events ORDER BY updated_at DESC LIMIT 20''')]
        return {'enabled':enabled(repo,db),'system_concurrency':1,'human_concurrency':1,'counts':counts,'events':rows,
                'pick_target':PICK_TARGET,'picks_ready':ready,'picks_needed':max(0,PICK_TARGET-ready),
                'selection_policy':'featured_then_upcoming_round_robin'}


def usable(event,now):
    try:
        return (supported(event) and not event.get('is_stale') and event.get('source_present') is not False and
            event.get('phase')=='prematch' and bool(event.get('kickoff_at')) and instant(event['kickoff_at'])>now)
    except (ValueError,TypeError):return False


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

    def candidates(self,now):
        from . import archive
        from .featured_events import collection as featured
        with self.repo.connection() as db:
            known={r[0] for r in db.execute('SELECT event_id FROM featured_analysis_events UNION SELECT event_id FROM ai_analysis_jobs WHERE lane=\'system\'')}
            last=db.execute("SELECT sport FROM featured_analysis_events WHERE selection_source='catalogue' ORDER BY created_at DESC,event_id DESC LIMIT 1").fetchone()
        main=[e for e in featured(self.repo,limit=1000)['events'] if e['source_event_id'] not in known and not e.get('is_stale') and e.get('source_present') is not False]
        if main:return main,'featured'
        from .today_picks import collection as picks
        if picks(self.repo,now=now)['total']>=PICK_TARGET:return [],'catalogue'
        today=now.astimezone(MSK).date();tomorrow=today+timedelta(days=1)
        groups={}
        for event in archive.catalog(self.repo,limit=None)['events']:
            if event['source_event_id'] in known or not usable(event,now):continue
            if not today<=instant(event['kickoff_at']).astimezone(MSK).date()<=tomorrow:continue
            groups.setdefault(event['sport'],[]).append(event)
        sports=sorted(capabilities())
        if last and last['sport'] in sports:
            index=sports.index(last['sport'])+1;sports=sports[index:]+sports[:index]
        # Each completed/failed attempt advances the sport cursor durably;
        # within a sport inspect the nearest kickoff first.
        return [event for sport in sports for event in sorted(groups.get(sport,[]),key=lambda e:(instant(e['kickoff_at']),e['source_event_id']))],'catalogue'

    def tick(self):
        if not enabled(self.repo) or not self.settings.qwen_configured or self.settings.event_analysis_mode!='neural-verdict':return False
        now=datetime.now(timezone.utc)
        from . import archive
        with self.repo.connection() as db:
            if db.execute("SELECT 1 FROM ai_analysis_jobs WHERE lane='system' AND state IN ('queued','running')").fetchone():return False
            preparing=db.execute("SELECT * FROM featured_analysis_events WHERE state='preparing'").fetchone()
        if preparing:
            row=dict(preparing)
            if row['selection_source']=='catalogue':
                from .today_picks import collection as picks
                if picks(self.repo,now=now)['total']>=PICK_TARGET:
                    self.update(row['event_id'],'skipped','Подборка на сегодня и завтра уже заполнена.');return True
        else:
            candidates,selection_source=self.candidates(now)
            row=None
            with self.repo.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT 1 FROM featured_analysis_events WHERE state='preparing'").fetchone():return False
                for event in candidates:
                    identifier=event['source_event_id']
                    if event.get('is_stale') or event.get('source_present') is False:continue
                    if db.execute('SELECT 1 FROM featured_analysis_events WHERE event_id=?',(identifier,)).fetchone():continue
                    eligible=usable(event,now)
                    state='preparing' if eligible else 'skipped'
                    db.execute('''INSERT INTO featured_analysis_events(event_id,home,away,sport,kickoff_at,state,created_at,updated_at,last_error,selection_source)
                        VALUES (?,?,?,?,?,?,?,?,?,?)''',(identifier,event.get('home'),event.get('away'),event.get('sport'),event.get('kickoff_at'),state,
                        now.isoformat(),now.isoformat(),None if eligible else 'Событие уже началось или формат не поддерживается.',selection_source))
                    if eligible:
                        row=dict(db.execute('SELECT * FROM featured_analysis_events WHERE event_id=?',(identifier,)).fetchone());break
            if not row:return False
        identifier=row['event_id'];document=archive.document(self.repo,identifier)
        if not document or not usable(document,now):
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
