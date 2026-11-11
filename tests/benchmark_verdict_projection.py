"""Local synthetic read benchmark; never use an application database URL."""
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
import json
import os
import sys
import time
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))

from matchlab.mysql_backend import MySQLTarget,Connection,initialize,template_schema,quote
from matchlab.verdict_outcomes import site_records
from matchlab.verdict_projection import MYSQL_COLUMN
from test_verdict_projection import LegacyRead


def main():
    import pymysql
    target=MySQLTarget(os.environ['MYSQL_TEST_ADMIN_URL']);options=target.options()
    if options['host'] not in ('127.0.0.1','localhost') or options['database']!='matchlab_test':raise SystemExit('Separate local test server only')
    name='matchlab_report_perf_'+uuid4().hex[:12];admin=pymysql.connect(**options,autocommit=True)
    admin.cursor().execute('CREATE DATABASE '+quote(name)+' CHARACTER SET utf8mb4')
    target=MySQLTarget(target.url.rsplit('/',1)[0]+'/'+name)
    try:
        initialize(target,schema=template_schema())
        with closing(Connection(target)) as db:
            db.raw.cursor().execute('ALTER TABLE event_analyses DROP COLUMN `'+MYSQL_COLUMN+'`')
            at=datetime.now(timezone.utc).isoformat()
            payload=json.dumps({'event':{'sport':'football','home':'Fixture A','away':'Fixture B'},'context':{
                'neural_verdict':{'engine':'qwen','decision':'skip','preference':None},
                'ai_generation':{'state':'ready'},'team_statistics':{'observed_at':at,'match_phase':'prematch','raw':'x'*262144}}})
            rows=[(uuid4().hex,'fixture-'+str(i),at,payload,'fixture-owner') for i in range(128)]
            db.executemany('INSERT INTO event_analyses(id,request_key,generated_at,payload,user_id) VALUES (?,?,?,?,?)',rows);db.commit()
        start=time.perf_counter();initialize(target,schema=template_schema());backfill=time.perf_counter()-start
        with closing(Connection(target)) as db:
            def measure(source):
                start=time.perf_counter();records=list(site_records(source));return records,time.perf_counter()-start
            measure(LegacyRead(db));measure(db)
            legacy,old=measure(LegacyRead(db));compact,new=measure(db)
            assert compact==legacy
        print(json.dumps({'rows':len(rows),'raw_input_bytes':len(payload.encode())*len(rows),'backfill_seconds':round(backfill,3),
            'legacy_query_seconds':round(old,4),'compact_query_seconds':round(new,4),'speedup':round(old/new,1),
            'identical_report_records':True},indent=2))
    finally:admin.cursor().execute('DROP DATABASE '+quote(name));admin.close()


if __name__=='__main__':main()
