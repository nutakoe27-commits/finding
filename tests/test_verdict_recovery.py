"""Bounded regeneration, explicit abstention and zero-cost live handling."""
from dataclasses import replace
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.markets import validate_event_markets
from matchlab.neural_verdict import build_packet,document_quote,generate,render
from matchlab.statistics import analysis_statistics
from matchlab.storage import Repository
from account_helpers import signed_in
from test_product import event
from verdict_helpers import statistics,reply


class VerdictRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=Settings(Path(self.temp.name),'http://localhost:9/v1','fixture',event_analysis_mode='neural-verdict')
        self.repo=Repository(self.settings.database);self.repo.initialize()

    def packet(self):
        e=validate_event_markets(event());now=datetime.now(timezone.utc)
        return build_packet(self.repo,e,analysis_statistics(statistics()),now,document_quote(e,now))

    def test_third_attempt_regenerates_from_frozen_facts_without_repeating_bad_assistant_text(self):
        p=self.packet();seen=[];bad=reply(p);bad['comparison']['home']['text']+=' Забито 9999 голов.'
        def model(settings,messages):
            seen.append(messages);return json.dumps(bad if len(seen)<3 else reply(p))
        v,g=generate(self.settings,p,model,historical=True)
        self.assertEqual(v['engine'],'qwen');self.assertEqual(g['model_calls'],3)
        self.assertEqual([a['stage'] for a in g['attempts']],['initial','repair','regenerate'])
        self.assertEqual([m['role'] for m in seen[2]],['system','user','user'])
        self.assertTrue(all(messages[1]['content']==seen[0][1]['content'] for messages in seen))
        self.assertIn('Сформируй новый',json.loads(seen[2][-1]['content'])['instruction'])

    def test_repeated_bad_data_finishes_with_nobody_and_skip_but_retains_admin_error(self):
        p=self.packet();bad=reply(p);bad['comparison']['home']['text']+=' Забито 9999 голов.'
        with patch('matchlab.qwen._completion',return_value=json.dumps(bad)) as model:v,g=generate(self.settings,p,historical=True)
        self.assertEqual(model.call_count,3);self.assertEqual(g['state'],'unavailable');self.assertEqual(g['reason'],'unsupported_number')
        self.assertEqual(v['engine'],'model_error');self.assertIsNone(v['preference']);self.assertEqual(v['decision'],'skip')
        self.assertEqual(v['stake_percent'],0);self.assertIn('Претендент: Никто.',render(v));self.assertIn('Решение: Пропустить матч.',render(v))
        self.assertNotIn('9999',render(v));self.assertTrue(all('rejected_response' not in a for a in g['attempts']))

    def test_no_evidence_returns_explicit_abstention_without_calling_or_retrying(self):
        p=self.packet();p['policy']['force_skip']=True;p['limitations']=['Нет подтверждённых фактов об участниках.']
        with patch('matchlab.qwen._completion',side_effect=AssertionError('called model')):v,g=generate(self.settings,p)
        self.assertEqual(g['model_calls'],0);self.assertEqual(g['reason'],'insufficient_data')
        self.assertEqual(v['engine'],'data_guard');self.assertIn('Претендент: Никто.',render(v))

    def test_live_finished_and_other_sports_have_specific_skip_reasons(self):
        for change,reason in [('live','live_not_supported'),('finished','finished_event'),('hockey','unsupported_sport')]:
            p=self.packet()
            if change=='hockey':p['event']['sport']=change
            else:p['event']['phase']=change
            with self.subTest(change=change),patch('matchlab.qwen._completion',side_effect=AssertionError('called model')):v,g=generate(self.settings,p)
            self.assertEqual(g['reason'],reason);self.assertEqual(g['model_calls'],0);self.assertIsNone(v['preference'])

    def test_transport_failures_do_not_trigger_a_loop_of_repeated_requests(self):
        for error in ('Сервис не ответил вовремя.','Сервер Qwen вернул HTTP 401.','Не удалось подключиться к Qwen.'):
            with self.subTest(error=error),patch('matchlab.qwen._completion',side_effect=ValueError(error)) as model:
                v,g=generate(self.settings,self.packet())
            self.assertEqual(model.call_count,1);self.assertEqual(v['decision'],'skip');self.assertEqual(g['state'],'unavailable')

    def test_truncated_response_can_be_regenerated(self):
        p=self.packet()
        with patch('matchlab.qwen._completion',side_effect=[ValueError('Qwen достиг лимита токенов.'),json.dumps(reply(p))]):
            v,g=generate(self.settings,p,historical=True)
        self.assertEqual(v['engine'],'qwen');self.assertEqual(g['model_calls'],2)

    def test_neural_abstention_is_distinct_from_a_failed_generation(self):
        p=self.packet();value=reply(p,'skip',preference=None);value['reason']['code']='no_clear_preference'
        v,g=generate(self.settings,p,lambda *_:json.dumps(value),historical=True)
        self.assertEqual(v['engine'],'qwen');self.assertEqual(g['state'],'ready');self.assertIn('Претендент: Никто.',render(v))

    def test_all_three_attempts_are_in_actual_token_usage(self):
        p=self.packet();bad=reply(p);bad['comparison']['home']['text']+=' Забито 9999 голов.';calls=[]
        def provider(*args,**kwargs):
            calls.append(1)
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(bad if len(calls)<3 else reply(p))}}],
                'usage':{'prompt_tokens':1000,'completion_tokens':100,'total_tokens':1100}}
        with patch('matchlab.qwen._request',side_effect=provider):v,g=generate(self.settings,p,historical=True)
        self.assertEqual(v['engine'],'qwen');self.assertEqual(g['token_usage']['total_tokens'],3300)
        self.assertEqual(g['token_usage']['peak_prompt_tokens'],1000)

    def test_live_api_needs_neither_a_model_connection_nor_a_statistics_job(self):
        self.repo.import_event_markets(event(phase='live'))
        with TestClient(create_app(replace(self.settings,qwen_base_url='',qwen_model=''))) as client:
            signed_in(client,self.repo)
            with patch('matchlab.qwen._completion',side_effect=AssertionError('called model')):
                response=client.post('/api/bookmakers/events/123/explain',json={})
            self.assertEqual(response.status_code,200,response.text)
            v=response.json()['context']['neural_verdict'];self.assertIsNone(v['preference']);self.assertEqual(v['decision'],'skip')
            self.assertEqual(response.json()['context']['ai_generation']['reason'],'live_not_supported')
            with self.repo.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM statistics_request_jobs').fetchone()[0],0)


if __name__=='__main__':unittest.main()
