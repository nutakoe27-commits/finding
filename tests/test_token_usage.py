"""Provider counts, repairs, failures, cache isolation and administrator access."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from matchlab.token_usage import capture_usage,begin_call
from matchlab.token_analytics import run
from matchlab.qwen import _completion
from matchlab.neural_verdict import build_packet,document_quote,generate
from matchlab.statistics import analysis_statistics
from matchlab.markets import validate_event_markets
from account_helpers import signed_in
from test_product import event
from verdict_helpers import statistics,reply


def response(text='Ответ',prompt=1000,output=100,finish='stop'):
    return {'choices':[{'finish_reason':finish,'message':{'role':'assistant','content':text}}],
        'usage':{'prompt_tokens':prompt,'completion_tokens':output,'total_tokens':prompt+output}}


class TokenUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=Settings(Path(self.temp.name),'http://localhost:9/v1','fixture',event_analysis_mode='neural-verdict')
        self.repo=Repository(self.settings.database);self.repo.initialize()

    def packet(self):
        value=event();value['statistics_url']='https://winline.ru/stats/match/72334410'
        value=validate_event_markets(value);at=datetime.now(timezone.utc)
        return build_packet(self.repo,value,analysis_statistics(statistics()),at,document_quote(value,at))

    def test_reported_counts_include_output_limit_failures_without_retaining_prompts(self):
        with capture_usage() as capture,patch('matchlab.qwen._request',return_value=response(finish='length')):
            with self.assertRaises(ValueError):_completion(self.settings,[{'role':'user','content':'PRIVATE_PROMPT'}])
        u=capture.summary();self.assertEqual((u['prompt_tokens'],u['completion_tokens'],u['total_tokens']),(1000,100,1100))
        self.assertEqual(u['peak_context_tokens'],2024);self.assertTrue(u['complete'])
        self.assertNotIn('PRIVATE_PROMPT',json.dumps(u))

    def test_timeout_and_missing_or_invalid_usage_remain_unknown_not_zero(self):
        with capture_usage() as capture,patch('matchlab.qwen._request',side_effect=ValueError('timeout')):
            with self.assertRaises(ValueError):_completion(self.settings,[])
        self.assertEqual(capture.summary()['unreported_calls'],1);self.assertIsNone(capture.summary()['total_tokens'])
        for usage in ({},{'prompt_tokens':-1,'completion_tokens':10},{'prompt_tokens':True,'completion_tokens':10},
                      {'prompt_tokens':100,'completion_tokens':10,'total_tokens':900}):
            body=response();body['usage']=usage
            with self.subTest(usage=usage),capture_usage() as capture,patch('matchlab.qwen._request',return_value=body):_completion(self.settings,[])
            self.assertIsNone(capture.summary()['total_tokens'])

    def test_nested_and_parallel_captures_do_not_double_count_or_mix_requests(self):
        def call(n):
            c=begin_call(100);c.finish({'prompt_tokens':n,'completion_tokens':10},'stop')
        with capture_usage() as outer:
            with capture_usage() as inner:call(1000)
            with ThreadPoolExecutor(2) as pool:
                futures=[pool.submit(copy_context().run,call,n) for n in (2000,3000)]
                for f in futures:f.result()
        self.assertEqual(inner.summary()['total_tokens'],1010)
        self.assertEqual((outer.summary()['model_calls'],outer.summary()['total_tokens']),(3,6030))
        with capture_usage() as next_request:self.assertEqual(next_request.summary()['total_tokens'],0)

    def test_repair_sums_both_calls_but_context_peak_is_only_the_largest_call(self):
        packet=self.packet();calls=[]
        def provider(*args,**kwargs):
            v=reply(packet)
            if not calls:v['comparison']['home']['text']+=' Забито 9999 голов.'
            calls.append(1);return response(json.dumps(v),prompt=len(calls)*1000,output=100)
        with patch('matchlab.qwen._request',side_effect=provider):verdict,g=generate(self.settings,packet,historical=True)
        self.assertEqual(verdict['engine'],'qwen');u=g['token_usage']
        self.assertEqual((u['model_calls'],u['total_tokens'],u['peak_prompt_tokens'],u['peak_context_tokens']),(2,3200,2000,3024))
        self.assertEqual(g['attempts'][0]['token_usage']['total_tokens'],1100)

    def test_legacy_parallel_evidence_pipeline_keeps_the_request_capture(self):
        from dataclasses import replace
        from matchlab.event_reasoning import analyze
        from test_event_reasoning import FakeModel,context_large
        model=FakeModel()
        def complete(settings,messages):
            c=begin_call(settings.qwen_max_tokens)
            result=model.complete(settings,messages)
            c.finish({'prompt_tokens':100,'completion_tokens':20},'stop')
            return result
        with capture_usage() as capture:
            _,g=analyze(replace(self.settings,qwen_parallelism=2),context_large(),complete,model.request)
        self.assertGreater(capture.summary()['model_calls'],1)
        self.assertEqual(capture.summary()['model_calls'],g['model_calls'])

    def test_direct_api_persists_usage_and_cache_hit_does_not_reuse_original_cost(self):
        value=event();value['statistics_url']='https://winline.ru/stats/match/72334410'
        self.repo.import_event_markets(value);self.repo.import_statistics(statistics())
        def provider(*args,**kwargs):
            p=json.loads(kwargs['json']['messages'][1]['content']);return response(json.dumps(reply(p)))
        with TestClient(create_app(self.settings)) as client,patch('matchlab.qwen._request',side_effect=provider):
            signed_in(client,self.repo)
            first=client.post('/api/bookmakers/events/123/explain',json={});self.assertEqual(first.status_code,200,first.text)
            self.assertEqual(first.json()['context']['ai_generation']['token_usage']['total_tokens'],1100)
            second=client.post('/api/bookmakers/events/123/explain',json={});self.assertTrue(second.json()['cached'])
            self.assertEqual(client.get('/api/admin/ai-token-usage').status_code,403)
            signed_in(client,self.repo,username='admin_tokens',role='admin')
            result=client.get('/api/admin/ai-token-usage');self.assertEqual(result.status_code,200,result.text)
            d=result.json();self.assertEqual(d['metrics']['total_tokens'],1100);self.assertEqual(d['metrics']['cached_requests'],1)
            self.assertEqual(d['requests'][0]['token_usage']['total_tokens'],0)

    def test_background_jobs_persist_actual_counts_including_cached_and_failed_requests(self):
        from matchlab.analysis_jobs import AnalysisJobs
        from matchlab import accounts
        uid=accounts.create_user(self.repo,'job_user','tokens-fixture-password-123',agreements=True)
        with self.repo.connection() as db:user=dict(db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone())
        payload={'id':'analysis1','analysis_type':'neural_verdict','context':{'ai_generation':{'state':'ready','model_calls':1}},'cached':False}
        def execute(job,snapshot):
            _completion(self.settings,[])
            return payload
        jobs=AnalysisJobs(self.repo,self.settings,execute)
        with patch('matchlab.qwen._request',return_value=response()):
            jobs.enqueue(user,'123','bookmakers','first',{});self.assertTrue(jobs.run_one())
        jobs.execute=lambda job,snapshot:{**payload,'cached':True}
        jobs.enqueue(user,'123','bookmakers','second',{});self.assertTrue(jobs.run_one())
        d=run(self.repo);self.assertEqual(d['metrics']['total_tokens'],1100);self.assertEqual(d['metrics']['cached_requests'],1)

    def test_partial_requests_contribute_known_counts_but_not_complete_request_percentiles(self):
        with capture_usage() as c:
            a=begin_call(200);a.finish({'prompt_tokens':10000,'completion_tokens':500},'stop');begin_call(200)
        self.insert('partial',{'model_calls':2,'token_usage':c.summary()})
        self.insert('old',None)
        self.insert('cached',{'cache_hit':True,'model_calls':0})
        d=run(self.repo);m=d['metrics']
        self.assertEqual(m['total_tokens'],10500);self.assertEqual(m['partial_requests'],1);self.assertEqual(m['missing_requests'],1)
        self.assertIsNone(m['request_tokens_p50']);self.assertEqual(sum(b['calls'] for b in d['histogram']),1)

    def insert(self,key,details):
        with self.repo.connection() as db:
            db.execute('INSERT INTO ai_usage(id,user_id,at,kind,target,status,details) VALUES (?,?,?,?,?,?,?)',
                (key,'owner',datetime.now(timezone.utc).isoformat(),'event','/api/bookmakers/events/123/explain','success',json.dumps(details) if details else None))

    def test_token_endpoint_requires_admin_even_for_empty_reports(self):
        with TestClient(create_app(self.settings)) as client:
            self.assertEqual(client.get('/api/admin/ai-token-usage').status_code,401)
            signed_in(client,self.repo,username='admin_usage',role='admin')
            self.assertEqual(client.get('/api/admin/ai-token-usage?days=999').status_code,422)
            self.assertEqual(client.get('/api/admin/ai-token-usage?kind=other').status_code,422)


if __name__=='__main__':unittest.main()
