"""Run only persistence contracts against disposable, uniquely named MySQL DBs.

Requires MYSQL_TEST_ADMIN_URL for a LOCAL test server, database matchlab_test.
Never points a contract test at the application database. Each allocated test
schema is deleted in finally; credentials are never printed.
"""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import unittest
from urllib.parse import quote as urlquote
from uuid import uuid4

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from matchlab.config import Settings
from matchlab.storage import Repository as RealRepository
from matchlab.mysql_backend import MySQLTarget,quote,Connection,initialize,template_schema


def main():
    import pymysql
    target=MySQLTarget(os.environ['MYSQL_TEST_ADMIN_URL']);options=target.options()
    if options['host'] not in ('127.0.0.1','localhost','::1') or options['database']!='matchlab_test':raise SystemExit('Use only a separate local matchlab_test server/database.')
    admin=pymysql.connect(**options,autocommit=True)
    run=uuid4().hex[:12];allocated={};original=Settings.database
    def database(path):
        key=str(Path(path).resolve());name='matchlab_it_'+run+'_'+hashlib.sha256(key.encode()).hexdigest()[:12]
        if name not in allocated:
            admin.cursor().execute('CREATE DATABASE '+quote(name)+' CHARACTER SET utf8mb4')
            opts={**options,'database':name}
            allocated[name]=MySQLTarget('mysql://'+urlquote(opts['user'],safe='')+':'+urlquote(opts['password'],safe='')+'@'+opts['host']+':'+str(opts['port'])+'/'+name)
            schema=template_schema();initialize(allocated[name],schema=schema)
            db=Connection(allocated[name])
            try:
                c=db.raw.cursor();c.execute('CREATE TABLE `_matchlab_migration` (id INT PRIMARY KEY,payload LONGTEXT NOT NULL) ENGINE=InnoDB')
                c.execute('INSERT INTO `_matchlab_migration` VALUES (1,%s)',(json.dumps({'format':'matchlab.mysql-migration.v1','state':'verified','schema':schema}),))
            finally:db.close()
        return allocated[name]
    Settings.database=property(lambda self:database(self.data_dir))
    modules=sys.argv[1:] or ['test_billing','test_accounts','test_analysis_jobs','test_archive','test_merchant_prelaunch']
    try:
        suite=unittest.TestSuite()
        for name in modules:
            module_name=name.split('.')[0];module=importlib.import_module(module_name)
            if hasattr(module,'Repository'):
                module.Repository=lambda path:RealRepository(path if isinstance(path,MySQLTarget) or Path(path).is_file() else database(Path(path).parent))
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name) if name!=module_name else unittest.defaultTestLoader.loadTestsFromModule(module))
        result=unittest.TextTestRunner(verbosity=1,failfast=True).run(suite)
        return 0 if result.wasSuccessful() else 1
    finally:
        Settings.database=original
        for name in allocated:admin.cursor().execute('DROP DATABASE '+quote(name))
        admin.close()


if __name__=='__main__':raise SystemExit(main())
