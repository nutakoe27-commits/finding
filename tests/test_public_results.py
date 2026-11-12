"""Public betting-only cohorts, distinct prices, finality and private input isolation."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from matchlab.public_results import report,detail
from outcome_helpers import forecast,archive_result


class PublicResultsTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name);self.repo=Repository(self.root/'matchlab.sqlite3');self.repo.initialize()

    def save(self,**kw):return forecast(self.repo,uuid4().hex,**kw)
    def update(self,payload):
        with self.repo.connection() as db:db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(payload),payload['id']))

    def test_only_betting_decisions_enter_metrics_and_pending_is_not_a_win(self):
        self.save(score='2:0');self.save(preference='away',score='2:0');self.save(score=None)
        self.save(decision='skip',score='2:0');self.save(decision='skip',preference=None,score='0:0')
        self.save(engine='model_error');self.save(kickoff=datetime.now(timezone.utc)+timedelta(hours=1),score=None)
        data=report(self.repo);m=data['metrics']
        self.assertEqual((m['decisions'],m['settled'],m['won'],m['lost'],m['pending']),(3,2,1,1,1))
        self.assertEqual(m['win_rate'],.5)
        self.assertTrue(all(row['decision']=='bet' for row in data['matches']))
        pending=next(row for row in data['matches'] if row['state']=='pending')
        self.assertEqual(pending['stake_percent'],.25)

    def test_duplicate_line_is_counted_once_but_changed_prices_are_separate_decisions(self):
        first=self.save(score='2:0');at=datetime.fromisoformat(first['generated_at'])
        same=self.save(event=first['event'],at=at+timedelta(seconds=5),score=None)
        changed=self.save(event=first['event'],at=at+timedelta(seconds=10),score=None)
        changed['context']['neural_input']['quote']['odds']['home']=2.4
        changed['context']['neural_verdict']['odds']=2.4;self.update(changed)
        data=report(self.repo)
        self.assertEqual((data['metrics']['decisions'],data['metrics']['matches'],data['metrics']['won']),(2,1,2))
        self.assertAlmostEqual(data['metrics']['average_odds'],2.2)
        self.assertEqual({row['id'] for row in data['matches']},{first['id'],changed['id']})
        self.assertNotIn(same['id'],{row['id'] for row in data['matches']})

    def test_future_or_expired_prices_are_excluded_by_the_same_rule_for_wins_and_losses(self):
        for score in ('2:0','0:2'):
            item=self.save(score=score);q=item['context']['neural_input']['quote']
            q['observed_at']=(datetime.fromisoformat(item['generated_at'])-timedelta(minutes=4)).isoformat();self.update(item)
        self.assertEqual(report(self.repo)['metrics']['decisions'],0)

    def test_public_projection_never_contains_user_notes_context_tokens_or_identity(self):
        item=self.save();item['context']['unverified_user_notes']='PRIVATE_USER_NOTE'
        item['context']['neural_verdict']['reason']['text']='PRIVATE_USER_NOTE'
        self.update(item)
        data=detail(self.repo,item['id']);encoded=json.dumps(data)
        self.assertTrue(data['explanation_redacted']);self.assertEqual(data['blocks'],[])
        self.assertNotIn('PRIVATE_',encoded);self.assertNotIn('token_usage',encoded);self.assertNotIn('context',data)
        self.assertEqual(report(self.repo)['metrics']['decisions'],1)

    def test_clean_answer_is_original_and_anonymous_and_corrections_resettle(self):
        item=self.save(score=None)
        item['context']['neural_input']['unverified_user_notes']='';item['context'].pop('notes',None)
        item['context']['analysis_origin']='system';item['context']['neural_verdict']['reason']['text']='Именно это объяснение было сохранено до матча.'
        self.update(item)
        first=detail(self.repo,item['id']);self.assertFalse(first['explanation_redacted'])
        self.assertEqual(first['origin'],'system');self.assertEqual(first['state'],'pending')
        with patch('matchlab.qwen._completion',side_effect=AssertionError('called model')):
            archive_result(self.repo,item['event'],'0:2','result')
            after=detail(self.repo,item['id'])
        self.assertEqual(after['blocks'],first['blocks']);self.assertFalse(after['bet_won'])
        self.assertEqual(report(self.repo)['metrics']['lost'],1)

    def test_public_guest_api_is_separate_from_private_history_and_admin_reports(self):
        item=self.save();item['context']['neural_input']['unverified_user_notes']='';item['context'].pop('notes',None);self.update(item)
        skip=self.save(decision='skip')
        with TestClient(create_app(Settings(self.root,'',''))) as client:
            self.assertEqual(client.get('/api/results').status_code,200)
            self.assertEqual(client.get('/api/results/'+item['id']).status_code,200)
            self.assertEqual(client.get('/api/results/'+skip['id']).status_code,404)
            self.assertEqual(client.get('/api/analyses/'+item['id']).status_code,401)
            self.assertEqual(client.get('/api/admin/verdict-outcomes').status_code,401)
            for query in ('offset=-1','limit=1000','sport=not_supported'):
                self.assertEqual(client.get('/api/results?'+query).status_code,422)

    def test_average_odds_includes_losses_and_pending_bets_and_is_independent_of_page_or_stake(self):
        self.save(score='2:0')
        other=self.save(score='2:0');other['context']['neural_input']['quote']['odds']['home']=2.4
        other['context']['neural_verdict']['odds']=2.4;other['context']['neural_verdict']['stake_percent']=.5;self.update(other)
        self.save(preference='away',score='2:0');self.save(score=None);self.save(decision='skip')
        data=report(self.repo,limit=1);m=data['metrics']
        self.assertEqual((m['decisions'],m['settled'],m['average_odds_count']),(4,3,4))
        self.assertAlmostEqual(m['average_odds'],(2+2.4+3.5+2)/4)
        self.assertEqual(m['average_odds'],report(self.repo,offset=1,limit=1)['metrics']['average_odds'])
        self.assertNotIn('simulation',data);self.assertFalse(any(key.startswith('paper_') for key in m))

    def test_no_average_is_unknown_but_a_pending_recommendation_already_has_a_price(self):
        self.assertIsNone(report(self.repo)['metrics']['average_odds'])
        self.save(score=None)
        m=report(self.repo)['metrics']
        self.assertEqual((m['average_odds'],m['average_odds_count']),(2,1))
        self.assertIsNone(m['win_rate'])

    def test_cached_api_results_resettle_when_an_archived_score_is_corrected(self):
        item=self.save(score=None)
        with TestClient(create_app(Settings(self.root,'',''))) as client:
            self.assertEqual(client.get('/api/results').json()['metrics']['pending'],1)
            archive_result(self.repo,item['event'],'2:0','corrected')
            self.assertEqual(client.get('/api/results').json()['metrics']['won'],1)
            archive_result(self.repo,item['event'],'0:2','corrected')
            self.assertEqual(client.get('/api/results').json()['metrics']['lost'],1)
