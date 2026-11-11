"""Compact MySQL report reads must agree with the original saved-snapshot query."""
from contextlib import closing
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_mysql_migration as fixture
from outcome_helpers import forecast
from matchlab.storage import Repository
from matchlab.mysql_backend import Connection
from matchlab.database_migration import migrate,export_sqlite,inventory
from matchlab.verdict_outcomes import site_records
from matchlab.verdict_projection import MYSQL_COLUMN


class LegacyRead:
    dialect='sqlite'
    def __init__(self,db):self.db=db
    def execute(self,*args):return self.db.execute(*args)


@unittest.skipUnless(os.getenv('MYSQL_TEST_ADMIN_URL'),'Separate local MySQL test server required')
class VerdictProjectionTests(unittest.TestCase):
    setUp=fixture.MySQLMigrationTests.setUp

    def ready(self):
        migrate(self.source,self.target,self.folder/'verified.sqlite3',apply=True)
        return Repository(self.target)

    def save(self,repo,**kw):
        p=forecast(repo,uuid4().hex,score=None,**kw)
        p['context']['team_statistics']['large_private_blob']='PRIVATE_STATISTICS_NOT_FOR_REPORTS'*4096
        with repo.connection() as db:db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(p),p['id']))
        return p

    def test_compact_records_equal_original_query_and_exclude_private_input(self):
        repo=self.ready()
        self.save(repo);self.save(repo,decision='skip',preference=None);self.save(repo,engine='model_error')
        with repo.connection() as db:
            self.assertEqual(list(site_records(db)),list(site_records(LegacyRead(db))))
            encoded=json.dumps(list(site_records(db)))
        for secret in ('PRIVATE_NOTES_DO_NOT_EXPORT','PRIVATE_CONTEXT_DO_NOT_EXPORT','PRIVATE_FULL_PROMPT_DO_NOT_EXPORT','PRIVATE_STATISTICS_NOT_FOR_REPORTS'):
            self.assertNotIn(secret,encoded)

    def test_raw_updates_regenerate_projection_atomically_and_rollback_together(self):
        repo=self.ready();p=self.save(repo)
        p['context']['neural_verdict'].update(decision='skip',preference=None)
        with repo.connection() as db:db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(p),p['id']))
        with repo.connection() as db:self.assertEqual(next(site_records(db))['verdict']['decision'],'skip')
        p['context']['neural_verdict']['decision']='bet'
        with self.assertRaisesRegex(ValueError,'abort'):
            with repo.connection() as db:
                db.execute('BEGIN IMMEDIATE');db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(p),p['id']))
                self.assertEqual(next(site_records(db))['verdict']['decision'],'bet')
                raise ValueError('abort')
        with repo.connection() as db:self.assertEqual(next(site_records(db))['verdict']['decision'],'skip')

    def test_malformed_and_non_verdict_payloads_do_not_break_projection(self):
        repo=self.ready();p=self.save(repo)
        for payload in ('not-json','{}','{"context":{"neural_verdict":[]}}','null'):
            with repo.connection() as db:db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(payload,p['id']))
            with repo.connection() as db:self.assertEqual(list(site_records(db)),[])

    def test_full_snapshots_stay_identical_and_portable_backup_keeps_records(self):
        repo=self.ready();p=self.save(repo)
        with repo.connection() as db:
            before=db.execute('SELECT payload FROM event_analyses WHERE id=?',(p['id'],)).fetchone()[0]
            expected=list(site_records(db))
        repo.initialize();path=export_sqlite(self.target,self.folder/'backup.sqlite3')
        import sqlite3
        with closing(sqlite3.connect(path)) as db:
            db.row_factory=sqlite3.Row
            self.assertEqual(db.execute('SELECT payload FROM event_analyses WHERE id=?',(p['id'],)).fetchone()[0],before)
            self.assertNotIn(MYSQL_COLUMN,{r['name'] for r in db.execute('PRAGMA table_info(event_analyses)')})
            self.assertEqual(list(site_records(db)),expected)

    def test_existing_populated_mysql_is_backfilled_without_changing_any_saved_value(self):
        repo=self.ready()
        with repo.connection() as db:db.raw.cursor().execute('ALTER TABLE event_analyses DROP COLUMN `'+MYSQL_COLUMN+'`')
        self.save(repo)
        with repo.connection() as db:
            before=inventory(db,self.schema);expected=list(site_records(LegacyRead(db)))
        repo.initialize()
        with repo.connection() as db:
            self.assertEqual(inventory(db,self.schema),before)
            self.assertEqual(list(site_records(db)),expected)


if __name__=='__main__':unittest.main()
