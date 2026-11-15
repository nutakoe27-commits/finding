import copy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
from threading import RLock
import unittest
from unittest.mock import Mock, patch

from matchlab.config import Settings
from matchlab import participant_model_review as m
from matchlab.token_usage import begin_call


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'report.json'
        self.at=datetime(2027,1,1,12,tzinfo=timezone.utc)
        clock=patch.object(m,'now',return_value=self.at);self.clock=clock.start();self.addCleanup(clock.stop)
        self.settings=Settings(data_dir=self.root,participant_model_report=str(self.path),qwen_base_url='http://unused/v1',qwen_model='site-primary-model')
        self.item={'event':{'source_event_id':'1','sport':'football','home':'A','away':'B','competition':'Test','kickoff_at':(self.at+timedelta(hours=3)).isoformat()},
            'state':'pending','winner':None,'sample_as_of':(self.at-timedelta(minutes=1)).isoformat(),'issued_at':self.at.isoformat(),
            'scope':'regulation','market':'Исход 1X2','prices':{'home':2.,'draw':3.,'away':4.},'probabilities':{'home':.65,'away':.2},
            'statistics_features':{s:{**{k:None for k in m.STAT_FIELDS},'selected_form_count':10,'opponent_form_count':10,
                'selection_side':s,'sport':'football','market_scope':'regulation','target':'MUST_NOT_LEAK'} for s in ('home','away')}}
        self.data={'format':'prognozich.model-admin.v1','state':'ready','feature_profile':'statistics_only.v1','decision_mode':m.MODE,
            'model_sha256':'a'*64,'checked_at':self.at.isoformat(),'lead_minutes':60,'matches':[self.item],
            'decision_outcomes':{'1':{'state':'pending','winner':None}},'secret':'MUST_NOT_LEAK'}
        self.write()
        self.call=Mock(side_effect=self.valid)
        self.reviewer=m.Reviewer(self.settings,RLock(),self.call)

    def write(self):self.path.write_text(json.dumps(self.data))

    def valid(self,*args):
        begin_call(400).finish({'prompt_tokens':200,'completion_tokens':30,'total_tokens':230},'stop')
        return json.dumps({'decision':'bet','side':'home','reason_codes':['favorable_combination','uncalibrated_model']})

    def record(self):return json.loads(next(m.store(self.settings).glob('*.json')).read_text())

    def view(self):return m.attach_reviews(copy.deepcopy(self.data),self.data,self.settings)

    def test_once_only_same_provider_frozen_probabilities_no_result_leak_and_scoring(self):
        self.assertTrue(self.reviewer.run_one());self.assertEqual(self.call.call_count,1)
        opts,messages=self.call.call_args.args;self.assertEqual(opts.qwen_model,self.settings.qwen_model)
        packet=json.loads(messages[1]['content'])
        self.assertEqual(packet['probabilities'],{'home':.65,'away':.2})
        self.assertNotIn('MUST_NOT_LEAK',json.dumps(packet));self.assertNotIn('winner',packet)
        self.assertNotIn('decision_outcomes',packet);self.assertNotIn('state',packet)
        self.reviewer.next_check=0;self.assertFalse(self.reviewer.run_one());self.assertEqual(self.call.call_count,1)
        record=self.record();self.assertEqual(record['token_usage']['total_tokens'],230)
        self.assertEqual(self.view()['neural_metrics']['pending'],1)
        self.data['decision_outcomes']['1']={'state':'scored','winner':'home'}
        view=self.view();self.assertEqual(view['neural_metrics']['won'],1);self.assertEqual(view['neural_metrics']['roi'],1.)
        self.assertNotIn('input',view['matches'][0]['neural_review'])
        self.data['decision_outcomes']['1']['winner']='draw';self.assertEqual(self.view()['neural_metrics']['lost'],1)
        self.assertEqual(self.view()['neural_metrics']['roi'],-1.)
        self.assertEqual(self.record(),record)

    def test_llm_not_formula_controls_decision_and_failures_are_not_skips(self):
        # No hard-coded p>=55% or price gate: model estimates are immutable inputs.
        self.item['probabilities']['home']=.51;self.item['prices']['home']=1.1;self.write()
        self.reviewer.run_one();self.assertEqual(self.record()['decision'],'bet')
        self.assertEqual(self.record()['input']['probabilities']['home'],.51)
        # A different forecast gets an invalid response twice, never a synthetic skip.
        self.item['event']['source_event_id']='2';self.write();self.reviewer.next_check=0
        self.call.side_effect=lambda *_:'{"decision":"bet","side":"away","reason_codes":["invented"],"probability":0.99}'
        self.reviewer.run_one();view=self.view()
        self.assertEqual(view['neural_metrics']['ready'],1);self.assertEqual(view['neural_metrics']['errors'],1)
        self.assertEqual(view['neural_metrics']['skipped'],0);self.assertEqual(self.call.call_count,3)

    def test_skips_do_not_count_as_wins_and_old_models_stale_or_finished_never_call(self):
        self.call.side_effect=lambda *_:'{"decision":"skip","side":null,"reason_codes":["uncalibrated_model"]}'
        self.reviewer.run_one();self.data['decision_outcomes']['1']={'state':'scored','winner':'away'}
        v=self.view()['neural_metrics'];self.assertEqual(v['skipped'],1);self.assertEqual(v['settled'],0);self.assertIsNone(v['accuracy'])
        self.item['event']['source_event_id']='new';original=copy.deepcopy(self.data)
        for mutate in (lambda:self.data.update(decision_mode='price_gate.v1'),
                       lambda:self.data.update(checked_at=(self.at-timedelta(minutes=6)).isoformat()),
                       lambda:self.item.update(state='scored',winner='home'),
                       lambda:self.item.update(sample_as_of=(self.at-timedelta(minutes=17)).isoformat(),issued_at=(self.at-timedelta(minutes=16)).isoformat()),
                       lambda:self.item['event'].update(kickoff_at=(self.at+timedelta(minutes=59)).isoformat())):
            self.data=copy.deepcopy(original);self.item=self.data['matches'][0];mutate();self.write();self.reviewer.next_check=0
            self.assertFalse(self.reviewer.run_one())
        self.assertEqual(self.call.call_count,1)

    def test_late_response_is_not_a_prediction_and_restart_does_not_retry(self):
        def late(*args):
            self.clock.return_value=self.at+timedelta(hours=3)
            return self.valid(*args)
        self.call.side_effect=late;self.reviewer.run_one()
        self.assertEqual(self.record()['state'],'expired');self.assertIsNone(self.record()['decision'])
        self.assertEqual(self.view()['neural_metrics']['errors'],1)
        self.clock.return_value=self.at
        m.Reviewer(self.settings,RLock(),self.call).run_one();self.assertEqual(self.call.call_count,1)

    def test_get_is_read_only_with_no_network_and_outcomes_outside_latest_100_are_scored(self):
        self.reviewer.run_one();self.data['matches']=[]
        self.data['decision_outcomes']['1']={'state':'scored','winner':'home'}
        self.assertEqual(self.view()['neural_metrics']['won'],1)
        self.assertEqual(self.call.call_count,1)
        self.data['model_sha256']='b'*64
        self.assertEqual(self.view()['neural_metrics']['ready'],0)

    def test_shared_lock_prevents_duplicate_calls_and_bad_schema_is_rejected(self):
        import fcntl
        root=m.store(self.settings);root.mkdir()
        with (root/'review.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertFalse(self.reviewer.run_one());self.call.assert_not_called()
        for value in ({'decision':'skip','side':'home','reason_codes':['weak_preference']},
                      {'decision':'bet','side':'home','reason_codes':['weak_preference']},
                      {'decision':'skip','side':None,'reason_codes':['favorable_combination']},
                      {'decision':'skip','side':None,'reason_codes':[]}):
            with self.assertRaises(ValueError):m.validate_reply(json.dumps(value))

    def test_idle_callback_runs_only_on_system_lane_after_public_queue_is_empty(self):
        from matchlab.analysis_jobs import AnalysisJobs
        for lane in ('human','system'):
            jobs=AnalysisJobs(None,self.settings,Mock())
            def idle():jobs.stopping.set()
            jobs.idle_system=Mock(side_effect=idle)
            def run_one(which):
                if lane=='human':jobs.stopping.set()
                return False
            jobs.run_one=Mock(side_effect=run_one)
            jobs.run(lane)
            self.assertEqual(jobs.idle_system.call_count,int(lane=='system'))
