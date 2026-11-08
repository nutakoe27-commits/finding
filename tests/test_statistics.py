from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from io import StringIO
from argparse import Namespace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess

from fastapi.testclient import TestClient

from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.statistics import FORMAT,analysis_statistics,validate_statistics
from matchlab.event_analysis import analysis_context
from matchlab.storage import Repository
from matchlab.statistics_worker import read_config, runtime_config, report
from scripts import configure_statistics_client
from test_product import event

PROJECTION=json.loads((Path(__file__).parent/'fixtures/team_statistics_projection.json').read_text())
KEY='statistics-test-collector-key-1234567890'


def stats(event_id='123',state='ok'):
    now=datetime.now(timezone.utc).isoformat()
    return {'format':FORMAT,'source_event_id':event_id,'event_url':f'https://winline.ru/stavki/sport/futbol/rossiya/rpl/{event_id}',
        'statistics_url':'https://winline.ru/stats/match/72334410','state':state,'attempted_at':now,'observed_at':now,**deepcopy(PROJECTION)}


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); folder=self.stack.enter_context(tempfile.TemporaryDirectory()); self.path=Path(folder)
        self.client=self.stack.enter_context(TestClient(create_app(Settings(self.path,'http://127.0.0.1:9/v1','local-test-model',bookmaker_ingest_token=KEY)),client=('127.0.0.1',1234)))
        self.repo=Repository(self.path/'matchlab.sqlite3')
        parent=event(); parent.update(home='Спартак М',away='Акрон Тольятти',statistics_url='https://winline.ru/stats/match/72334410')
        self.repo.import_event_markets(parent)
        self.headers={'X-Matchlab-Collector-Key':KEY}

    def tearDown(self): self.stack.close()

    def post(self,value): return self.client.post('/api/bookmakers/statistics/ingest',json=value,headers=self.headers)

    def test_queue_is_protected_and_returns_only_explicitly_linked_events(self):
        self.assertEqual(self.client.get('/api/bookmakers/statistics/queue').status_code,401)
        result=self.client.get('/api/bookmakers/statistics/queue',headers=self.headers).json()
        self.assertEqual(result['linked_events'],1)
        self.assertEqual(result['jobs'][0]['statistics_url'],'https://winline.ru/stats/match/72334410')
        self.repo.import_event_markets(event('124'))
        self.assertEqual(len(self.client.get('/api/bookmakers/statistics/queue',headers=self.headers).json()['jobs']),1)

    def test_ready_statistics_preserve_exact_source_and_do_not_change_training_data(self):
        revision=self.repo.revision()
        response=self.post(stats()); self.assertEqual(response.status_code,200,response.text)
        saved=self.client.get('/api/bookmakers/events/123/statistics').json()['statistics']
        self.assertEqual(saved['match_phase'],'finished')
        self.assertEqual(saved['sections'][0]['text'],PROJECTION['sections'][0]['text'])
        self.assertEqual(saved['binding'],'observed_event_link')
        self.assertEqual(self.repo.revision(),revision)
        self.assertEqual(self.client.get('/api/bookmakers/statistics/queue',headers=self.headers).json()['jobs'],[])

    def test_foreign_widget_wrong_stats_id_and_access_denied_cannot_be_ingested_as_facts(self):
        for mutate in ('url','provider','body'):
            value=stats()
            if mutate=='url': value['statistics_url']='https://winline.ru/stats/match/999'
            if mutate=='provider': value['provider_url']='https://example.com/winline/ru/match/72334410'
            if mutate=='body': value['sections'][0]['text']='Access Denied: no permission'
            self.assertEqual(self.post(value).status_code,422)
        self.assertIsNone(self.repo.statistics('123'))

    def test_blocked_refresh_keeps_last_good_snapshot_and_applies_backoff(self):
        self.post(stats())
        previous=self.repo.statistics('123')
        self.post(stats(state='blocked'))
        current=self.repo.statistics('123')
        self.assertEqual(current['sections'],previous['sections'])
        self.assertEqual(current['observed_at'],previous['observed_at'])
        self.assertEqual(current['latest_attempt']['state'],'blocked')
        self.assertEqual(self.client.get('/api/bookmakers/statistics/queue',headers=self.headers).json()['jobs'],[])
        with patch('matchlab.storage.datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime.now(timezone.utc)+timedelta(hours=2)
            self.assertEqual(len(self.repo.statistics_queue()['jobs']),1)

    def test_ai_uses_audited_statistics_and_cache_changes_when_data_arrive(self):
        with patch('matchlab.api.qwen.explain_event',return_value='Разбор рынка.') as completion:
            first=self.client.post('/api/bookmakers/events/123/explain',json={}).json()
        self.assertIsNone(completion.call_args.args[1]['team_statistics'])
        self.post(stats())
        with patch('matchlab.api.qwen.explain_event',return_value='Матч завершён. Ретроспективный разбор.') as completion:
            second=self.client.post('/api/bookmakers/events/123/explain',json={}).json()
        context=completion.call_args.args[1]
        self.assertEqual(context['team_statistics']['statistics_id'],'72334410')
        self.assertEqual(context['team_statistics']['match_phase'],'finished')
        self.assertIsNone(context['statistical_model'])
        guarded=analysis_context(self.repo.event_markets('123'),statistics={'probabilities':{'home':.5,'draw':.25,'away':.25}},team_statistics=context['team_statistics'])
        self.assertIsNone(guarded['statistical_model'])
        self.assertFalse(second['cached'])
        self.assertNotEqual(first['id'],second['id'])
        saved=self.client.get('/api/analyses/'+second['id']).json()
        self.assertEqual(saved['context']['team_statistics']['sections'][0]['title'],'Последние матчи- Спартак М')

    def test_blocked_page_is_explicit_and_is_not_an_ai_data_source(self):
        self.post(stats(state='blocked'))
        snapshot=self.repo.statistics('123')
        self.assertEqual(snapshot['state'],'blocked')
        self.assertIsNone(analysis_statistics(snapshot))

    def test_client_setup_copies_only_collector_key_without_printing_it(self):
        output=self.path/'client.json'
        result=subprocess.CompletedProcess([],0,json.dumps({'collector_key':KEY}).encode())
        messages=StringIO()
        with patch('sys.argv',['configure_statistics_client.py','--output',str(output)]),patch.object(configure_statistics_client.subprocess,'run',return_value=result) as ssh,redirect_stdout(messages):
            configure_statistics_client.main()
        self.assertNotIn(KEY,messages.getvalue())
        self.assertEqual(output.stat().st_mode&0o777,0o600)
        self.assertEqual(read_config(output)['collector_key'],KEY)
        self.assertEqual(ssh.call_args.args[0][0:2],['ssh','root@89.111.143.45'])
        self.assertNotIn('.env',ssh.call_args.args[0][2])

    def test_client_rejects_public_receiver_and_readable_secret_file(self):
        output=self.path/'client.json'
        output.write_text(json.dumps({'format':'matchlab.statistics-client.v1','api_url':'http://127.0.0.1:18082','collector_key':KEY}))
        output.chmod(0o644)
        with self.assertRaises(ValueError): read_config(output)
        output.chmod(0o600)
        config=json.loads(output.read_text()); config['api_url']='https://example.com'
        output.write_text(json.dumps(config))
        with self.assertRaises(ValueError): read_config(output)

    def test_server_reads_existing_environment_key_without_any_mac_config(self):
        args=Namespace(api_url='http://127.0.0.1:18081',config=None)
        with patch.dict(os.environ,BOOKMAKER_INGEST_TOKEN=KEY):
            self.assertEqual(runtime_config(args)['collector_key'],KEY)
        with patch.dict(os.environ,BOOKMAKER_INGEST_TOKEN=''):
            with self.assertRaises(ValueError): runtime_config(args)

    def test_server_status_preserves_blocked_state_without_secrets(self):
        output=self.path/'server/status.json'
        with redirect_stdout(StringIO()) as messages:
            report(Namespace(status_file=output),{'state':'blocked','statistics':{'blocked':1}})
        self.assertEqual(json.loads(output.read_text())['state'],'blocked')
        self.assertEqual(output.stat().st_mode&0o777,0o600)
        self.assertNotIn(KEY,messages.getvalue())


if __name__=='__main__': unittest.main()
