"""Upgrade existing payment data and verify a complete portable backup."""
from contextlib import closing
import sqlite3
import unittest
import os

import test_mysql_migration as fixture
from matchlab.storage import Repository
from matchlab.database_migration import migrate,inventory,export_sqlite
from matchlab.mysql_backend import inspect_sqlite,readonly


@unittest.skipUnless(os.getenv('MYSQL_TEST_ADMIN_URL'),'Separate local MySQL test server required')
class LiveBillingUpgradeTests(unittest.TestCase):
    setUp=fixture.MySQLMigrationTests.setUp

    def test_existing_orders_ledger_and_unlocks_survive_new_receipt_schema_and_reverse_backup(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.row_factory=sqlite3.Row
            db.execute('DROP TABLE analysis_credit_allocations');db.execute('DROP TABLE billing_receipt_jobs')
            db.execute('ALTER TABLE payment_orders DROP COLUMN receipt_email');db.commit()
            old_schema=inspect_sqlite(db);before=inventory(db,old_schema)
        migrate(self.source,self.target,self.folder/'before-live.sqlite3',apply=True)
        repo=Repository(self.target);repo.initialize()
        with repo.connection() as db:self.assertEqual(inventory(db,old_schema),before)
        path=export_sqlite(self.target,self.folder/'after-live.sqlite3')
        with readonly(path) as db:
            self.assertEqual(inventory(db,old_schema),before)
            self.assertIn('receipt_email',{r['name'] for r in db.execute('PRAGMA table_info(payment_orders)')})
            self.assertIsNone(db.execute('SELECT receipt_email FROM payment_orders').fetchone()[0])
            self.assertEqual(db.execute('SELECT count(*) FROM analysis_credit_allocations').fetchone()[0],0)


if __name__=='__main__':unittest.main()
