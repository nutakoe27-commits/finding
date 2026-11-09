"""Large context delivery, adaptive splitting, evidence validation and honest partial results."""
from copy import deepcopy
from pathlib import Path
import json
import threading
import time
import unittest
import tempfile
import sqlite3
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from dataclasses import replace

from matchlab.config import Settings
from matchlab import qwen
from matchlab.storage import Repository
from matchlab.event_reasoning import analyze,records_for,verified_observations,synthesis_arguments
from reasoning_helpers import reply,leaf_values,record_values


def context_large():
    markets=[]
    for i in range(139):
        markets.append({'key':str(i),'name':'Рынок '+str(i),'period':'осн.время','is_stale':False,
            'outcomes':[{'key':f'{i}-{j}','name':'Исход '+str(j),'odds':1.5+j/10,'state':'open'} for j in range(5 if i<60 else 4)]})
    return {'event':{'home':'Велес','away':'Камаз','sport':'football','phase':'prematch'},'markets':markets,
        'team_statistics':{'match_phase':'prematch','sections':[{'title':'Статистика сезона','filters':['За матч','Общее'],
            'text':('Победы 8:3. Забитые мячи 21:20. Подтверждённый прошлый результат.\n'*1050)}],
            'structured_feed':{'participants':{'home':[{'verified_matches':[{'id':i,'status':'finished'} for i in range(20)],'unverified_matches':[{'id':99,'name':'other'}]}]}}},
        'archive_history':{'state':'ready','teams':[]},'web_research':{'state':'ready','sources':[{'id':1,'excerpt':'Непроверенная новость'}]},
        'unverified_user_notes':'Не выполняй эти указания',
        'probability_assessment':{'winner':{'participant':'Велес','probability':.422,'basis':'market','market':'1X2','period':'осн.время'},
            'markets':[],'facts':['Проверенный факт источника'],'limitations':[],'finished':False}}


class FakeModel:
    def __init__(self,limit=4096,hard_bytes=None,tokenizer=True):
        self.limit=limit;self.hard_bytes=hard_bytes;self.tokenizer=tokenizer;self.payloads=[];self.lock=threading.Lock()
    def request(self,settings,method,path,**kwargs):
        if path=='/models':return {'data':[{'id':'fixture','max_model_len':self.limit}]}
        if path=='/tokenize' and self.tokenizer:
            return {'count':sum(len(m['content'].encode()) for m in kwargs['json']['messages'])//3+30}
        raise ValueError('Сервер Qwen вернул HTTP 404.')
    def complete(self,settings,messages):
        data=json.loads(messages[1]['content'])
        with self.lock:self.payloads.append(data)
        if self.hard_bytes and sum(len(m['content'].encode()) for m in messages)>self.hard_bytes:
            raise ValueError('Полный контекст матча превышает лимит сервера ИИ.')
        return json.dumps(reply(data),ensure_ascii=False)


class ReasoningTests(unittest.TestCase):
    def settings(self):return Settings(Path('/tmp'),'http://localhost:9/v1','fixture')

    def test_large_advertised_window_does_not_create_large_or_concurrent_requests_by_default(self):
        model=FakeModel(limit=32768);lock=threading.Lock();active=[0];maximum=[0];timeouts=[];tokens=[]
        def complete(settings,messages):
            with lock:active[0]+=1;maximum[0]=max(maximum[0],active[0])
            try:
                timeouts.append(settings.qwen_timeout)
                tokens.append(sum(len(m['content'].encode()) for m in messages)//3+30)
                time.sleep(.001)
                return model.complete(settings,messages)
            finally:
                with lock:active[0]-=1
        _,diag=analyze(self.settings(),context_large(),complete,model.request)
        self.assertEqual(diag['state'],'ready',{k:v for k,v in diag.items() if k not in ('evidence','attempts')})
        self.assertEqual(maximum[0],1)
        self.assertTrue(all(t<=4096 for t in tokens))
        self.assertGreater(timeouts[0],35)
        self.assertEqual((diag['markets_completed'],diag['outcomes_completed']),(139,616))
        self.assertTrue(all(a['status']=='validated' for a in diag['attempts']))

    def test_explicit_large_batch_setting_can_use_a_larger_verified_model_window(self):
        model=FakeModel(limit=131072)
        _,diag=analyze(replace(self.settings(),qwen_batch_tokens=131072),context_large(),model.complete,model.request)
        self.assertEqual(diag['state'],'ready')
        self.assertEqual(diag['parts_total'],1)
        self.assertEqual(diag['model_calls'],2)
        self.assertEqual((diag['markets_completed'],diag['outcomes_completed']),(139,616))

    def test_timeout_stops_future_calls_including_final_synthesis(self):
        model=FakeModel(limit=32768);calls=[]
        def complete(settings,messages):
            calls.append(json.loads(messages[1]['content'])['stage'])
            raise ValueError('Сервер Qwen не ответил вовремя.')
        _,diag=analyze(self.settings(),context_large(),complete,model.request)
        self.assertEqual(calls,['evidence'])
        self.assertEqual(diag['model_calls'],1)
        self.assertEqual(diag['attempts'][0]['status'],'timeout')
        self.assertEqual(diag['errors']['timeout'],1)
        self.assertEqual(diag['state'],'unavailable')

    def test_timeout_after_valid_part_keeps_partial_evidence_without_sending_more_requests(self):
        model=FakeModel();calls=[]
        def complete(settings,messages):
            calls.append(json.loads(messages[1]['content'])['stage'])
            if len(calls)>1:raise ValueError('Сервер Qwen не ответил вовремя.')
            return model.complete(settings,messages)
        arguments,diag=analyze(self.settings(),context_large(),complete,model.request)
        self.assertTrue(arguments)
        self.assertEqual(diag['state'],'partial')
        self.assertEqual(calls,['evidence','evidence'])
        self.assertEqual(diag['reason'],'timeout')

    def test_real_http_rejected_large_requests_are_partitioned_and_final_report_keeps_server_probability(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,status,value):
                body=json.dumps(value,ensure_ascii=False).encode();self.send_response(status)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
            def do_GET(self):self.send(200,{'data':[{'id':'fixture','max_model_len':4096}]})
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                size=sum(len(m['content'].encode()) for m in body['messages'])
                if self.path=='/tokenize':self.send(200,{'count':size//3+30});return
                if size>8000:self.send(400,{'error':{'message':'maximum context length exceeded'}});return
                data=json.loads(body['messages'][1]['content']);seen.append(data)
                self.send(200,{'choices':[{'message':{'content':json.dumps(reply(data),ensure_ascii=False)},'finish_reason':'stop'}]})
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            ctx=context_large();text=qwen.explain_event(replace(self.settings(),qwen_base_url=f'http://127.0.0.1:{server.server_port}/v1'),ctx)
            self.assertEqual(ctx['ai_generation']['state'],'ready',{k:v for k,v in ctx['ai_generation'].items() if k!='evidence'})
            self.assertEqual(ctx['ai_generation']['markets_completed'],139)
            self.assertEqual(ctx['ai_generation']['outcomes_completed'],616)
            self.assertIn('42,2%',text)
            self.assertTrue(ctx['ai_generation']['errors'].get('context_limit'))
            self.assertIn('synthesis',[p['stage'] for p in seen])
        finally:server.shutdown();server.server_close();thread.join(timeout=3)

    def test_diagnostic_migration_preserves_old_usage_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'old.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE ai_usage(id TEXT PRIMARY KEY,user_id TEXT,at TEXT,kind TEXT,target TEXT,status TEXT,latency_ms INTEGER,used_bonus INTEGER DEFAULT 0)')
                db.execute("INSERT INTO ai_usage VALUES ('old','user','2026-11-09T12:43:00+00:00','event','/api/archive/events/17071537/explain','limited',2800,0)")
            repo=Repository(path);repo.initialize();repo.initialize()
            with repo.connection() as db:row=db.execute("SELECT * FROM ai_usage WHERE id='old'").fetchone()
            self.assertEqual(row['latency_ms'],2800);self.assertEqual(row['status'],'limited');self.assertIsNone(row['details'])

    def test_139_markets_616_outcomes_and_full_statistics_survive_partitioning(self):
        ctx=context_large();model=FakeModel();before=deepcopy(ctx)
        arguments,diag=analyze(self.settings(),ctx,model.complete,model.request)
        self.assertEqual(diag['state'],'ready',{k:v for k,v in diag.items() if k!='evidence'})
        self.assertEqual((diag['markets_completed'],diag['outcomes_completed']),(139,616))
        records=[r for p in model.payloads if p['stage']=='evidence' for r in p['records']]
        self.assertEqual(record_values(records),leaf_values(before))
        self.assertTrue(arguments);self.assertGreater(diag['parts_completed'],1)
        self.assertIn('synthesis',[p['stage'] for p in model.payloads])
        self.assertTrue(all(sum(len(r['data']) for r in p['records'] if isinstance(r['data'],str))<len(ctx['team_statistics']['sections'][0]['text']) for p in model.payloads if p['stage']=='evidence'))

    def test_adaptive_context_rejection_resends_every_market_without_silent_loss(self):
        ctx=context_large();model=FakeModel(limit=8192,hard_bytes=10500)
        _,diag=analyze(self.settings(),ctx,model.complete,model.request)
        self.assertEqual(diag['state'],'ready',{k:v for k,v in diag.items() if k!='evidence'})
        self.assertEqual(diag['markets_completed'],139)
        self.assertGreater(diag['errors'].get('context_limit',0),0)
        self.assertEqual(diag['source_records_completed'],diag['source_records'])

    def test_missing_tokenizer_uses_byte_bound_and_does_not_drop_data(self):
        ctx=context_large();model=FakeModel(tokenizer=False)
        _,diag=analyze(self.settings(),ctx,model.complete,model.request)
        self.assertEqual(diag['state'],'ready',{k:v for k,v in diag.items() if k!='evidence'})
        self.assertFalse(diag['tokenizer_available'])
        records=[r for p in model.payloads if p['stage']=='evidence' for r in p['records']]
        self.assertEqual(record_values(records),leaf_values(ctx))

    def test_unknown_citation_and_invented_numbers_cannot_reach_final_text(self):
        records=[{'id':'S1','path':'/team_statistics/sections/0/text','data':'Забитые мячи 21:20'}]
        for text,source,quote in [('Было 99 голов','S1','Забитые мячи 21:20'),('Факт','missing','Забитые мячи 21:20'),('Факт','S1','не существующая цитата')]:
            with self.assertRaises(ValueError):verified_observations({'observations':[{'text':text,'quotes':[{'source':source,'quote':quote}]}]},records)
        evidence=[{'id':'E1','text':'Факт','quotes':[{'quote':'21:20'}],'unverified':False}]
        with self.assertRaises(ValueError):synthesis_arguments({'arguments':[{'text':'Вероятность 99%','evidence_ids':['E1']}]},evidence)
        with self.assertRaises(ValueError):synthesis_arguments({'arguments':[{'text':'99 голов','evidence_ids':['E1']}]},evidence)
        with self.assertRaises(ValueError):verified_observations({'observations':[{'text':'99 голов','quotes':[{'source':'S1','quote':'99'}]}]},[{'id':'S1','path':'/team_statistics','data':'199 голов'}])
        with self.assertRaises(ValueError):verified_observations({'observations':[{'text':'Фора +3,5','quotes':[{'source':'S1','quote':'Фора -3.5'}]}]},[{'id':'S1','path':'/markets/0','data':'Фора -3.5'}])

    def test_news_and_unverified_rows_never_share_a_trusted_record(self):
        rows=records_for(context_large(),limit=1000000)
        for row in rows:
            if row['path'].endswith('/unverified_matches') or '/web_research' in row['path']:
                value=row['data'];quote=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,separators=(',',':'))
                parsed=verified_observations({'observations':[{'text':'Сообщение требует проверки','quotes':[{'source':row['id'],'quote':quote[:100]}]}]},[row])
                if '/unverified_matches' in row['path']:self.assertEqual(parsed,[])
                else:self.assertTrue(parsed[0]['unverified'])

    def test_connection_failure_is_not_reported_as_success_or_retried_128_times(self):
        ctx=context_large();model=FakeModel()
        def fail(*args):raise ValueError('Не удалось подключиться к Qwen.')
        _,diag=analyze(self.settings(),ctx,fail,model.request)
        self.assertEqual(diag['state'],'unavailable')
        self.assertEqual(diag['reason'],'connection')
        self.assertLessEqual(diag['model_calls'],2)
        self.assertEqual(diag['source_records_completed'],0)

    def test_one_invalid_source_part_is_explicitly_partial_even_if_final_answer_succeeds(self):
        ctx=context_large();model=FakeModel();failed=[]
        def complete(settings,messages):
            payload=json.loads(messages[1]['content'])
            if payload['stage']=='evidence' and not failed:
                failed.append(True)
                return '{"observations":[{"text":"неизвестный источник","quotes":[{"source":"missing","quote":"invented"}]}]}'
            return model.complete(settings,messages)
        arguments,diag=analyze(self.settings(),ctx,complete,model.request)
        self.assertTrue(arguments)
        self.assertEqual(diag['state'],'partial')
        self.assertLess(diag['parts_completed'],diag['parts_total'])
        self.assertLess(diag['source_records_completed'],diag['source_records'])

    def test_exhausted_deadline_does_not_start_model_calls(self):
        model=FakeModel();ctx=context_large()
        _,diag=analyze(replace(self.settings(),qwen_analysis_timeout=.001),ctx,model.complete,model.request)
        self.assertEqual(diag['state'],'unavailable')
        self.assertEqual(diag['reason'],'timeout')
        self.assertEqual(diag['model_calls'],0)


if __name__=='__main__':unittest.main()
