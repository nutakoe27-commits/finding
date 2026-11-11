"""Public system picks: correct MSK day, frozen quotes, no private inputs or generation."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from matchlab.today_picks import collection,detail
from outcome_helpers import forecast


class TodayPicksTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.repo=Repository(self.root/'matchlab.sqlite3');self.repo.initialize()
        self.now=datetime(2026,11,11,12,tzinfo=timezone.utc)

    def save(self,*,origin='system',price=2.0,**kw):
        kw.setdefault('kickoff',self.now+timedelta(hours=1));kw.setdefault('at',self.now-timedelta(seconds=30));kw.setdefault('score',None)
        item=forecast(self.repo,uuid4().hex,**kw);c=item['context'];c['analysis_origin']=origin
        c.pop('notes',None);c['neural_input']['unverified_user_notes']=''
        side=c['neural_verdict']['preference']
        if side:
            c['neural_input']['quote']['odds'][side]=price
            if c['neural_verdict']['decision']=='bet':c['neural_verdict']['odds']=price
        self.update(item);return item

    def update(self,item):
        with self.repo.connection() as db:db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(item),item['id']))

    def test_today_and_tomorrow_only_system_ready_upcoming_bets_and_never_skip_or_preference(self):
        bet=self.save();skip=self.save(decision='skip',preference='away',price=3.5)
        self.save(origin='human');self.save(engine='model_error');self.save(engine='data_guard')
        tomorrow=self.save(kickoff=self.now+timedelta(days=1));self.save(kickoff=self.now+timedelta(days=2))
        self.save(kickoff=self.now-timedelta(minutes=10),at=self.now-timedelta(minutes=20))
        with patch('matchlab.qwen._completion',side_effect=AssertionError('called model')):
            data=collection(self.repo,now=self.now)
        self.assertEqual(data['available'],2);self.assertEqual({p['id'] for p in data['picks']},{bet['id'],tomorrow['id']})
        self.assertEqual((data['date'],data['through_date']),('2026-11-11','2026-11-12'))
        self.assertEqual(detail(self.repo,tomorrow['id'],now=self.now)['id'],tomorrow['id'])
        row=data['picks'][0]
        self.assertNotIn(skip['id'],{p['id'] for p in data['picks']})
        self.assertNotIn('PRIVATE_',json.dumps(data));self.assertNotIn('context',row);self.assertNotIn('token_usage',row)

    def test_odds_filters_use_preferred_side_and_none_is_not_zero(self):
        low=self.save(price=1.5);high=self.save(preference='away',price=3.5);self.save(decision='skip',preference=None)
        self.assertEqual(collection(self.repo,now=self.now)['total'],2)
        rows=collection(self.repo,now=self.now,min_odds=3,max_odds=4)['picks']
        self.assertEqual([p['id'] for p in rows],[high['id']])
        self.assertEqual(collection(self.repo,now=self.now,min_odds=1.5,max_odds=1.5)['picks'][0]['id'],low['id'])
        for kw in ({'min_odds':3,'max_odds':2},{'min_odds':float('nan')},{'max_odds':float('inf')},{'min_odds':1}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):collection(self.repo,now=self.now,**kw)

    def test_moscow_day_can_differ_from_utc_and_started_cards_expire(self):
        self.now=datetime(2026,11,11,21,30,tzinfo=timezone.utc)
        item=self.save(kickoff=self.now+timedelta(minutes=10))
        tomorrow=self.save(kickoff=datetime(2026,11,12,21,10,tzinfo=timezone.utc))
        self.save(kickoff=datetime(2026,11,13,21,10,tzinfo=timezone.utc))
        before=collection(self.repo,now=self.now)
        self.assertEqual((before['date'],before['through_date']),('2026-11-12','2026-11-13'));self.assertEqual(before['total'],2)
        later=self.now+timedelta(minutes=11)
        self.assertEqual([row['id'] for row in collection(self.repo,now=later)['picks']],[tomorrow['id']])
        self.assertTrue(detail(self.repo,item['id'],now=later)['started'])

    def test_stale_future_or_different_event_quotes_cannot_be_published(self):
        for decision in ('bet','skip'):
            for invalid in ('stale','future','other_event'):
                item=self.save(decision=decision);q=item['context']['neural_input']['quote'];at=datetime.fromisoformat(item['generated_at'])
                if invalid=='stale':q['observed_at']=(at-timedelta(minutes=4)).isoformat()
                elif invalid=='future':q['observed_at']=(at+timedelta(seconds=1)).isoformat()
                else:q['external_id']='another-match';q['home']='Чужая команда'
                self.update(item)
        self.assertEqual(collection(self.repo,now=self.now)['total'],0)

    def test_today_tomorrow_window_crosses_year_boundary_in_moscow(self):
        self.now=datetime(2026,12,31,20,59,tzinfo=timezone.utc)
        first=self.save(kickoff=datetime(2026,12,31,21,0,tzinfo=timezone.utc))
        last=self.save(kickoff=datetime(2027,1,1,20,59,tzinfo=timezone.utc))
        self.save(kickoff=datetime(2027,1,1,21,0,tzinfo=timezone.utc))
        data=collection(self.repo,now=self.now)
        self.assertEqual((data['date'],data['through_date']),('2026-12-31','2027-01-01'))
        self.assertEqual({p['id'] for p in data['picks']},{first['id'],last['id']})

    def test_first_system_answer_is_not_replaced_with_a_better_filtered_price(self):
        first=self.save(price=1.8)
        self.save(event=first['event'],price=3.5,at=self.now-timedelta(seconds=10))
        self.assertEqual(collection(self.repo,now=self.now)['total'],1)
        self.assertEqual(collection(self.repo,now=self.now,min_odds=3)['total'],0)

    def test_private_notes_redact_the_saved_explanation_even_for_a_system_answer(self):
        item=self.save();item['context']['notes']='PRIVATE_OWNER_NOTE';item['context']['neural_verdict']['reason']['text']='PRIVATE_OWNER_NOTE';self.update(item)
        saved=detail(self.repo,item['id'],now=self.now)
        self.assertTrue(saved['explanation_redacted']);self.assertEqual(saved['blocks'],[])
        self.assertNotIn('PRIVATE_',json.dumps(saved))
        private=self.save(origin='human')
        with self.assertRaises(HTTPException):detail(self.repo,private['id'],now=self.now)

    def test_guest_api_cannot_be_changed_into_a_skip_feed_and_requires_no_model(self):
        item=self.save();skip=self.save(decision='skip')
        with TestClient(create_app(Settings(self.root,'',''))) as client,patch('matchlab.today_picks.datetime') as clock,patch('matchlab.verdict_outcomes.datetime') as report_clock,patch('matchlab.qwen._completion',side_effect=AssertionError('called model')):
            clock.now.return_value=self.now
            report_clock.now.return_value=self.now
            response=client.get('/api/picks/today?min_odds=1.5&decision=skip')
            self.assertEqual(response.status_code,200);self.assertEqual(response.json()['total'],1)
            self.assertNotIn('selection_label',response.json()['picks'][0]);self.assertNotIn('blocks',response.json()['picks'][0])
            self.assertNotIn('home',response.json()['picks'][0]['event']);self.assertNotIn('away',response.json()['picks'][0]['event'])
            self.assertNotIn('competition',response.json()['picks'][0]['event'])
            self.assertEqual(response.headers['cache-control'],'no-store')
            saved=client.get('/api/results/'+item['id']);self.assertEqual(saved.status_code,200)
            self.assertTrue(saved.json()['today_pick']);self.assertEqual(saved.json()['decision'],'bet')
            self.assertEqual(saved.json()['event']['home'],item['event']['home'])
            self.assertEqual(saved.json()['event']['competition'],item['event']['competition'])
            self.assertEqual(client.get('/api/results/'+skip['id']).status_code,404)
            self.assertEqual(client.get('/api/picks/today?min_odds=4&max_odds=2').status_code,422)
            self.assertEqual(client.get('/api/admin/verdict-outcomes').status_code,401)

    def test_a_known_live_or_rescheduled_source_event_is_removed_before_scheduled_start(self):
        from matchlab.archive import packed
        item=self.save();event=item['event']
        for value in ({'is_live':True,'kickoff_at':event['kickoff_at']},{'is_live':False,'kickoff_at':(self.now+timedelta(days=1)).isoformat()}):
            with self.repo.connection() as db:
                db.execute('INSERT OR REPLACE INTO archive_feed VALUES (1,?,?,?)',('test',self.now.isoformat(),self.now.isoformat()))
                db.execute('INSERT OR REPLACE INTO archive_feed_rows VALUES (?,?,?)',('test',event['source_event_id'],packed(value)))
            self.assertEqual(collection(self.repo,now=self.now)['total'],0)
            self.assertEqual(detail(self.repo,item['id'],now=self.now)['id'],item['id'])
