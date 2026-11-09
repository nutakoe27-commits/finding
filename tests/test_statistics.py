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

from account_helpers import signed_in

from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.statistics import FORMAT,analysis_statistics,validate_statistics
from matchlab.statistics_feed import extract_passive_feed
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


def structured_feed(observed_at):
    target={'_id':72334410,'teams':{'home':{'_id':10,'name':'Спартак М'},'away':{'_id':20,'name':'Акрон Тольятти'}}}
    docs=[{'match':target},{'team':{'_id':10,'name':'Спартак М'},'matches':[{'_id':101,'status':'finished','teams':{'home':{'_id':10}}}]},
          {'team':{'_id':20,'name':'Акрон Тольятти'},'matches':[{'_id':102,'status':'scheduled','teams':{'away':{'_id':20}}}]}]
    result=extract_passive_feed(docs,'72334410'); result['observed_at']=observed_at
    return result


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); folder=self.stack.enter_context(tempfile.TemporaryDirectory()); self.path=Path(folder)
        self.client=self.stack.enter_context(TestClient(create_app(Settings(self.path,'http://127.0.0.1:9/v1','local-test-model',bookmaker_ingest_token=KEY)),client=('127.0.0.1',1234)))
        self.repo=Repository(self.path/'matchlab.sqlite3')
        signed_in(self.client,self.repo)
        parent=event(); parent.update(home='Спартак М',away='Акрон Тольятти',statistics_url='https://winline.ru/stats/match/72334410')
        self.repo.import_event_markets(parent)
        self.headers={'X-Matchlab-Collector-Key':KEY}

    def tearDown(self): self.stack.close()

    def post(self,value): return self.client.post('/api/bookmakers/statistics/ingest',json=value,headers=self.headers)

    def test_queue_is_protected_and_returns_only_explicitly_linked_events(self):
        self.assertEqual(self.client.get('/api/bookmakers/statistics/queue').status_code,401)
        result=self.client.get('/api/bookmakers/statistics/queue',headers=self.headers).json()
        self.assertIn('statistics_feed_v1',result['capabilities'])
        self.assertEqual(result['linked_events'],1)
        self.assertEqual(result['jobs'][0]['statistics_url'],'https://winline.ru/stats/match/72334410')
        self.repo.import_event_markets(event('124'))
        self.assertEqual(len(self.client.get('/api/bookmakers/statistics/queue',headers=self.headers).json()['jobs']),1)

    def test_each_events_statistics_url_remains_independent(self):
        second=event('124'); second['statistics_url']='https://winline.ru/stats/match/72334422'
        self.repo.import_event_markets(second)
        jobs=self.client.get('/api/bookmakers/statistics/queue',headers=self.headers).json()['jobs']
        self.assertEqual({job['source_event_id']:job['statistics_url'] for job in jobs},
            {'123':'https://winline.ru/stats/match/72334410','124':'https://winline.ru/stats/match/72334422'})
        wrong=stats(event_id='124')
        self.assertEqual(self.post(wrong).status_code,422)

    def test_new_statistics_button_survives_partial_prices_without_restamping_old_prices(self):
        previous=self.repo.event_markets('123')
        partial=event(); partial['statistics_url']='https://winline.ru/stats/match/72334422'
        partial.update(home=previous['home'],away=previous['away'])
        partial['markets'][0]['outcomes'].pop()
        result=self.repo.import_event_markets(partial)
        self.assertFalse(result['updated'])
        current=self.repo.event_markets('123')
        self.assertEqual(current['markets'],previous['markets'])
        self.assertEqual(current['observed_at'],previous['observed_at'])
        self.assertEqual(current['capture_started_at'],previous['capture_started_at'])
        self.assertEqual(current['statistics_url'],'https://winline.ru/stats/match/72334422')
        self.assertEqual(self.repo.statistics_queue()['jobs'][0]['statistics_url'],current['statistics_url'])

    def test_partial_page_with_different_participants_cannot_replace_statistics_link(self):
        previous=self.repo.event_markets('123')
        partial=event(); partial['statistics_url']='https://winline.ru/stats/match/72334422'
        partial['markets'][0]['outcomes'].pop()
        self.repo.import_event_markets(partial)
        self.assertEqual(self.repo.event_markets('123')['statistics_url'],previous['statistics_url'])

    def test_partial_event_prices_can_refresh_until_a_full_snapshot_is_available(self):
        partial=event('124'); partial['markets'][0]['outcomes'].pop()
        self.repo.import_event_markets(partial)
        partial['markets'][0]['outcomes'][0]['odds']=3.7
        self.assertTrue(self.repo.import_event_markets(partial)['updated'])
        current=self.repo.event_markets('124')
        self.assertFalse(current['coverage']['complete'])
        self.assertEqual(current['markets'][0]['outcomes'][0]['odds'],3.7)

    def test_ready_statistics_preserve_exact_source_and_do_not_change_training_data(self):
        revision=self.repo.revision()
        response=self.post(stats()); self.assertEqual(response.status_code,200,response.text)
        saved=self.client.get('/api/bookmakers/events/123/statistics').json()['statistics']
        self.assertEqual(saved['match_phase'],'finished')
        self.assertEqual(saved['sections'][0]['text'],PROJECTION['sections'][0]['text'])
        self.assertEqual(saved['binding'],'observed_event_link')
        self.assertEqual(self.repo.revision(),revision)
        self.assertEqual(self.client.get('/api/bookmakers/statistics/queue',headers=self.headers).json()['jobs'],[])

    def test_structured_feed_reaches_model_only_after_match_and_team_validation(self):
        value=stats(); value['structured_feed']=structured_feed(value['observed_at'])
        response=self.post(value); self.assertEqual(response.status_code,200,response.text)
        model=analysis_statistics(self.repo.statistics('123'))
        self.assertEqual(model['structured_feed']['participants']['home'][0]['verified_matches'][0]['_id'],101)
        self.assertEqual(model['structured_feed']['participants']['home'][0]['unverified_matches'],[])
        self.assertEqual(model['structured_feed']['participants']['away'][0]['verified_matches'][0]['status'],'scheduled')
        forged=stats(); forged['structured_feed']=structured_feed(forged['observed_at']); forged['structured_feed']['match']['_id']=999
        self.assertEqual(self.post(forged).status_code,422)
        self.assertEqual(self.repo.statistics('123')['structured_feed']['match']['_id'],72334410)

    def test_structured_feed_with_future_observation_is_rejected(self):
        value=stats(); value['structured_feed']=structured_feed(value['observed_at'])
        value['structured_feed']['observed_at']=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        self.assertEqual(self.post(value).status_code,422)

    def test_unverified_rows_remain_separate_in_model_context(self):
        value=stats(); value['structured_feed']=structured_feed(value['observed_at'])
        value['structured_feed']['participants']['home'][0]['matches'].append(
            {'_id':999,'teams':{'home':{'_id':9999,'name':'Спартак М'}}})
        self.assertTrue(self.post(value).json()['updated'])
        snapshot=self.repo.statistics('123')
        self.assertEqual(len(snapshot['structured_feed']['participants']['home'][0]['matches']),2)
        item=analysis_statistics(snapshot)['structured_feed']['participants']['home'][0]
        self.assertEqual([row['_id'] for row in item['verified_matches']],[101])
        self.assertEqual([row['_id'] for row in item['unverified_matches']],[999])

    def test_fast_snapshot_never_replaces_saved_detailed_snapshot(self):
        detailed=stats(); self.assertTrue(self.post(detailed).json()['updated'])
        fast=stats(); fast['capture_kind']='fast'; fast['structured_feed']=structured_feed(fast['observed_at'])
        response=self.post(fast)
        self.assertEqual(response.json().get('reason'),'preserved_detailed')
        saved=self.repo.statistics('123')
        self.assertEqual(saved['capture_kind'],'detailed')
        self.assertEqual(saved['sections'][0]['text'],detailed['sections'][0]['text'])

    def test_fast_snapshot_without_structured_feed_is_rejected(self):
        value=stats(); value['capture_kind']='fast'
        self.assertEqual(self.post(value).status_code,422)

    def test_fast_snapshot_can_be_replaced_by_later_detailed_capture(self):
        fast=stats(); fast['capture_kind']='fast'; fast['structured_feed']=structured_feed(fast['observed_at'])
        self.assertTrue(self.post(fast).json()['updated'])
        self.assertEqual(self.repo.statistics('123')['capture_kind'],'fast')
        detailed=stats(); detailed['structured_feed']=structured_feed(detailed['observed_at'])
        self.assertTrue(self.post(detailed).json()['updated'])
        self.assertEqual(self.repo.statistics('123')['capture_kind'],'detailed')

    def test_foreign_widget_wrong_stats_id_and_access_denied_cannot_be_ingested_as_facts(self):
        for mutate in ('url','provider','body'):
            value=stats()
            if mutate=='url': value['statistics_url']='https://winline.ru/stats/match/999'
            if mutate=='provider': value['provider_url']='https://example.com/winline/ru/match/72334410'
            if mutate=='body': value['sections'][0]['text']='Access Denied: no permission'
            self.assertEqual(self.post(value).status_code,422)
        self.assertIsNone(self.repo.statistics('123'))

    def test_views_reject_invalid_types_or_sections_from_unvisited_tabs(self):
        for views, view in ((['overview'], 'statistics'), ([['overview']], 'overview'), (['overview'], [])):
            value=stats()
            value['observed_views']=views
            value['sections'][0]['view']=view
            with self.subTest(views=views, view=view):
                self.assertEqual(self.post(value).status_code,422)

    def test_source_filters_and_observation_reach_model_without_reinterpretation(self):
        value=stats()
        value['observed_views']=['overview']
        value['season_filters_complete']=False
        value['sections']=[{'title':'Статистика сезона',
                            'text':'За матч\nОбщее\n47\n%\nПобеды\n19\n%\n1.24\nЗабитые мячи\n1.25',
                            'view':'overview','filters':['За матч','Общее'],
                            'observed_at':value['observed_at']}]
        response=self.post(value)
        self.assertEqual(response.status_code,200,response.text)
        section=analysis_statistics(self.repo.statistics('123'))['sections'][0]
        self.assertEqual(section['filters'],['За матч','Общее'])
        self.assertIn('1.24\nЗабитые мячи\n1.25',section['text'])
        self.assertEqual(section['observed_at'],value['observed_at'])

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

    def test_provider_block_pauses_other_events_until_retry_window(self):
        other=event('124'); other['statistics_url']='https://winline.ru/stats/match/72334410'
        self.repo.import_event_markets(other)
        self.assertEqual(self.post(stats(state='blocked')).status_code,200)
        queue=self.client.get('/api/bookmakers/statistics/queue',headers=self.headers).json()
        self.assertEqual(queue['jobs'],[])
        self.assertIsNotNone(queue['blocked_until'])
        self.assertIsNotNone(self.client.get('/api/bookmakers/events/124/statistics').json()['source_blocked_until'])
        with patch('matchlab.storage.datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime.now(timezone.utc)+timedelta(hours=2)
            self.assertIsNone(self.repo.statistics_blocked_until())
            self.assertEqual(len(self.repo.statistics_queue()['jobs']),2)

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

    def test_ai_request_receives_separated_structured_feed(self):
        value=stats(); value['capture_kind']='fast'; value['structured_feed']=structured_feed(value['observed_at'])
        value['structured_feed']['participants']['home'][0]['matches'].append(
            {'_id':999,'teams':{'home':{'_id':9999,'name':'Спартак М'}}})
        self.assertTrue(self.post(value).json()['updated'])
        with patch('matchlab.api.qwen.explain_event',return_value='Разбор.') as completion:
            response=self.client.post('/api/bookmakers/events/123/explain',json={})
        self.assertEqual(response.status_code,200,response.text)
        stats_context=completion.call_args.args[1]['team_statistics']
        self.assertEqual(stats_context['capture_kind'],'fast')
        home=stats_context['structured_feed']['participants']['home'][0]
        self.assertEqual([row['_id'] for row in home['verified_matches']],[101])
        self.assertEqual([row['_id'] for row in home['unverified_matches']],[999])

    def test_blocked_page_is_explicit_and_is_not_an_ai_data_source(self):
        self.post(stats(state='blocked'))
        snapshot=self.repo.statistics('123')
        self.assertEqual(snapshot['state'],'blocked')
        self.assertIsNone(analysis_statistics(snapshot))

    def test_ai_context_keeps_full_source_sections_independent_of_ui_summary(self):
        self.post(stats())
        snapshot=self.repo.statistics('123')
        snapshot['header']='А'*3000
        snapshot['sections']=[{'title':f'Раздел {index}', 'text':f'Команда {index}\n'+('данные '*400)} for index in range(20)]
        context=analysis_statistics(snapshot)
        self.assertEqual(context['total_sections'],20)
        self.assertEqual(len(context['sections']),20)
        self.assertEqual(context['truncated_sections'],0)
        self.assertFalse(context['header_truncated'])
        self.assertEqual(sum(len(item['text']) for item in context['sections']),sum(len(item['text']) for item in snapshot['sections']))
        self.assertTrue(all(not item['truncated'] and item['text'] for item in context['sections']))

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
