from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from test_product import event

KEY='refresh-test-collector-key-1234567890'


class EventRefreshTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); path=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.client=self.stack.enter_context(TestClient(create_app(Settings(path,bookmaker_ingest_token=KEY)),client=('127.0.0.1',1234)))
        self.repo=Repository(path/'matchlab.sqlite3')
        match=event(); match['statistics_url']='https://winline.ru/stats/match/72334410'
        self.repo.import_event_markets(match)
        self.headers={'X-Matchlab-Collector-Key':KEY}

    def tearDown(self): self.stack.close()

    def request(self,event_id='123'):
        return self.client.post(f'/api/bookmakers/events/{event_id}/refresh',json={})

    def test_claim_finish_and_coalescing_are_bound_to_the_requested_match(self):
        response=self.request(); self.assertEqual(response.status_code,202,response.text)
        job=response.json(); self.assertEqual(job['state'],'queued')
        self.assertEqual(self.request().json()['job_id'],job['job_id'])
        self.assertEqual(self.client.post('/api/bookmakers/refresh/claim',json={}).status_code,401)
        claim=self.client.post('/api/bookmakers/refresh/claim',json={},headers=self.headers).json()['job']
        self.assertEqual(claim['job_id'],job['job_id'])
        self.assertEqual(claim['event']['url'],'https://winline.ru/stavki/sport/futbol/rossiya/rpl/123')
        self.assertEqual(claim['event']['statistics_url'],'https://winline.ru/stats/match/72334410')
        self.assertIsNone(self.client.post('/api/bookmakers/refresh/claim',json={},headers=self.headers).json()['job'])
        result={'job_id':job['job_id'],'lease_id':claim['lease_id'],'market_state':'ok','statistics_state':'blocked'}
        self.assertEqual(self.client.post('/api/bookmakers/refresh/finish',json={**result,'lease_id':'f'*32},headers=self.headers).status_code,409)
        self.assertEqual(self.client.post('/api/bookmakers/refresh/finish',json={**result,'statistics_state':'ok-ish'},headers=self.headers).status_code,422)
        self.assertEqual(self.client.post('/api/bookmakers/refresh/finish',json=result,headers=self.headers).status_code,200)
        status=self.client.get(f"/api/bookmakers/events/123/refresh/{job['job_id']}").json()
        self.assertEqual((status['state'],status['market_state'],status['statistics_state']),('finished','ok','blocked'))
        self.assertEqual(self.request().status_code,429)
        self.assertEqual(self.client.get(f"/api/bookmakers/events/124/refresh/{job['job_id']}").status_code,404)

    def test_input_origin_unknown_events_and_queue_limits(self):
        self.assertEqual(self.request('999').status_code,404)
        self.assertEqual(self.client.post('/api/bookmakers/events/123/refresh').status_code,403)
        self.assertEqual(self.client.post('/api/bookmakers/events/123/refresh',json={},headers={'sec-fetch-site':'cross-site'}).status_code,403)
        for number in range(20):
            self.repo.import_event_markets(event(str(1000+number)))
            self.assertEqual(self.request(str(1000+number)).status_code,202)
        self.assertEqual(self.request().status_code,429)

    def test_expired_lease_is_reclaimed_and_old_worker_cannot_finish(self):
        job=self.request().json()
        claim=self.client.post('/api/bookmakers/refresh/claim',json={},headers=self.headers).json()['job']
        with self.repo.connection() as db:
            db.execute('UPDATE event_refresh_jobs SET lease_until=? WHERE event_id=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(timespec='microseconds'),'123'))
        new=self.client.post('/api/bookmakers/refresh/claim',json={},headers=self.headers).json()['job']
        self.assertNotEqual(claim['lease_id'],new['lease_id'])
        body={'job_id':job['job_id'],'lease_id':claim['lease_id'],'market_state':'ok','statistics_state':'ok'}
        self.assertEqual(self.client.post('/api/bookmakers/refresh/finish',json=body,headers=self.headers).status_code,409)
        self.assertEqual(self.client.post('/api/bookmakers/refresh/finish',json={**body,'lease_id':new['lease_id']},headers=self.headers).status_code,200)

    def test_timeout_is_visible_instead_of_claiming_success(self):
        job=self.request().json()
        with patch('matchlab.storage.datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime.now(timezone.utc)+timedelta(minutes=6)
            self.assertEqual(self.repo.event_refresh('123',job['job_id'])['state'],'timeout')
            self.assertIsNone(self.repo.claim_event_refresh())


if __name__=='__main__': unittest.main()
