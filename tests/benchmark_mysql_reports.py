"""Compare complete reports on an isolated, synthetic 30k-result MySQL archive."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json
import os
import sys
import time
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from matchlab.mysql_backend import MySQLTarget,Connection,initialize,template_schema,quote
from matchlab.storage import Repository
from matchlab.public_results import report
from matchlab.verdict_sports import PROFILES
from outcome_helpers import forecast


def main():
    import pymysql
    base=MySQLTarget(os.environ['MYSQL_TEST_ADMIN_URL']);options=base.options()
    if options['host'] not in ('127.0.0.1','localhost') or options['database']!='matchlab_test':raise SystemExit('Separate local test server only')
    name='matchlab_report_bench_'+uuid4().hex[:12];admin=pymysql.connect(**options,autocommit=True)
    admin.cursor().execute('CREATE DATABASE '+quote(name)+' CHARACTER SET utf8mb4')
    target=MySQLTarget(base.url.rsplit('/',1)[0]+'/'+name)
    try:
        initialize(target,schema=template_schema());repo=Repository(target)
        current=datetime.now(timezone.utc);sports=['football','hockey','basketball','tennis','volleyball']
        scores={'football':'2:0','hockey':'4:2','basketball':'87:75','tennis':'2:0','volleyball':'3:1'}
        rows=[]
        for i in range(30000):
            sport=sports[i%5];p=PROFILES[sport]
            at=(current-timedelta(days=10+i%120,hours=(i//120)%24)).isoformat()
            home,away='Other A '+str(i),'Other B '+str(i)
            value={'date':at,'member1':home,'member2':away,'championship':'Other','score':scores[sport]}
            rows.append(('other-'+str(i),p.archive_id,p.label,at,home,away,scores[sport],current.isoformat(),current.isoformat(),json.dumps(value)))
        for i in range(10):
            sport=sports[i%5];p=PROFILES[sport];kickoff=current-timedelta(days=2,hours=i)
            event={'source_event_id':str(i),'sport':sport,'phase':'prematch','kickoff_at':kickoff.isoformat(),
                'home':'Fixture A '+str(i),'away':'Fixture B '+str(i),'competition':'Target competition'}
            saved=forecast(repo,uuid4().hex,event=event,kickoff=kickoff,score=None)
            context=saved['context'];context['neural_verdict']['scope']=p.scope
            context['neural_input']['quote'].update(sport=sport,scope=p.scope)
            if not p.draw:context['neural_input']['quote']['odds'].pop('draw')
            with repo.connection() as db:db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(saved),saved['id']))
            value={'date':event['kickoff_at'],'member1':event['home'],'member2':event['away'],'championship':event['competition'],'score':scores[sport]}
            rows.append(('fixture-'+str(i),p.archive_id,p.label,event['kickoff_at'],event['home'],event['away'],scores[sport],current.isoformat(),current.isoformat(),json.dumps(value)))
        with repo.connection() as db:db.executemany('INSERT INTO archive_latest_results VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
        from matchlab.multisport_evidence import archive_results
        execute=Connection.execute
        reads=[]
        def measured(db,sql,*args,**kw):
            cursor=execute(db,sql,*args,**kw)
            if 'archive_latest_results' in sql and 'payload' in sql:reads.append(cursor.rowcount)
            return cursor
        def old_read(*args,**kw):
            kw.pop('fixtures',None);return archive_results(*args,**kw)
        def measure():
            reads.clear();start=time.perf_counter();value=report(repo,limit=20)
            return value,time.perf_counter()-start,sum(reads)
        with patch('matchlab.verdict_outcomes.datetime') as clock,patch.object(Connection,'execute',measured):
            clock.now.return_value=current
            with patch('matchlab.multisport_evidence.archive_results',old_read):
                measure();before,old,old_rows=measure()
            measure();after,new,new_rows=measure()
        assert before==after
        print(json.dumps({'archive_results':len(rows),'forecasts':10,'before_seconds':round(old,4),'after_seconds':round(new,4),
            'speedup':round(old/new,1),'archive_payloads_before':old_rows,'archive_payloads_after':new_rows,
            'identical_complete_public_report':True},indent=2))
    finally:admin.cursor().execute('DROP DATABASE '+quote(name));admin.close()


if __name__=='__main__':main()
