"""Upgrade a populated production schema without changing existing values."""
from contextlib import closing
import os
import sqlite3
import unittest
import test_mysql_migration as fixture
from matchlab.storage import Repository
from matchlab.database_migration import migrate,inventory,export_sqlite
from matchlab.mysql_backend import inspect_sqlite,readonly


@unittest.skipUnless(os.getenv('MYSQL_TEST_ADMIN_URL'),'Separate local MySQL required')
class SubscriptionUpgradeTests(unittest.TestCase):
    setUp=fixture.MySQLMigrationTests.setUp
    def test_orders_paid_credits_sessions_and_unlocked_answers_survive_additive_upgrade(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.row_factory=sqlite3.Row
            for table in ('billing_subscriptions','billing_subscription_charges','billing_subscription_periods'):db.execute('DROP TABLE '+table)
            db.execute('DROP INDEX orders_subscription');db.execute('ALTER TABLE payment_orders DROP COLUMN subscription_id');db.commit()
            old=inspect_sqlite(db);before=inventory(db,old)
        migrate(self.source,self.target,self.folder/'before-subscriptions.sqlite3',apply=True)
        repo=Repository(self.target);repo.initialize()
        with repo.connection() as db:
            self.assertEqual(inventory(db,old),before)
            self.assertIsNone(db.execute('SELECT subscription_id FROM payment_orders').fetchone()[0])
            self.assertEqual(db.execute('SELECT count(*) FROM billing_subscriptions').fetchone()[0],0)
        snapshot=export_sqlite(self.target,self.folder/'after-subscriptions.sqlite3')
        with readonly(snapshot) as db:self.assertEqual(inventory(db,old),before)
if __name__=='__main__':unittest.main()
