"""Full-table typed checksums and rollback-safe migration on a disposable MySQL."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from matchlab.storage import Repository
from matchlab.mysql_backend import MySQLTarget,Connection,inspect_sqlite,quote,readonly
from matchlab.database_migration import migrate,export_sqlite,inventory


@unittest.skipUnless(os.getenv('MYSQL_TEST_ADMIN_URL'),'Separate local MySQL test server required')
class MySQLMigrationTests(unittest.TestCase):
    def setUp(self):
        import pymysql
        base=MySQLTarget(os.environ['MYSQL_TEST_ADMIN_URL']);options=base.options()
        if options['host'] not in ('127.0.0.1','localhost') or options['database']!='matchlab_test':raise RuntimeError('Use only local matchlab_test')
        self.admin=pymysql.connect(**options,autocommit=True);self.addCleanup(self.admin.close)
        self.name='matchlab_migration_test_'+uuid4().hex[:12]
        self.admin.cursor().execute('CREATE DATABASE '+quote(self.name)+' CHARACTER SET utf8mb4')
        self.addCleanup(lambda:self.admin.cursor().execute('DROP DATABASE '+quote(self.name)))
        self.target=MySQLTarget(base.url.rsplit('/',1)[0]+'/'+self.name)
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.folder=Path(temp.name)
        self.source=self.folder/'source.sqlite3';repo=Repository(self.source);repo.initialize()
        with repo.connection() as db:
            schema=inspect_sqlite(db)
            for table in schema:
                values=[]
                for c in table['columns']:
                    if c['type']=='BLOB':value=b'\x00\xff\x80'+bytes(range(256))
                    elif c['type']=='INTEGER':value=1
                    elif c['name']=='role':value='user'
                    elif c['name'] in ('payload','metadata','details','report','event_data','keys_json'):value=json.dumps({'home':'Команда 🌍','null':None,'x':'a\x00b'},ensure_ascii=False)
                    else:value='field_'+c['name']
                    if not c['notnull'] and not c['pk'] and c['name'] not in ('payload','metadata'):value=None
                    values.append(value)
                db.execute('INSERT INTO '+quote(table['name'])+' VALUES ('+','.join('?' for _ in values)+')',values)
            db.execute('CREATE TABLE migration_extra (key TEXT PRIMARY KEY,value TEXT,data BLOB,number REAL)')
            for key in ('A','a','a '):db.execute('INSERT INTO migration_extra VALUES (?,?,?,?)',(key,'Привет 🌍\x00x',b'\xff\x00',1.234567890123))
            self.schema=inspect_sqlite(db);self.original=inventory(db,self.schema)

    def test_rehearsal_import_and_reverse_backup_preserve_every_value(self):
        before=self.source.read_bytes()
        rehearsal=migrate(self.source,self.target,self.folder/'rehearsal.sqlite3')
        self.assertEqual(rehearsal['state'],'rehearsal_verified');self.assertEqual(rehearsal['tables'],self.original)
        with closing(Connection(self.target)) as db:self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],0)
        report=migrate(self.source,self.target,self.folder/'final.sqlite3',apply=True)
        self.assertEqual(report['tables'],self.original);self.assertEqual(self.source.read_bytes(),before)
        with closing(Connection(self.target)) as db:
            db.execute('UPDATE users SET bonus_analyses=bonus_analyses+7');db.commit()
            current=inventory(db,self.schema)
        destination=export_sqlite(self.target,self.folder/'newest.sqlite3')
        with readonly(destination) as db:self.assertEqual(inventory(db,self.schema),current)
        self.assertNotEqual(current,self.original);self.assertEqual(destination.stat().st_mode&0o777,0o600)

    def test_changed_source_check_rolls_back_and_existing_data_is_never_replaced(self):
        def changed(*args):raise ValueError('Source changed')
        with self.assertRaisesRegex(ValueError,'Source changed'):
            migrate(self.source,self.target,self.folder/'aborted.sqlite3',apply=True,frozen_check=changed)
        with closing(Connection(self.target)) as db:self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],0)
        migrate(self.source,self.target,self.folder/'good.sqlite3',apply=True)
        with self.assertRaisesRegex(ValueError,'уже есть данные'):
            migrate(self.source,self.target,self.folder/'blocked.sqlite3',apply=True)
        with closing(Connection(self.target)) as db:self.assertEqual(inventory(db,self.schema),self.original)

    def test_refuses_an_unrelated_database_and_unknown_backup_columns(self):
        with closing(Connection(self.target)) as db:db.raw.cursor().execute('CREATE TABLE unrelated (id INT PRIMARY KEY)')
        with self.assertRaisesRegex(ValueError,'не пуста'):
            migrate(self.source,self.target,self.folder/'blocked.sqlite3')

    def test_datetime_and_json_semantics_match_sqlite(self):
        with closing(Connection(self.target)) as db,readonly(self.source) as sqlite:
            for value in ('2026-11-11T12:30:00.123456+03:00','2026-11-11T09:30:00.123456Z','2026-11-11','2026-11-11T09:30:00+00:00'):
                expected=sqlite.execute('SELECT julianday(?)',(value,)).fetchone()[0]
                actual=db.execute('SELECT julianday(?)',(value,)).fetchone()[0]
                self.assertAlmostEqual(actual,expected,places=8)
            q="SELECT json_extract(?, '$.x','$.absent','$.null'),json_type(?, '$.obj')"
            params=('{"x":3,"null":null}','{"obj":{}}')
            self.assertEqual(json.loads(db.execute(q,params).fetchone()[0]),json.loads(sqlite.execute(q,params).fetchone()[0]))
            self.assertEqual(db.execute(q,params).fetchone()[1],'object')
            for value in ('2026-11-11T21:30:00Z','2026-11-11T02:00:00+03:00'):
                q="SELECT date(?, '+3 hours')"
                self.assertEqual(str(db.execute(q,(value,)).fetchone()[0]),sqlite.execute(q,(value,)).fetchone()[0])
            self.assertEqual(tuple(db.execute("SELECT json_extract(?, '$.x')=1,json_extract(?, '$.x')=0",('{"x":true}','{"x":false}')).fetchone()),(1,1))
        self.assertNotIn('local-root-fixture-only',str(self.target));self.assertNotIn('local-root-fixture-only',repr(self.target))

    def test_runtime_refuses_unverified_target_and_backup_refuses_unknown_columns(self):
        from matchlab.mysql_backend import initialize
        with self.assertRaisesRegex(ValueError,'проверенной миграции'):initialize(self.target)
        migrate(self.source,self.target,self.folder/'good.sqlite3',apply=True)
        with closing(Connection(self.target)) as db:db.raw.cursor().execute('ALTER TABLE users ADD COLUMN future_field TEXT')
        path=self.folder/'incomplete.sqlite3'
        with self.assertRaisesRegex(ValueError,'Схема MySQL изменилась'):export_sqlite(self.target,path)
        self.assertFalse(path.exists())

    def test_application_privileges_are_sufficient_without_server_admin(self):
        from matchlab.mysql_backend import initialize
        name='ml_limit_'+uuid4().hex[:10];password='local-limited-fixture-only'
        self.admin.cursor().execute('CREATE USER %s@%s IDENTIFIED BY %s',(name,'%',password))
        self.addCleanup(lambda:self.admin.cursor().execute('DROP USER '+repr(name)+"@'%'"))
        self.admin.cursor().execute('GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,REFERENCES ON '+quote(self.name)+'.* TO '+repr(name)+"@'%'")
        base=MySQLTarget(os.environ['MYSQL_TEST_ADMIN_URL']).options()
        target=MySQLTarget('mysql://'+name+':'+password+'@127.0.0.1:'+str(base['port'])+'/'+self.name)
        migrate(self.source,target,self.folder/'limited.sqlite3',apply=True);initialize(target)
        with closing(Connection(target)) as db:
            db.execute('BEGIN IMMEDIATE');db.execute('UPDATE users SET bonus_analyses=bonus_analyses+1');db.commit()
        export_sqlite(target,self.folder/'limited-backup.sqlite3')

    def test_catalogue_preserves_unicode_casefold_search(self):
        from test_product import event
        migrate(self.source,self.target,self.folder/'good.sqlite3',apply=True)
        repo=Repository(self.target)
        with repo.connection() as db:db.execute('DELETE FROM bookmaker_event_markets')
        repo.import_event_markets(event('123',home='Straße'))
        result=repo.market_events(search='STRASSE')
        self.assertEqual(result['total'],1)
        self.assertEqual(result['events'][0]['home'],'Straße')

    def test_additive_app_schema_changes_remain_in_portable_backups(self):
        from matchlab.mysql_backend import initialize
        with closing(sqlite3.connect(self.source)) as db:db.execute('ALTER TABLE users DROP COLUMN last_login_at');db.commit()
        migrate(self.source,self.target,self.folder/'older.sqlite3',apply=True)
        initialize(self.target)
        path=export_sqlite(self.target,self.folder/'updated.sqlite3')
        with readonly(path) as db:self.assertIn('last_login_at',{r['name'] for r in db.execute('PRAGMA table_info(users)')})


if __name__=='__main__':unittest.main()
