"""Prospective isolation, no target leakage, stable features and late outcome scoring."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('participant_worker',ROOT/'deploy/participant_model/worker.py')
w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
ev,tr=w.load_source()

class Clock(datetime):
    value=datetime(2027,1,1,12,tzinfo=timezone.utc)
    @classmethod
    def now(cls,tz=None):return cls.value

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.bundle=self.root/'bundle';self.state=self.root/'state';self.state.mkdir()
        (self.bundle/'model').mkdir(parents=True);(self.bundle/'dataset').mkdir()
        self.db=self.root/'dataset.sqlite3';self.token=self.root/'token';self.token.write_text('secret')
        self.now=datetime(2027,1,1,12,tzinfo=timezone.utc);Clock.value=self.now
        self.clock=patch.object(w,'datetime',Clock);self.clock.start();self.addCleanup(self.clock.stop)
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE metadata (key TEXT,value TEXT)');db.execute("INSERT INTO metadata VALUES ('owner','doscbot.fresh-collector.v1')")
            db.execute('CREATE TABLE samples (id INTEGER PRIMARY KEY,event_id TEXT,as_of TEXT,kickoff_at TEXT,sha256 TEXT,payload TEXT)')
            db.execute('CREATE TABLE observations (id INTEGER PRIMARY KEY,kind TEXT,entity_id TEXT,observed_at TEXT,received_at TEXT,sha256 TEXT,payload TEXT)')
        self.old=self.sample('1','old',self.now-timedelta(days=2));self.new=self.sample('99','new',self.now)
        self.save_sample(self.old);self.save_sample(self.new);self.native(self.new)
        rows=[];market,_=tr.winner_market(self.old['event'])
        for side,features in tr.participant_rows(self.old,market):
            rows.append({'format':'doscbot.participant-training-row.v1','event_group':'1','sample_sha256':w.digest(self.old),
                'partition':'unassigned','as_of':self.old['as_of'],'kickoff_at':self.old['event']['kickoff_at'],
                'target':int(side=='home'),'features':features,'result':{'received_at':(self.now-timedelta(days=1,hours=12)).isoformat()}})
        raw=b''.join(w.canonical(r)+b'\n' for r in rows);sha=hashlib.sha256(raw).hexdigest()
        (self.bundle/'dataset/rows.jsonl').write_bytes(raw)
        (self.bundle/'dataset/schema.json').write_bytes(w.canonical(tr.SCHEMA))
        (self.bundle/'model/feature-schema.json').write_bytes(w.canonical(tr.SCHEMA))
        (self.bundle/'dataset/manifest.json').write_bytes(w.canonical({'schema_sha256':w.digest(tr.SCHEMA),'checked_at':(self.now-timedelta(days=1)).isoformat(),
            'split_required':True,'lead_minutes':60,'export_rows':2,'rows_sha256':sha,'requested_sports':['football']}))
        (self.bundle/'model/model.cbm').write_bytes(b'fixture-not-a-real-model')
        code_hash=w.digest({n:hashlib.sha256((Path(tr.__file__).parent/(n+'.py')).read_bytes()).hexdigest() for n in ('training','training_evidence','training_diagnostics','profiles','store','outcomes','monitor_data')})
        (self.bundle/'model/metrics.json').write_bytes(w.canonical({'target':'participant_win.v1','schema_sha256':w.digest(tr.SCHEMA),'dataset_rows_sha256':sha,
            'preparation_code_sha256':code_hash,'model_sha256':hashlib.sha256(b'fixture-not-a-real-model').hexdigest(),'training_events':1,'training_rows':2,
            'finished_at':(self.now-timedelta(hours=20)).isoformat()}))
        self.predict=Mock(side_effect=lambda f,*args: .7 if f['selection_side']=='home' else .2)

    def sample(self,event_id,sid,at):
        return {'format':'doscbot.source-input.v2','as_of':at.isoformat(),'event':{'source_event_id':event_id,'sport':'football','phase':'prematch','home':'A','away':'B','competition':'League','kickoff_at':(at+timedelta(hours=3)).isoformat(),
            'markets':[{'key':'m','name':'Исход 1X2','period':'осн.время','outcomes':[{'name':n,'odds':v,'state':'open'} for n,v in [('1',1.8),('X',3.5),('2',4.0)]]}]},
            'team_statistics':{'source_event_id':event_id,'statistics_id':sid,'state':'ok','match_phase':'prematch','observed_at':at.isoformat()},
            'widget_data':{'sport':'football','event_id':event_id,'statistics_id':sid,'profile':{'identity_verified':True},'match':{'status':0,'home':{'id':'a'},'away':{'id':'b'}}}}

    def save_sample(self,p):
        with sqlite3.connect(self.db) as db:db.execute('INSERT INTO samples(event_id,as_of,kickoff_at,sha256,payload) VALUES (?,?,?,?,?)',(p['event']['source_event_id'],p['as_of'],p['event']['kickoff_at'],w.digest(p),w.canonical(p).decode()))

    def observation(self,kind,event_id,value,at):
        with sqlite3.connect(self.db) as db:db.execute('INSERT INTO observations(kind,entity_id,observed_at,received_at,sha256,payload) VALUES(?,?,?,?,?,?)',(kind,event_id,at.isoformat(),at.isoformat(),w.digest(value),w.canonical(value).decode()))

    def native(self,p,at=None):
        e=p['event'];self.observation('event',e['source_event_id'],{'id':int(e['source_event_id']),'idSport':1,'isLive':False,'date':e['kickoff_at'],'members':['A','B'],'idRadar':p['widget_data']['statistics_id']},at or self.now)

    def cycle(self):return w.cycle(self.bundle,self.db,self.state,self.token,predict=self.predict)

    def test_freeze_once_exact_features_read_only_and_outcome_later(self):
        before=self.db.read_bytes();r=self.cycle()
        self.assertEqual((r['recorded_events'],r['scored_events'],r['pending_events']),(1,0,1))
        self.assertEqual(self.predict.call_count,2);self.assertEqual(before,self.db.read_bytes())
        self.assertEqual(r['matches'][0]['probabilities'],{'home':.7,'away':.2})
        market,_=tr.winner_market(self.new['event'])
        self.assertEqual([c.args[0] for c in self.predict.call_args_list],[f for _,f in tr.participant_rows(self.new,market)])
        self.cycle();self.assertEqual(self.predict.call_count,2)
        Clock.value=self.now+timedelta(hours=6)
        self.observation('event_outcome','99',{'format':'doscbot.event-outcome.v1','event_id':'99','sport_id':1,'home':'A','away':'B','kickoff_at':self.new['event']['kickoff_at'],'result_start_at':self.new['event']['kickoff_at'],'method':'sport_names_time_unique','score_text':'2:0 (1:0 1:0)'},Clock.value)
        before=self.db.read_bytes();r=self.cycle();self.assertEqual(self.db.read_bytes(),before)
        self.assertEqual(r['scored_events'],1,r)
        self.assertEqual(r['matches'][0]['winner'],'home');self.assertAlmostEqual(r['evaluation']['overall']['brier'],.065)
        self.assertEqual(self.predict.call_count,2)

    def test_partial_pair_is_not_saved(self):
        self.predict.side_effect=[.7,TimeoutError()]
        r=self.cycle();self.assertEqual(r['state'],'degraded');self.assertEqual(r['recorded_events'],0)
        self.assertFalse(list((self.state/'frozen').rglob('*.json')))

    def test_late_completion_not_saved(self):
        def late(*args):Clock.value=self.now+timedelta(hours=2,seconds=1);return .4
        self.predict.side_effect=late;r=self.cycle();self.assertEqual(r['recorded_events'],0)
        self.assertEqual(r['skipped']['horizon_passed_during_inference'],1)

    def test_training_identity_alias_excluded(self):
        p=self.sample('100','old',self.now);self.save_sample(p);self.native(p)
        r=self.cycle();self.assertEqual(r['recorded_events'],1);self.assertEqual(r['skipped']['duplicate_or_trained_statistics'],1)

    def test_stale_schedule_and_preexisting_outcome_excluded(self):
        Clock.value=self.now+timedelta(minutes=6);r=self.cycle();self.assertEqual(r['recorded_events'],0)
        self.assertEqual(r['skipped']['current_schedule_stale_or_missing'],1)
        Clock.value=self.now;self.observation('event_outcome','99',{},self.now)
        r=self.cycle();self.assertEqual(r['recorded_events'],0);self.assertEqual(r['skipped']['outcome_already_exists'],1)
        self.predict.assert_not_called()

    def test_corrupt_frozen_data_rejected(self):
        self.cycle();file=next((self.state/'frozen').rglob('*.json'));value=json.loads(file.read_text());value['data']['packet']['rows'][0]['probability']=1
        file.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'checksum'):self.cycle()

    def test_http_response_identity_and_finite_probability(self):
        features=dict(next(tr.participant_rows(self.new,tr.winner_market(self.new['event'])[0]))[1])
        valid={'format':'prognozich.participant-prediction.v1','target':'participant_win.v1','model_sha256':'model','schema_sha256':'schema','features_sha256':w.digest(features),'probability':.7,**{k:features[k] for k in ('sport','selection_side','market_scope')}}
        for delta in ({},{'model_sha256':'wrong'},{'features_sha256':'wrong'},{'probability':True},{'probability':1.01},{'selection_side':'away'}):
            response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);response.read.return_value=json.dumps(valid|delta).encode()
            with patch.object(w,'urlopen',return_value=response):
                if delta:
                    with self.assertRaises(ValueError):w.remote_predict(features,'model','schema','secret')
                else:self.assertEqual(w.remote_predict(features,'model','schema','secret'),.7)
