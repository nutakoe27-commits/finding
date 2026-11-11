"""Retire table tennis from new work while retaining frozen answers/results."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from matchlab import archive
from matchlab.featured_events import collection as featured
from matchlab.storage import Repository
from matchlab.today_picks import collection as picks
from matchlab.verdict_sports import capabilities,supported,PROFILES,result_score
from matchlab.neural_verdict import document_quote,build_packet,generate
from matchlab.config import Settings
from test_archive import header
from test_multisport_verdict import fixture
from browser_archive_smoke import feed
from outcome_helpers import forecast


class CatalogPoolTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name);self.repo=Repository(self.root/'matchlab.sqlite3');self.repo.initialize()
        self.now=datetime.now(timezone.utc)

    def test_stream_catalogue_counters_filters_and_featured_hide_table_tennis(self):
        a=header('101');a.update(sport_id=20,sport_label='Настольный теннис',featured_rank=0)
        b=header('102');b['featured_rank']=1
        feed(self.repo,[a,b])
        data=archive.catalog(self.repo,include_started=True,limit=None)
        self.assertEqual([e['source_event_id'] for e in data['events']],['102'])
        self.assertEqual(data['summary']['upcoming'],1)
        self.assertNotIn('table_tennis',{s['key'] for s in data['sports']})
        self.assertEqual(archive.catalog(self.repo,sport='table_tennis')['total'],0)
        self.assertEqual([e['source_event_id'] for e in featured(self.repo)['events']],['102'])
        self.assertIsNotNone(archive.document(self.repo,'101'))

    def test_legacy_catalogue_and_counters_also_hide_but_do_not_delete_table_tennis(self):
        for sport in ('tennis','table_tennis'):self.repo.import_event_markets({**fixture(sport,self.now),'source_event_id':'91001' if sport=='tennis' else '91002',
            'url':'https://winline.ru/stavki/sport/'+('tennis/91001' if sport=='tennis' else 'nastolnyj_tennis/91002')})
        data=self.repo.market_events(include_started=True)
        self.assertEqual((data['total'],data['summary']['total']),(1,1))
        self.assertNotIn('table_tennis',{s['key'] for s in data['sports']})
        self.assertEqual(self.repo.market_events(sport='table_tennis')['total'],0)
        self.assertIsNotNone(self.repo.event_markets('91002'))

    def test_no_new_table_tennis_analysis_but_historical_score_scope_remains(self):
        e=fixture('table_tennis',self.now)
        self.assertFalse(supported(e));self.assertNotIn('table_tennis',capabilities())
        self.assertIsNone(document_quote(e,self.now))
        packet=build_packet(self.repo,e,None,self.now)
        verdict,generation=generate(Settings(self.root,'http://localhost:9/v1','fixture'),packet,
            lambda *_:self.fail('disabled sport called AI'))
        self.assertEqual((verdict['decision'],generation['reason'],generation['model_calls']),('skip','unsupported_sport',0))
        self.assertEqual(result_score('3:1 (11:8 8:11 11:9 12:10)','table_tennis'),(3,1))

    def test_existing_future_table_tennis_bet_does_not_fill_daily_selection(self):
        e={'source_event_id':'101','sport':'table_tennis','home':'Альфа','away':'Бета','competition':'Турнир',
            'phase':'prematch','kickoff_at':(self.now+timedelta(hours=1)).isoformat()}
        item=forecast(self.repo,uuid4().hex,event=e,score=None,at=self.now-timedelta(seconds=1))
        c=item['context'];c['analysis_origin']='system';c.pop('notes',None)
        q=c['neural_input']['quote'];q.update(sport='table_tennis',scope=PROFILES['table_tennis'].scope);q['odds'].pop('draw')
        c['neural_verdict']['scope']=PROFILES['table_tennis'].scope
        c['neural_input']['unverified_user_notes']=''
        with self.repo.connection() as db:db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(item),item['id']))
        with patch('matchlab.today_picks.supported',return_value=True):self.assertEqual(picks(self.repo,now=self.now)['total'],1)
        self.assertEqual(picks(self.repo,now=self.now)['total'],0)
        self.assertIsNotNone(self.repo.event_analysis(analysis_id=item['id']))


if __name__=='__main__':unittest.main()
