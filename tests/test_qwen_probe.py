"""Read-only production diagnostics must not print keys, prompts or model content."""
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from matchlab.config import Settings
from matchlab.storage import Repository
from matchlab.qwen_probe import check_load,inspect_context


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.settings=Settings(Path(self.tmp.name),'http://localhost:9/v1','fixture',qwen_api_key='private-key',qwen_enable_thinking=False)
        self.repo=Repository(self.settings.database);self.repo.initialize()
        self.repo.save_event_analysis({'id':'probe','generated_at':datetime.now(timezone.utc).isoformat(),
            'context':{'event':{'home':'A','away':'B'},'markets':[],'team_statistics':{'text':'private source text'},
                       'ai_generation':{'evidence':['previous runtime notes']}}},'probe-key')

    def test_context_measurement_does_not_generate_or_expose_source(self):
        calls=[]
        def request(settings,method,path,**kwargs):
            calls.append((method,path,kwargs))
            if path=='/models':return {'data':[{'id':'fixture','max_model_len':32768}]}
            if path=='/tokenize':return {'count':60000 if json.loads(kwargs['json']['messages'][1]['content']).get('stage')=='evidence' else 50000}
            self.fail('Unexpected model generation')
        with patch('matchlab.qwen._request',side_effect=request):report=inspect_context(self.settings)
        self.assertEqual(report['raw_context']['minimum_context_tokens'],50768)
        self.assertEqual(report['single_evidence_request']['minimum_context_tokens'],60768)
        self.assertEqual(report['single_evidence_request']['reserved_output_tokens'],512)
        self.assertFalse(report['single_evidence_request']['fits_advertised_window'])
        self.assertNotIn('private',json.dumps(report))
        text=calls[-1][2]['json']['messages'][1]['content']
        self.assertIn('private source text',text)
        self.assertNotIn('previous runtime notes',text)
        self.assertEqual(json.loads(text)['stage'],'evidence')

    def test_probe_times_two_sequential_requests_without_printing_response_or_credentials(self):
        calls=[]
        def request(settings,method,path,**kwargs):
            calls.append(path)
            if path=='/models':return {'data':[{'id':'fixture','max_model_len':32768}]}
            if path=='/tokenize':return {'count':500}
            return {'usage':{'prompt_tokens':500,'completion_tokens':8},'choices':[{'message':{'content':'private model reply','reasoning_content':'private thoughts'},'finish_reason':'stop'}]}
        with patch('matchlab.qwen._request',side_effect=request):report=check_load(self.settings)
        self.assertEqual([p['stage'] for p in report['probes']],['small','match_sample'])
        self.assertEqual(calls.count('/chat/completions'),2)
        self.assertNotIn('private',json.dumps(report))
        self.assertTrue(all(p['elapsed_ms']>=0 for p in report['probes']))

    def test_failed_small_probe_does_not_add_load_with_a_large_request(self):
        with patch('matchlab.qwen._request',side_effect=ValueError('Не удалось подключиться к Qwen. private-key')) as request:
            report=check_load(self.settings)
        self.assertEqual(request.call_count,1)
        self.assertEqual(report['probes'][0]['reason'],'connection')
        self.assertNotIn('private-key',json.dumps(report))

    def test_model_error_with_http_200_stops_probe_before_larger_request(self):
        for reply in ({'error': {'message': 'EngineDeadError private-key'}},
                      {'object': 'error', 'message': 'EngineDeadError private-key', 'code': 500},
                      {'choices': [], 'usage': {}}, {'choices': [{'text': 'private legacy completion'}]}):
            with self.subTest(shape=tuple(reply)), patch('matchlab.qwen._request',return_value=reply) as request:
                report=check_load(self.settings)
            self.assertEqual(request.call_count,1)
            self.assertEqual(report['probes'][0]['state'],'failed')
            self.assertNotIn('private',json.dumps(report))


if __name__=='__main__':unittest.main()
