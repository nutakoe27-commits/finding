"""Observed source selection survives projection, ingest and public filtering."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest
from fastapi.testclient import TestClient
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.featured_events import select, collection
from matchlab.storage import Repository
from matchlab import archive
from winline.projection import event as project_source
from test_product import event
from test_archive import header
from browser_archive_smoke import feed


class FeaturedEventsTests(unittest.TestCase):
    def setUp(self): self.now = datetime.now(timezone.utc)

    def e(self, key, rank=0, **kw):
        return {'source_event_id':str(key),'home':'A','away':'B','sport':'football',
                'competition':'Лига','phase':'prematch','featured_rank':rank,
                'kickoff_at':(self.now+timedelta(hours=3)).isoformat(),**kw}

    def test_only_observed_source_selection_is_promoted(self):
        values=[self.e(1,None,competition='Лига чемпионов'), self.e(2,4)]
        self.assertEqual([e['source_event_id'] for e in select(values,now=self.now)],['2'])

    def test_source_order_wins_over_sport_proximity_freshness_and_league(self):
        values=[self.e(1,3,competition='Лига чемпионов'),self.e(2,1,sport='hockey',is_stale=True),
                self.e(3,0,kickoff_at=(self.now+timedelta(days=3)).isoformat())]
        self.assertEqual([e['source_event_id'] for e in select(values,now=self.now)],['3','2','1'])

    def test_deduplicates_and_bounds_without_diversity_reordering(self):
        values=[self.e(i,i) for i in range(1,10)]
        selected=select(values+[values[0]],limit=5,now=self.now)
        self.assertEqual([e['source_event_id'] for e in selected],['1','2','3','4','5'])

    def test_finished_unknown_past_outright_and_invalid_ranks_are_excluded(self):
        values=[self.e(1),self.e(2,phase='finished'),self.e(3,kickoff_at=(self.now-timedelta(hours=1)).isoformat()),
                self.e(4,event_kind='outright'),self.e(5,home=None),self.e('bad/id'),
                self.e(6,True),self.e(7,-1),self.e(8,1000),self.e(9,'1')]
        self.assertEqual([e['source_event_id'] for e in select(values,now=self.now)],['1'])

    def test_legacy_catalogue_is_not_an_editorial_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Path(folder)/'test.sqlite3');repo.initialize();repo.import_event_markets(event())
            self.assertEqual(collection(repo)['events'],[])

    def test_public_api_returns_mixed_sports_in_exact_source_order(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(Path(folder));repo=Repository(settings.database);repo.initialize()
            football=header('101');football['featured_rank']=2
            hockey=header('102',live=True,score='2:1');hockey.update(sport_id=4,sport_label='Хоккей',featured_rank=0)
            feed(repo,[football,hockey,header('103')])
            with TestClient(create_app(settings)) as client:
                data=client.get('/api/bookmakers/featured-events?limit=100').json()
                self.assertEqual(data['selection'],'winline_homepage')
                self.assertEqual([e['source_event_id'] for e in data['events']],['102','101'])
                self.assertEqual(data['events'][0]['live_score'],'2:1')
                self.assertEqual([e['source_event_id'] for e in client.get('/api/bookmakers/featured-events?sport=football&phase=upcoming').json()['events']],['101'])
                self.assertEqual(client.get('/api/bookmakers/featured-events?phase=finished').status_code,422)
                self.assertEqual(client.get('/api/bookmakers/featured-events?limit=1000').status_code,422)

    def test_latest_snapshot_removes_old_featured_membership(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Path(folder)/'test.sqlite3');repo.initialize()
            first=header('101');first['featured_rank']=0;feed(repo,[first])
            self.assertEqual(len(collection(repo)['events']),1)
            feed(repo,[header('101')]);self.assertEqual(collection(repo)['events'],[])

    def test_invalid_transport_rank_is_rejected(self):
        for rank in (True,-1,1000,'2',1.5):
            with self.subTest(rank=rank):
                value=header();value['featured_rank']=rank
                with self.assertRaises(ValueError):archive.validate_event(value)

    def test_raw_snapshot_rank_survives_projection(self):
        row={'raw_json':json.dumps({'members':['A','B'],'_featured_rank':7}),
             'last_seen':self.now.isoformat(),'id':101,'radar_id':None,'sport_id':1,'country_id':1,
             'championship_id':1,'start_at':(self.now+timedelta(days=1)).isoformat(),
             'is_live':False,'score':None,'clock':None,'period_scores':None}
        value=project_source(row,{1:'Футбол'},{1:'РФ'},{1:'Лига'})
        self.assertEqual(value['featured_rank'],7)
        archive.validate_event(value)
        self.assertEqual(archive.project(value)['featured_rank'],7)
        del row['raw_json'];row['raw_json']='{"members":["A","B"]}'
        self.assertIsNone(project_source(row,{1:'Футбол'},{},{})['featured_rank'])

if __name__=='__main__':unittest.main()
