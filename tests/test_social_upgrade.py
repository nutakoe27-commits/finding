"""Add OAuth to a populated MySQL without changing users, purchases or sessions."""
from contextlib import closing
import os
import sqlite3
import unittest

import test_mysql_migration as fixture
from matchlab.storage import Repository
from matchlab.database_migration import migrate,inventory,export_sqlite
from matchlab.mysql_backend import inspect_sqlite,readonly


@unittest.skipUnless(os.getenv('MYSQL_TEST_ADMIN_URL'),'Separate local MySQL test server required')
class SocialUpgradeTests(unittest.TestCase):
    setUp=fixture.MySQLMigrationTests.setUp

    def test_existing_accounts_sessions_and_paid_access_survive_schema_upgrade_and_portable_backup(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.row_factory=sqlite3.Row
            db.execute('DROP TABLE social_login_attempts');db.execute('DROP TABLE social_login_sources')
            db.execute('ALTER TABLE users DROP COLUMN display_name');db.execute('ALTER TABLE users DROP COLUMN last_login_provider');db.commit()
            old=inspect_sqlite(db);before=inventory(db,old)
        migrate(self.source,self.target,self.folder/'before-social.sqlite3',apply=True)
        repo=Repository(self.target);repo.initialize()
        with repo.connection() as db:self.assertEqual(inventory(db,old),before)
        snapshot=export_sqlite(self.target,self.folder/'after-social.sqlite3')
        with readonly(snapshot) as db:
            self.assertEqual(inventory(db,old),before)
            self.assertEqual(db.execute('SELECT last_login_provider FROM users').fetchone()[0],'local')
            self.assertEqual(db.execute('SELECT count(*) FROM social_login_attempts').fetchone()[0],0)
