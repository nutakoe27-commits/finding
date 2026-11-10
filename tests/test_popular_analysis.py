"""Source-selected cards, sparse observed season rates and grounded explanations."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from matchlab import archive
from matchlab.api import create_app
from matchlab.chances import assess,fit_goals,goal_probabilities,render_report
from matchlab.config import Settings
from matchlab.event_analysis import analysis_context
from matchlab.event_reasoning import trusted_facts,verified_observations
from matchlab.independent_context import independent_context
from matchlab.market_focus import focus_context
from matchlab.match_history import normalize
from matchlab.popular_markets import companion
from matchlab.season_evidence import season_rates
from matchlab.storage import Repository
from matchlab.markets import validate_event_markets
from winline.projection import FORMAT
from account_helpers import signed_in
from test_archive import header
from test_product import event
from test_statistics import stats
from test_event_reasoning import FakeModel
from test_independent_analysis import provider_context
from reasoning_helpers import EXPLANATION,explained,completion

FIXTURES=Path(__file__).parent/'fixtures'
POPULAR=json.loads((FIXTURES/'winline_popular_markets.json').read_text())['markets']
SEASON=json.loads((FIXTURES/'winline_u23_season_rates.json').read_text())
NOW=datetime.now(timezone.utc)


def observed_event(identifier='101'):
    value=event(identifier)
    value.update(home=SEASON['home'],away=SEASON['away'],popular_markets=deepcopy(POPULAR),popular_complete=True,
                 statistics_url='https://winline.ru/stats/match/72334410')
    return value


class PopularTests(unittest.TestCase):
    def context(self):
        document=validate_event_markets(observed_event())
        context=analysis_context(document,team_statistics=deepcopy(SEASON))
        context['probability_assessment']={'markets':[],'winner':None,'facts':['Исходные показатели'],'limitations':[]}
        return document,context

    def test_all_eight_source_cards_override_heuristic_limit_and_preserve_first_half_parity(self):
        _,context=self.context();selected=focus_context(context,2)
        self.assertEqual(len(selected['markets']),8)
        self.assertEqual([m['group'] for m in selected['markets']].count('Популярные на матч'),4)
        self.assertEqual([m['group'] for m in selected['markets']].count('Популярные на тайм'),4)
        self.assertTrue(any(m['name']=='Чет/Нечет' and m['period']=='1 тайм' for m in selected['markets']))
        self.assertTrue(selected['snapshot']['complete']);self.assertNotIn('popular_markets',selected)
        self.assertEqual(selected['market_selection']['policy'],'source-popular-v1')

    def test_sparse_season_snapshot_produces_win_percentage_and_all_eighteen_outcomes(self):
        document,context=self.context();context=focus_context(context)
        with patch('matchlab.chances.football_sample',return_value=[]):result=assess(None,document,context)
        self.assertIsNotNone(result['winner']);self.assertTrue(0<result['winner']['probability']<1)
        self.assertAlmostEqual(sum(result['outcome_probabilities'].values()),1)
        self.assertEqual(len(result['markets']),18)
        self.assertEqual(len({m['market_key'] for m in result['markets']}),8)
        self.assertIn('%',render_report(result).split('\n\n')[0])
        self.assertTrue(any('Малая или давняя выборка' in text for text in result['limitations']))
        self.assertTrue(any('таймов' in text for text in result['limitations']))

    def test_popular_prices_do_not_change_probabilities_or_card_selection(self):
        document,context=self.context();selected=focus_context(context)
        with patch('matchlab.chances.football_sample',return_value=[]):first=assess(None,document,selected)
        for market in context['popular_markets']:
            for outcome in market['outcomes']:outcome['odds']=9999
        changed=focus_context(context)
        with patch('matchlab.chances.football_sample',return_value=[]):second=assess(None,document,changed)
        self.assertEqual(first,second)
        self.assertNotIn('"odds"',json.dumps(independent_context(changed)))

    def test_probabilities_for_known_popular_conditions_do_not_require_open_fresh_prices(self):
        document,context=self.context();context=focus_context(context)
        for market in context['markets']:
            market['is_stale']=True
            for outcome in market['outcomes']:outcome['state']='unavailable'
        with patch('matchlab.chances.football_sample',return_value=[]):result=assess(None,document,context)
        self.assertEqual(len(result['markets']),18)
        self.assertTrue(all(row['availability']=='unconfirmed' for row in result['markets']))
        self.assertTrue(any('текущее наличие' in text for text in result['limitations']))

    def test_source_card_identity_and_phase_are_required_for_stream_companion(self):
        document=validate_event_markets(observed_event())
        self.assertEqual(len(companion(document,document)),8)
        for field,value in [('home','Another team'),('sport','tennis'),('source_event_id','999'),('phase','live')]:
            candidate={**document,field:value}
            if field=='phase':candidate['popular_phase']=value
            with self.subTest(field=field):self.assertEqual(companion(document,candidate),[])
        old={**document,'popular_observed_at':(NOW-timedelta(minutes=10)).isoformat()}
        self.assertTrue(all(m['is_stale'] for m in companion(document,old)))

    def test_popular_validation_rejects_unknown_groups_and_future_observation(self):
        for change in ('group','time'):
            value=observed_event()
            if change=='group':value['popular_markets'][0]['group']='Invented popular group'
            else:value['popular_observed_at']=(NOW+timedelta(hours=1)).isoformat()
            with self.subTest(change=change),self.assertRaises(ValueError):validate_event_markets(value)

    def test_partial_book_updates_popular_cards_without_freshening_original_prices(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Path(folder)/'matchlab.sqlite3');repo.initialize()
            original=observed_event();original.pop('popular_markets');repo.import_event_markets(original)
            before=repo.event_markets('101')
            changed=deepcopy(original);at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
            changed.update(popular_markets=deepcopy(POPULAR),popular_complete=True,observed_at=at,popular_observed_at=at)
            changed.pop('statistics_url');changed['coverage']['stable_passes']=0
            self.assertEqual(repo.import_event_markets(changed)['reason'],'incomplete_snapshot')
            after=repo.event_markets('101')
            self.assertEqual(after['observed_at'],before['observed_at']);self.assertEqual(after['markets'],before['markets'])
            self.assertEqual(len(after['popular_markets']),8);self.assertEqual(after['popular_observed_at'],at)

    def test_parallel_general_collector_preserves_popular_snapshot_with_its_original_time(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Path(folder)/'matchlab.sqlite3');repo.initialize()
            original=observed_event();repo.import_event_markets(original)
            before=repo.event_markets('101')
            later=deepcopy(original);later.pop('popular_markets');later.pop('popular_complete')
            later['observed_at']=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
            repo.import_event_markets(later)
            after=repo.event_markets('101')
            self.assertEqual(after['popular_markets'],before['popular_markets'])
            self.assertEqual(after['popular_observed_at'],before['popular_observed_at'])
            self.assertNotEqual(after['observed_at'],after['popular_observed_at'])
            later['phase']='live';later['observed_at']=(datetime.now(timezone.utc)+timedelta(seconds=2)).isoformat()
            repo.import_event_markets(later)
            self.assertFalse(repo.event_markets('101').get('popular_markets'))


class SeasonTests(unittest.TestCase):
    def test_observed_rates_are_not_confused_with_totals_or_streaks(self):
        rates=season_rates({'team_statistics':SEASON})
        self.assertEqual((rates['home']['for'],rates['home']['against']),(.33,1))
        self.assertEqual((rates['away']['for'],rates['away']['against']),(1.5,2.5))
        self.assertEqual((rates['home']['matches'],rates['away']['matches']),(3,4))
        totals={'sections':[s for s in SEASON['sections'] if 'Итоги' in s.get('filters',[])]}
        self.assertEqual(season_rates({'team_statistics':totals}),{})

    def test_one_confirmed_result_per_team_is_usable_without_thirty_game_gate(self):
        rows=[{'home':'a','away':'b','goals':(1,0),'at':NOW-timedelta(days=1)}]
        self.assertIsNotNone(fit_goals(rows,'a','b',NOW))
        self.assertIsNone(fit_goals([],'a','b',NOW))
        self.assertIsNotNone(fit_goals([],'a','b',NOW,season=season_rates({'team_statistics':SEASON})))

    def test_broad_history_includes_other_tournaments_but_not_unconfirmed_or_future_results(self):
        context=provider_context();row=context['team_statistics']['structured_feed']['participants']['home'][0]['verified_matches'][0]
        row['_utid']=999
        result=normalize(context,NOW,broad=True)
        self.assertEqual(result['samples']['home'][0]['scope_weight'],.5)
        row['status']=None;self.assertEqual(normalize(context,NOW,broad=True)['samples']['home'],[])
        row['status']='finished';row['time']['uts']=int((NOW+timedelta(days=1)).timestamp())
        self.assertEqual(normalize(context,NOW,broad=True)['samples']['home'],[])


class ExplanationTests(unittest.TestCase):
    def facts(self):return trusted_facts({'probability_assessment':{'facts':['Команда А: забито за матч 0.33, пропущено 1.','Команда Б: забито 1.5, пропущено 2.5.']}})

    def test_detailed_explanation_keeps_exact_facts_and_why_they_matter(self):
        value=explained(['F1','F2']);rows=verified_observations(value,[],self.facts(),require_explanation=True)
        self.assertIn('0.33',rows[0]['text']);self.assertIn('2.5',rows[0]['text']);self.assertIn(EXPLANATION,rows[0]['text'])
        self.assertEqual(len(rows[0]['quotes']),2)

    def test_unknown_references_invented_numbers_and_probability_overrides_fail(self):
        cases=[{'fact_ids':['F1']},explained(['missing']),explained(['F1']),explained(['F1'])]
        cases[2]['arguments'][0]['explanation']=EXPLANATION+' Вероятность победы 99%.'
        cases[3]['arguments'][0]['explanation']=EXPLANATION+' Зафиксировано 9999 голов.'
        for value in cases:
            with self.subTest(value=value),self.assertRaises(ValueError):verified_observations(value,[],self.facts(),require_explanation=True)

    def test_caution_is_allowed_but_guarantees_and_formulas_are_rejected(self):
        value=explained(['F1']);value['arguments'][0]['explanation']=EXPLANATION+' Это не гарантирует победу.'
        self.assertTrue(verified_observations(value,[],self.facts(),require_explanation=True))
        for text in (EXPLANATION+' Это гарантирует победу.',EXPLANATION+' Формула Пуассона описывает расчёт.'):
            value['arguments'][0]['explanation']=text
            with self.assertRaises(ValueError):verified_observations(value,[],self.facts(),require_explanation=True)

    def test_report_does_not_truncate_detailed_explanation_to_old_short_fact_limit(self):
        text=EXPLANATION*3
        report=render_report({'winner':None,'markets':[],'facts':[],'limitations':[]},arguments=[text])
        self.assertIn(text,report)


class PopularApiTests(unittest.TestCase):
    def test_full_stream_request_uses_eight_source_cards_season_rates_and_detailed_qwen_output(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(Path(folder),'http://localhost:9/v1','fixture',qwen_batch_tokens=128000)
            repo=Repository(settings.database)
            with TestClient(create_app(settings)) as client:
                signed_in(client,repo)
                source=observed_event();repo.import_event_markets(source)
                snapshot=stats('101');snapshot.update(sections=deepcopy(SEASON['sections']),header='Вьетнам (до23) — Таиланд (до23)',match_phase='prematch')
                self.assertTrue(repo.import_statistics(snapshot)['updated'])
                native=header('101');native.update(members=[source['home'],source['away']],kickoff_at=source['kickoff_at'],competition=source['competition'])
                cycle='a'*64
                archive.ingest(repo,{'format':FORMAT,'mode':'feed','cycle':cycle,'events':[native]})
                archive.ingest(repo,{'format':FORMAT,'mode':'commit','cycle':cycle,'source_at':native['observed_at'],'expected':1})
                model=FakeModel(limit=131072)
                with patch('matchlab.qwen._request',side_effect=model.request),patch('matchlab.qwen._completion',side_effect=completion):
                    response=client.post('/api/archive/events/101/explain',json={})
                self.assertEqual(response.status_code,200,response.text)
                result=response.json();context=result['context'];assessment=context['probability_assessment']
                self.assertEqual(context['coverage']['analyzed_markets'],8)
                self.assertEqual(context['coverage']['analyzed_outcomes'],18)
                self.assertEqual(assessment['history_diagnostics']['archive_matches'],0)
                self.assertIsNotNone(assessment['winner']);self.assertEqual(context['ai_generation']['state'],'ready')
                self.assertIn(EXPLANATION,result['text']);self.assertIn('%',result['text'].split('\n\n')[0])
                self.assertNotIn('"odds"',json.dumps(context));self.assertNotIn('Пуассон',result['text'])


if __name__=='__main__':unittest.main()
