import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from matchlab.participant_model_admin import report
from account_helpers import signed_in


def fixture_report():
    return {'format':'prognozich.model-admin.v1','state':'ready','checked_at':datetime.now(timezone.utc).isoformat(),
        'model_sha256':'a'*64,'target':'participant_win.v1','training_events':1030,'training_rows':2060,'lead_minutes':60,
        'recorded_events':2,'scored_events':1,'pending_events':1,'skipped':{},'unscored_reasons':{'result_not_available_at_cutoff':1},
        'evaluation':{'overall':{'events':1,'rows':2,'brier':.065,'log_loss':.29,'calibration':[{'bin':2,'rows':1,'predicted':.2,'observed':0},{'bin':7,'rows':1,'predicted':.7,'observed':1}]},
            'sports':{'football':{'events':1,'brier':.065,'log_loss':.29}},'bookmaker_baseline':{'brier':.13}},
        'matches':[{'event':{'source_event_id':'99','sport':'football','home':'Команда А','away':'Команда Б','competition':'Тестовый турнир','kickoff_at':datetime.now(timezone.utc).isoformat()},
            'issued_at':datetime.now(timezone.utc).isoformat(),'sample_as_of':datetime.now(timezone.utc).isoformat(),'scope':'regulation','market':'Исход 1X2','state':'scored','winner':'home','reason':None,'probabilities':{'home':.7,'away':.2},'prices':{'home':1.8,'draw':3.5,'away':4.0}}]}

class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'report.json';self.value=fixture_report();self.path.write_text(json.dumps(self.value))
        self.repo=Repository(self.root/'matchlab.sqlite3');self.repo.initialize()
        self.client=TestClient(create_app(Settings(data_dir=self.root,participant_model_report=str(self.path))))
        self.addCleanup(self.client.close)

    def test_authentication_and_no_public_model_access(self):
        url='/api/admin/participant-model'
        self.assertEqual(self.client.get(url).status_code,401)
        signed_in(self.client,self.repo);self.assertEqual(self.client.get(url).status_code,403)
        signed_in(self.client,self.repo,username='admin',role='admin')
        self.value['secret']='must not leak';self.value['matches'][0]['features']={'target':1}
        self.value['matches'][0]['prices_as_of']='2026-11-15T17:00:00+00:00'
        self.value['feature_profile']='statistics_only.v1'
        self.value['variant_metrics']={'statistics':{'evaluated':1,'hits':1}}
        self.value['matches'][0]['variants']={'statistics':{'side':'home'},'statistics_price':{'decision':'skip'}}
        self.path.write_text(json.dumps(self.value));response=self.client.get(url)
        self.assertEqual(response.status_code,200);self.assertIn('no-store',response.headers['cache-control'])
        data=response.json();self.assertNotIn('secret',data);self.assertNotIn('features',data['matches'][0]);self.assertFalse(data['stale'])
        self.assertEqual(data['matches'][0]['probabilities'],{'home':.7,'away':.2})
        self.assertEqual(data['matches'][0]['prices_as_of'],'2026-11-15T17:00:00+00:00')
        self.assertFalse(data['quality_verified'])
        self.assertEqual(data['feature_profile'],'statistics_only.v1')
        self.assertEqual(data['matches'][0]['variants']['statistics_price']['decision'],'skip')
        self.assertEqual(data['variant_metrics']['statistics']['evaluated'],1)
        self.assertEqual(data['matches'][0]['prices'],{'home':1.8,'draw':3.5,'away':4.0})

    def test_primary_review_is_admin_only_and_does_not_expose_prompt_or_call_provider(self):
        from unittest.mock import patch
        from matchlab.participant_model_review import POLICY,identifier
        self.value.update(decision_mode='primary_llm.v1',decision_outcomes={'99':{'state':'scored','winner':'home'}})
        self.path.write_text(json.dumps(self.value));folder=self.root/'participant-model-reviews';folder.mkdir()
        (folder/(identifier(self.value,self.value['matches'][0])+'.json')).write_text(json.dumps({
            'format':POLICY,'model_sha256':self.value['model_sha256'],'event_id':'99','state':'ready',
            'decision':'bet','side':'home','odds':1.8,'reason_codes':['favorable_combination'],
            'input':{'secret':'DO_NOT_EXPOSE'},'token_usage':{'total_tokens':123}}))
        signed_in(self.client,self.repo,username='admin',role='admin')
        with patch('matchlab.participant_model_review._completion',side_effect=AssertionError('GET must not generate')):
            response=self.client.get('/api/admin/participant-model')
        self.assertEqual(response.status_code,200);self.assertNotIn('DO_NOT_EXPOSE',response.text)
        self.assertEqual(response.json()['neural_metrics']['won'],1)
        self.assertEqual(response.json()['matches'][0]['neural_review']['decision'],'bet')

    def test_missing_invalid_and_stale_report(self):
        self.assertEqual(report('')['state'],'not_configured')
        self.assertEqual(report(str(self.root/'missing'))['state'],'waiting')
        self.path.write_text('broken');self.assertEqual(report(str(self.path))['state'],'error')
        self.value['checked_at']=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat();self.path.write_text(json.dumps(self.value))
        self.assertTrue(report(str(self.path))['stale'])
        other=self.root/'link';other.symlink_to(self.path);self.assertEqual(report(str(other))['state'],'error')
