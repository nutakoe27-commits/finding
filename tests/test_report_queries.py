"""Indexed settlement candidates, timezone boundaries and honest final grading."""
from datetime import datetime,timedelta,timezone
import json
import os
import unittest
from uuid import uuid4

import test_mysql_migration as fixture
from matchlab.database_migration import inventory,migrate
from matchlab.mysql_backend import Connection
from matchlab.report_queries import START_COLUMN,RECEIVED_COLUMN,fixture_results
from matchlab.storage import Repository
from matchlab.verdict_outcomes import run
from outcome_helpers import forecast,archive_result


@unittest.skipUnless(os.getenv('MYSQL_TEST_ADMIN_URL'),'Separate local MySQL test server required')
class ReportQueryTests(unittest.TestCase):
    setUp=fixture.MySQLMigrationTests.setUp

    def ready(self):
        migrate(self.source,self.target,self.folder/'verified.sqlite3',apply=True)
        repo=Repository(self.target)
        with repo.connection() as db:
            db.execute('DELETE FROM archive_latest_results');db.execute('DELETE FROM event_analyses')
        return repo

    def save(self,repo,**kw):return forecast(repo,uuid4().hex,**kw)

    def test_timezone_and_120_second_boundaries_preserve_ambiguity_and_corrections(self):
        repo=self.ready();at=datetime.now(timezone.utc)-timedelta(days=1)
        p=self.save(repo,kickoff=at,score=None)
        event={**p['event'],'kickoff_at':(at+timedelta(seconds=120)).astimezone(timezone(timedelta(hours=5,minutes=30))).isoformat()}
        archive_result(repo,event,'2:0','offset')
        self.assertEqual(run(repo)['metrics']['bet_wins'],1)
        archive_result(repo,event,'0:2','offset')
        self.assertEqual(run(repo)['metrics']['bet_losses'],1)
        event['kickoff_at']=(at-timedelta(seconds=120)).astimezone(timezone(timedelta(hours=-7))).isoformat()
        archive_result(repo,event,'2:0','second')
        self.assertEqual(run(repo)['matches'][0]['state'],'unresolved')
        event['kickoff_at']=(at-timedelta(seconds=120,microseconds=1000)).isoformat()
        archive_result(repo,event,'2:0','second')
        self.assertEqual(run(repo)['metrics']['bet_losses'],1)

    def test_generated_time_moves_with_raw_updates_and_rolls_back(self):
        repo=self.ready();p=self.save(repo)
        with repo.connection() as db:
            from matchlab.winline_backtest import results
            self.assertEqual(results(db,fixtures=[p['event']])[0],results(db)[0])
            self.assertEqual(len(list(fixture_results(db,1,[p['event']]))),1)
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE archive_latest_results SET start_at=?',((datetime.now(timezone.utc)-timedelta(days=100)).isoformat(),))
            self.assertEqual(list(fixture_results(db,1,[p['event']])),[])
            db.rollback()
        with repo.connection() as db:self.assertEqual(len(list(fixture_results(db,1,[p['event']]))),1)

    def test_legacy_compact_timestamp_falls_back_and_empty_cohort_reads_no_results(self):
        repo=self.ready();p=self.save(repo)
        at=datetime.fromisoformat(p['event']['kickoff_at'])
        with repo.connection() as db:db.execute('UPDATE archive_latest_results SET start_at=?',(at.strftime('%Y%m%dT%H%M%SZ'),))
        self.assertEqual(run(repo)['metrics']['bet_wins'],1)
        with repo.connection() as db:self.assertEqual(list(fixture_results(db,1,[])),[])

    def test_generated_utc_time_accepts_archive_timestamp_formats(self):
        repo=self.ready();p=self.save(repo,kickoff=datetime.now(timezone.utc).replace(microsecond=0)-timedelta(days=1))
        at=datetime.fromisoformat(p['event']['kickoff_at'])
        for value in (at.isoformat(),at.isoformat().replace('+00:00','Z'),at.isoformat(timespec='milliseconds'),at.isoformat().replace('+00:00','+0000'),
                at.astimezone(timezone(timedelta(hours=-4))).isoformat(),at.astimezone(timezone(timedelta(hours=5,minutes=30))).isoformat()):
            with repo.connection() as db:db.execute('UPDATE archive_latest_results SET start_at=?',(value,))
            with repo.connection() as db:self.assertEqual(len(list(fixture_results(db,1,[p['event']]))),1,value)

    def test_existing_archive_backfills_indexes_without_altering_original_values(self):
        repo=self.ready();self.save(repo)
        with repo.connection() as db:
            cursor=db.raw.cursor()
            cursor.execute('ALTER TABLE archive_latest_results DROP INDEX ml_result_fixture_time_v1, DROP INDEX ml_result_delivery_time_v1, DROP COLUMN `'+START_COLUMN+'`, DROP COLUMN `'+RECEIVED_COLUMN+'`')
            before=inventory(db,self.schema)
        repo.initialize()
        with repo.connection() as db:self.assertEqual(inventory(db,self.schema),before)
        self.assertEqual(run(repo)['metrics']['bet_wins'],1)

    def test_time_lookup_uses_range_index_and_does_not_read_unrelated_archive(self):
        repo=self.ready();p=self.save(repo)
        observed=datetime.now(timezone.utc).isoformat();rows=[]
        for i in range(1200):
            at=(datetime.now(timezone.utc)-timedelta(days=100,minutes=i)).isoformat()
            value={'date':at,'member1':'Other A','member2':'Other B','championship':'Other','score':'1:0'}
            rows.append(('other-'+str(i),1,'Футбол',at,'Other A','Other B','1:0',observed,observed,json.dumps(value)))
        with repo.connection() as db:
            db.executemany('INSERT INTO archive_latest_results VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
        with repo.connection() as db:
            self.assertEqual(len(list(fixture_results(db,1,[p['event']]))),1)
            sql='SELECT payload FROM archive_latest_results WHERE sport_id=1 AND (`'+START_COLUMN+'` BETWEEN julianday(?) AND julianday(?) OR `'+START_COLUMN+'` IS NULL)'
            c=db.raw.cursor();from matchlab.mysql_backend import compile_sql
            translated,_=compile_sql(sql);at=datetime.fromisoformat(p['event']['kickoff_at'])
            c.execute('EXPLAIN '+translated,{'p0':(at-timedelta(seconds=121)).isoformat(),'p1':(at+timedelta(seconds=121)).isoformat()})
            plan=dict(zip([x[0] for x in c.description],c.fetchone()))
            self.assertEqual(plan['key'],'ml_result_fixture_time_v1');self.assertEqual(plan['type'],'range')
        self.assertEqual(run(repo)['metrics']['bet_wins'],1)


if __name__=='__main__':unittest.main()
