from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from account_helpers import signed_in

from matchlab.api import create_app
from matchlab.bookmakers import EPL, model_teams
from matchlab.config import Settings
from matchlab.event_analysis import analysis_context
from matchlab.markets import headline_market, headline_preview, validate_event_markets
from matchlab.sports import SPORTS, sport_from_url
from matchlab.storage import Repository
from matchlab.winline_markets import primary_quote
from sports_fixtures import PROJECTIONS, document


class SportContracts(unittest.TestCase):
    def test_all_registered_sport_slugs_and_unknown_observed_sports_have_identities(self):
        for sport in SPORTS:
            for slug in sport.slugs:
                self.assertEqual(sport_from_url(f'https://winline.ru/stavki/sport/{slug}/'),sport.key)
        for slug in ('kibersport','kiber_fifa','virtual_sport','sport24_7'):
            self.assertIsNone(sport_from_url(f'https://winline.ru/stavki/sport/{slug}/'))
        self.assertEqual(sport_from_url('https://winline.ru/stavki/sport/biatlon/test/league/123'),'biatlon')

    def test_sport_must_match_canonical_source_url_and_football_quotes_remain_football_only(self):
        for item in PROJECTIONS:
            value = validate_event_markets(document(item))
            self.assertEqual(value['sport'],item['sport'])
            if item['sport'] != 'football':
                self.assertIsNone(primary_quote(value))
                self.assertIsNone(model_teams({**value,'competition':EPL,'home':'Arsenal','away':'Chelsea'}))
                invalid = deepcopy(value); invalid['sport']='football'
                with self.assertRaises(ValueError): validate_event_markets(invalid)

    def test_tennis_has_two_real_choices_and_hockey_regular_time_is_distinct_from_overtime(self):
        hockey = validate_event_markets(document(PROJECTIONS[1]))
        tennis = validate_event_markets(document(PROJECTIONS[2]))
        chosen = headline_market(hockey)
        self.assertEqual(chosen['period'],'осн.время')
        self.assertEqual(len(chosen['outcomes']),3)
        overtime = next(market for market in hockey['markets'] if market['period']=='с ОТ и Б')
        self.assertNotEqual(chosen['key'],overtime['key'])
        self.assertEqual([outcome['name'] for outcome in headline_market(tennis)['outcomes']],['П1','П2'])
        self.assertEqual(headline_market(tennis)['period'],'Матч')
        context = analysis_context(tennis)
        self.assertEqual(set(context['market_implied']['probabilities']),{'П1','П2'})
        self.assertIsNone(context['statistical_model'])

    def test_outright_has_real_title_no_fake_opponent_and_preview_does_not_claim_full_coverage(self):
        value = validate_event_markets(document(PROJECTIONS[3]))
        self.assertIsNone(value['home']); self.assertIsNone(value['away'])
        self.assertEqual(value['title'],'Тестовая гонка')
        preview = headline_preview(value)
        self.assertEqual(preview['outcome_count'],4)
        self.assertEqual(len(preview['outcomes']),3)
        self.assertEqual(analysis_context(value)['coverage']['analyzed_outcomes'],4)

    def test_large_winner_market_is_not_normalized_from_only_a_partial_context(self):
        value = document(PROJECTIONS[3])
        value['markets'][0]['outcomes']=[{'name':f'Участник {i}','odds':5+i} for i in range(100)]
        value['coverage']['expected_outcomes']=100
        context = analysis_context(validate_event_markets(value))
        self.assertEqual(context['coverage']['analyzed_outcomes'],80)
        self.assertIsNone(context['market_implied'])


class SportReceiver(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.path=Path(self.folder.name)
        self.client=TestClient(create_app(Settings(self.path,'http://127.0.0.1:9/v1','local-test-model')))
        self.client.__enter__()
        signed_in(self.client,Repository(self.path / 'matchlab.sqlite3'))
        self.repo=Repository(self.path/'matchlab.sqlite3')
        for item in PROJECTIONS: self.repo.import_event_markets(document(item))

    def tearDown(self):
        self.client.__exit__(None,None,None); self.folder.cleanup()

    def test_sport_filter_scopes_events_competitions_summary_and_sport_menu(self):
        result=self.client.get('/api/bookmakers/events?sport=tennis').json()
        self.assertEqual(result['total'],1)
        self.assertEqual(result['summary']['total'],1)
        self.assertEqual(result['competitions'],['ATP. Тестовый турнир'])
        self.assertEqual(result['events'][0]['sport'],'tennis')
        self.assertEqual(len(result['events'][0]['headline']['outcomes']),2)
        self.assertNotIn('markets',result['events'][0])
        self.assertEqual({item['key']:item['total'] for item in result['sports']}['hockey'],1)
        self.assertEqual(self.client.get('/api/bookmakers/events?sport=kibersport').status_code,422)
        self.assertEqual(self.client.get('/api/bookmakers/events?search=ГОНКА').json()['total'],1)

    def test_sport_cannot_overwrite_another_sport_with_the_same_event_id(self):
        previous=self.repo.event_markets(PROJECTIONS[0]['id'])
        wrong=document(PROJECTIONS[1]); wrong['source_event_id']=previous['source_event_id']; wrong['url']=wrong['url'].rsplit('/',1)[0]+'/'+wrong['source_event_id']
        with self.assertRaises(ValueError): self.repo.import_event_markets(wrong)
        self.assertEqual(self.repo.event_markets(previous['source_event_id']),previous)

    def test_sports_reach_ai_with_their_own_rules_and_selected_period_in_saved_history(self):
        for item in PROJECTIONS[1:]:
            with self.subTest(sport=item['sport']):
                stored=self.repo.event_markets(item['id'])
                market=stored['markets'][-1]
                with patch('matchlab.api.qwen.explain_event',return_value='Разбор выбранного спортивного рынка.') as completion:
                    result=self.client.post(f"/api/bookmakers/events/{item['id']}/explain",json={'market_key':market['key'],'outcome_key':market['outcomes'][0]['key']})
                self.assertEqual(result.status_code,200,result.text)
                context=completion.call_args.args[1]
                self.assertEqual(context['event']['sport'],item['sport'])
                self.assertIsNone(context['statistical_model'])
                self.assertEqual(context['markets'][0]['period'],market['period'])
                saved=self.client.get('/api/analyses/'+result.json()['id']).json()
                self.assertEqual(saved['event']['sport'],item['sport'])


if __name__=='__main__': unittest.main()
