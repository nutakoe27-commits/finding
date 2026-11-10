"""Real catalogue selection, truthful previews and bounded public carousel API."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.featured_events import select,collection
from matchlab.storage import Repository
from test_product import event


class FeaturedEventsTests(unittest.TestCase):
    def setUp(self):self.now=datetime.now(timezone.utc)

    def e(self,key,**kw):
        return {'source_event_id':str(key),'home':'A','away':'B','sport':'football','competition':'Лига '+str(key),
            'phase':'prematch','kickoff_at':(self.now+timedelta(hours=3)).isoformat(),'is_stale':False,**kw}

    def test_finished_unknown_and_past_events_are_not_promoted(self):
        values=[self.e(1),self.e(2,phase='finished'),self.e(3,kickoff_at=(self.now-timedelta(hours=1)).isoformat()),
            self.e(4,event_kind='outright'),self.e(5,home=None),self.e('bad/id')]
        self.assertEqual([e['source_event_id'] for e in select(values,now=self.now)],['1'])

    def test_fresh_nearby_competitions_rank_ahead_of_stale_or_distant_events(self):
        values=[self.e(1,is_stale=True),self.e(2,kickoff_at=(self.now+timedelta(days=3)).isoformat()),
            self.e(3),self.e(4,competition='Лига чемпионов')]
        self.assertEqual([e['source_event_id'] for e in select(values,now=self.now)],['4','3','2','1'])

    def test_diversity_deduplication_and_bound_do_not_invent_events(self):
        values=[self.e(i,competition='Один турнир') for i in range(1,10)]+[self.e(20),self.e(21)]
        selected=select(values+[values[0]],limit=5,now=self.now)
        self.assertEqual(len(selected),5);self.assertEqual(len({e['source_event_id'] for e in selected}),5)
        self.assertIn('20',[e['source_event_id'] for e in selected[:4]])

    def test_public_api_preserves_live_scores_and_filters_by_sport_and_phase(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(Path(folder));repo=Repository(settings.database);repo.initialize()
            repo.import_event_markets(event('123'));live=event('124',phase='live');live.update(live_score='2:1',live_clock="2Т 65'")
            repo.import_event_markets(live)
            with TestClient(create_app(settings)) as client:
                response=client.get('/api/bookmakers/featured-events?phase=live&sport=football')
                self.assertEqual(response.status_code,200,response.text);items=response.json()['events']
                self.assertEqual([e['source_event_id'] for e in items],['124']);self.assertEqual(items[0]['phase'],'live')
                self.assertEqual(client.get('/api/bookmakers/featured-events?limit=100').status_code,422)
                self.assertEqual(client.get('/api/bookmakers/featured-events?phase=finished').status_code,422)
                self.assertEqual(client.get('/api/bookmakers/featured-events?sport=hockey').json()['events'],[])

    def test_stream_source_preserves_the_actual_live_score(self):
        from browser_archive_smoke import feed
        from test_archive import header
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Path(folder)/'matchlab.sqlite3');repo.initialize()
            feed(repo,[header('101',live=True,score='2:1')])
            data=collection(repo,sport='football',phase='live')
            self.assertEqual(data['source_kind'],'stream')
            self.assertEqual(data['events'][0]['live_score'],'2:1')

    def test_old_legacy_observations_are_not_labeled_fresh(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Path(folder)/'matchlab.sqlite3');repo.initialize()
            value=event();value['observed_at']=(self.now-timedelta(minutes=10)).isoformat();repo.import_event_markets(value)
            data=collection(repo,sport='football')
            self.assertTrue(data['events'][0]['is_stale'])
            self.assertEqual(data['events'][0]['source_kind'],'legacy')


if __name__=='__main__':unittest.main()
