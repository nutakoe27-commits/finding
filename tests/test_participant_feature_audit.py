"""Read-only compatibility audit against the pinned external collector code."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


@unittest.skipUnless(os.environ.get('DOSCBOT_SOURCE'), 'Requires verified doscbot source checkout')
class FeatureAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.code = Path(os.environ['DOSCBOT_SOURCE'])
        sys.path.insert(0, str(self.code))
        self.addCleanup(lambda: sys.path.remove(str(self.code)))
        from collector.training import SCHEMA, participant_rows, winner_market
        at = datetime.now(timezone.utc)
        self.event = {'source_event_id': '99', 'sport': 'football', 'phase': 'prematch',
            'home': 'A', 'away': 'B', 'competition': 'League', 'kickoff_at': (at+timedelta(hours=2)).isoformat(),
            'markets': [{'key': 'm', 'name': 'Исход 1X2', 'period': 'осн.время',
                'outcomes': [{'name': n, 'odds': odds, 'state': 'open'} for n,odds in [('1',1.8),('X',3.5),('2',4.0)]]}]}
        self.payload = {'format': 'doscbot.source-input.v2', 'as_of': at.isoformat(), 'event': self.event,
            'team_statistics': {'source_event_id': '99', 'statistics_id': '123', 'state': 'ok',
                                'match_phase': 'prematch', 'observed_at': at.isoformat()},
            'widget_data': {'sport': 'football', 'event_id': '99', 'statistics_id': '123',
                'profile': {'identity_verified': True}, 'match': {'status': 0, 'home': {'id':'a'}, 'away': {'id':'b'}}}}
        self.database = self.root/'dataset.sqlite3'
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE metadata (key TEXT, value TEXT)')
            db.execute("INSERT INTO metadata VALUES ('owner','doscbot.fresh-collector.v1')")
            db.execute('CREATE TABLE samples (id INTEGER, event_id TEXT, as_of TEXT, kickoff_at TEXT, sha256 TEXT, payload TEXT)')
            db.execute('INSERT INTO samples VALUES (?,?,?,?,?,?)', (1,'99',at.isoformat(),self.event['kickoff_at'],digest(self.payload),canonical(self.payload).decode()))
        (self.root/'model').mkdir(); (self.root/'dataset').mkdir()
        (self.root/'model/feature-schema.json').write_bytes(canonical(SCHEMA))
        modules = ('training','training_evidence','training_diagnostics','profiles','store','outcomes','monitor_data')
        self.code_hash = digest({n:hashlib.sha256((self.code/'collector'/(n+'.py')).read_bytes()).hexdigest() for n in modules})
        market,_ = winner_market(self.event)
        self.rows = [{'format':'doscbot.participant-training-row.v1','sample_id':1,'sample_sha256':digest(self.payload),
            'event_group':'99','as_of':at.isoformat(),'kickoff_at':self.event['kickoff_at'],
            'features':features,'market':market} for _,features in participant_rows(self.payload,market)]
        self.schema = SCHEMA
        self.export()

    def export(self):
        raw = b''.join(canonical(r)+b'\n' for r in self.rows)
        checksum = hashlib.sha256(raw).hexdigest()
        (self.root/'dataset/rows.jsonl').write_bytes(raw)
        (self.root/'model/metrics.json').write_bytes(canonical({'dataset_rows_sha256':checksum,
            'schema_sha256':digest(self.schema),'preparation_code_sha256':self.code_hash}))
        (self.root/'dataset/manifest.json').write_bytes(canonical({'rows_sha256':checksum,'export_rows':len(self.rows),'lead_minutes':60}))

    def run_audit(self):
        script = Path(__file__).resolve().parents[1]/'deploy/participant_model/audit-features.py'
        result = subprocess.run([sys.executable,str(script),'--bundle',str(self.root),
            '--code',str(self.code),'--database',str(self.database)],capture_output=True,text=True)
        return result.returncode,json.loads(result.stdout)

    def test_exact_features_without_database_writes_or_outcomes(self):
        before = self.database.read_bytes()
        status,report = self.run_audit()
        self.assertEqual((status,report['state'],report['matched_events']),(0,'all_exported_features_match',1))
        self.assertEqual(self.database.read_bytes(),before)
        self.assertFalse(report['outcomes_queried'])

    def test_feature_drift_identifies_field_without_values(self):
        self.rows[0]['features']['market_count'] = 999
        self.export()
        status,report = self.run_audit()
        self.assertEqual(status,2)
        self.assertEqual(report['examples'][0]['fields'],['market_count'])

    def test_wrong_sample_identity_is_not_compared(self):
        self.rows[0]['sample_sha256'] = 'wrong'
        self.export()
        status,report = self.run_audit()
        self.assertEqual(status,2)
        self.assertEqual(report['compared_events'],0)
        self.assertEqual(report['failures'],{'sample_identity_or_checksum_mismatch':1})

    def test_different_preparation_code_rejected(self):
        self.code_hash = 'wrong'
        self.export()
        status,report = self.run_audit()
        self.assertEqual((status,report['state']),(2,'audit_failed'))
