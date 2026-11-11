"""Future system forecasts remain visible to admins without fake settled results."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from matchlab.storage import Repository
from matchlab.verdict_outcomes import run
from matchlab.public_results import report
from outcome_helpers import forecast


class SystemVisibilityTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.repo=Repository(Path(temp.name)/'test.sqlite3');self.repo.initialize();self.now=datetime.now(timezone.utc)

    def system(self,**kw):
        item=forecast(self.repo,uuid4().hex,**kw);item['context']['analysis_origin']='system'
        with self.repo.connection() as db:db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(item),item['id']))
        return item

    def test_future_system_forecast_is_pending_in_admin_and_not_published_or_scored_early(self):
        item=self.system(kickoff=self.now+timedelta(days=3),at=self.now-timedelta(seconds=10),score=None)
        data=run(self.repo,now=self.now,origin='system')
        self.assertEqual(data['total'],1);self.assertEqual(data['matches'][0]['id'],item['id'])
        self.assertEqual(data['matches'][0]['pending_reason'],'fixture_not_started')
        self.assertEqual((data['awaiting_start'],data['metrics']['pending'],data['metrics']['settled']),(1,1,0))
        self.assertIsNone(data['metrics']['bet_accuracy'])
        self.assertEqual(report(self.repo)['metrics']['decisions'],0)

    def test_human_forecast_does_not_hide_the_system_forecast_for_the_same_fixture(self):
        first=forecast(self.repo,uuid4().hex,score='2:0')
        self.system(event=first['event'],at=datetime.fromisoformat(first['generated_at'])+timedelta(seconds=10),score=None)
        self.assertEqual(run(self.repo,now=self.now)['total'],2)
        self.assertEqual(run(self.repo,now=self.now,origin='system')['matches'][0]['origin'],'system')
        self.assertEqual(run(self.repo,now=self.now,origin='human')['total'],1)
        # The public identical-line rule still prevents double counting.
        self.assertEqual(report(self.repo)['metrics']['decisions'],1)

    def test_system_no_data_is_visible_in_activity_but_not_promoted_to_a_forecast(self):
        self.system(decision='skip',preference=None,engine='data_guard',score=None)
        data=run(self.repo,now=self.now,origin='system')
        self.assertEqual(data['metrics']['fixtures'],0)
        self.assertEqual(data['system_activity'],{'recorded':1,'ready':0,'without_forecast':1})
        self.assertEqual(report(self.repo)['metrics']['decisions'],0)
