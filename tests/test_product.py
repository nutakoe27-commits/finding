from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from account_helpers import signed_in

from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.event_analysis import analysis_context, request_key
from matchlab.markets import FORMAT, validate_event_markets
from matchlab.storage import Repository
from matchlab import qwen


def event(event_id="123", *, competition="Россия. Премьер-лига", home="Спартак", phase="prematch"):
    now = datetime.now(timezone.utc)
    return {"format": FORMAT, "source": "winline", "sport": "football", "source_event_id": event_id,
            "url": f"https://winline.ru/stavki/sport/futbol/rossiya/rpl/{event_id}",
            "home": home, "away": "Зенит", "competition": competition, "phase": phase,
            "kickoff_at": (now + timedelta(days=2)).isoformat(), "observed_at": now.isoformat(),
            "parser_version": "product-test", "markets": [{"name": "Исход 1X2", "period": "осн.время",
                "outcomes": [{"name": "П1", "odds": 2.5}, {"name": "X", "odds": 3.4}, {"name": "П2", "odds": 2.8}]}],
            "coverage": {"expected_outcomes": 3, "all_tab_selected": True, "stable_passes": 2, "scroll_exhausted": True}}


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.client = TestClient(create_app(Settings(self.path, "http://127.0.0.1:9/v1", "isolated-model")))
        self.client.__enter__()
        signed_in(self.client,Repository(self.path / 'matchlab.sqlite3'))
        self.repo = Repository(self.path / "matchlab.sqlite3")
        self.repo.import_event_markets(event())

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def test_current_brand_is_used_in_api_and_ai_prompts(self):
        self.assertEqual(self.client.get('/openapi.json').json()['info']['title'],'prognozich ai')
        for prompt in (qwen.SYSTEM,qwen.EVENT_SYSTEM):
            self.assertIn('prognozich ai',prompt)
            self.assertNotIn('Matchlab',prompt)

    def test_quote_summary_does_not_infer_team_strength_or_match_status(self):
        settings = Settings(self.path, 'http://127.0.0.1:9/v1', 'fixture-model')
        ctx = {'team_statistics': None, 'statistical_model': None, 'archive_history': {'state': 'empty'},
               'web_research': {'state': 'unavailable'}, 'selection': {'market_key': 'double'},
               'snapshot': {'is_stale': False}, 'request_time': {'kickoff_passed': True},
               'event': {'phase': 'unknown'}, 'markets': [
                   {'key': 'double', 'name': 'Двойной шанс', 'is_stale': False,
                    'outcomes': [{'name': '1X', 'odds': 1.49, 'state': 'open'},
                                 {'name': 'X2', 'odds': 1.49, 'state': 'open'}]}]}
        with patch('matchlab.qwen._completion', side_effect=AssertionError('model called')):
            result = qwen.explain_event(settings, ctx)
        self.assertNotIn('1.49', result)
        self.assertNotIn('X2: 1.49', result)
        self.assertIn('текущая стадия матча не подтверждена', result)
        self.assertNotIn('повышенная волатильность', result)
        self.assertNotIn('матч начался', result)
        ctx['archive_history'] = {'state': 'ready'}
        with patch('matchlab.qwen._completion', return_value='Обычный разбор') as model:
            self.assertEqual(qwen.explain_event(settings, ctx), 'Обычный разбор')
            model.assert_called_once()

    def test_live_snapshot_reports_raw_clock_and_each_observation_time(self):
        settings = Settings(self.path, 'http://127.0.0.1:9/v1', 'fixture-model')
        context = {'team_statistics': None, 'statistical_model': None, 'archive_history': {'state': 'empty'},
                   'web_research': {'state': 'ready', 'sources': [{'id': 5, 'excerpt': 'unverified'}]},
                   'selection': {'market_key': 'live'}, 'request_time': {'kickoff_passed': True},
                   'snapshot': {'is_stale': True, 'observed_at': '2026-11-09T05:47:05+00:00'},
                   'event': {'phase': 'live', 'live_score': '2:0', 'live_clock': "2Т 13'"},
                   'markets': [{'key': 'live', 'name': 'Тотал', 'period': '2-й тайм', 'parameter': '2.5', 'is_stale': True,
                                'observed_at': '2026-11-09T05:40:00+00:00',
                                'outcomes': [{'name': 'Больше', 'odds': 1.42, 'state': 'open'}]}]}
        with patch('matchlab.qwen._completion', side_effect=AssertionError('model called')):
            text = qwen.explain_event(settings, context)
        self.assertIn('Счёт: 2:0', text)
        self.assertIn("Отметка источника: «2Т 13'»", text)
        self.assertIn('09.11.2026, 08:47:05 МСК', text)
        self.assertNotIn('Наблюдение коэффициентов', text)
        self.assertIn('Тотал · 2-й тайм · значение: 2.5', text)
        self.assertIn('Снимок мог устареть', text)  # Stale live score.
        self.assertNotIn('Эти котировки устарели', text)
        self.assertNotIn('давление', text)
        self.assertNotIn('доминиру', text)

    def test_catalogue_searches_all_pages_with_cyrillic_and_filters(self):
        self.repo.import_event_markets(event("124", home="Динамо", competition="Беларусь. Высшая лига"))
        self.repo.import_event_markets(event("125", home="Крылья Советов", phase="live"))
        listing = self.client.get("/api/bookmakers/events?include_started=true&search=СПАРТАК&limit=1").json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["events"][0]["source_event_id"], "123")
        self.assertEqual(listing["events"][0]["primary_odds"], {"home": 2.5, "draw": 3.4, "away": 2.8})
        self.assertNotIn("markets", listing["events"][0])
        self.assertEqual(listing["summary"]["live"], 1)
        upcoming = self.client.get("/api/bookmakers/events?include_started=true&phase=upcoming").json()
        self.assertEqual(upcoming["total"], 2)
        favorites = self.client.get("/api/bookmakers/events?include_started=true&ids=124,125").json()
        self.assertEqual(favorites["total"], 2)
        day = (datetime.now(timezone.utc) + timedelta(days=2, hours=3)).date().isoformat()
        self.assertEqual(self.client.get(f"/api/bookmakers/events?include_started=true&day={day}").json()["total"], 3)
        self.assertEqual(self.client.get("/api/bookmakers/events", params={"competition":"Беларусь. Высшая лига"}).json()["total"], 1)
        for query in ("phase=unknown", "sort=DROP", "ids=123,'bad'", "search=" + "a" * 201, "day=yesterday"):
            self.assertEqual(self.client.get("/api/bookmakers/events?" + query).status_code, 422)

    def test_non_epl_match_gets_ai_context_without_inventing_a_statistical_model(self):
        with patch("matchlab.api.qwen.explain_event", return_value="Разбор рынка.") as completion:
            response = self.client.post("/api/bookmakers/events/123/explain", json={})
        self.assertEqual(response.status_code, 200, response.text)
        context = completion.call_args.args[1]
        self.assertIsNone(context["statistical_model"])
        self.assertEqual(context["event"]["home"], "Спартак")
        self.assertFalse(context["request_time"]["kickoff_passed"])
        self.assertIsNone(context["market_implied"])
        self.assertIn("current_lineups", context["missing_data"])
        self.assertFalse(self.client.get("/api/bookmakers/events/123/analysis").json()["prediction_available"])

    def test_existing_snapshots_show_prices_after_upgrade_without_waiting_for_recrawl(self):
        with self.repo.connection() as db:
            db.execute("UPDATE bookmaker_event_markets SET metadata = json_remove(metadata, '$.primary_odds')")
        listing = self.client.get("/api/bookmakers/events").json()
        self.assertEqual(listing["events"][0]["primary_odds"], {"home":2.5,"draw":3.4,"away":2.8})
        self.assertNotIn("markets", listing["events"][0])

    def test_selected_outcome_reaches_ai_and_saved_history_uses_exact_snapshot(self):
        document = self.repo.event_markets("123")
        market = document["markets"][0]
        body = {"observed_at":document["observed_at"], "market_key":market["key"], "outcome_key":market["outcomes"][2]["key"], "notes":"Непроверенная заметка"}
        with patch("matchlab.api.qwen.explain_event", return_value="Оценка исхода П2.") as completion:
            result = self.client.post("/api/bookmakers/events/123/explain", json=body).json()
            repeated = self.client.post("/api/bookmakers/events/123/explain", json=body).json()
        self.assertEqual(completion.call_count, 1)
        self.assertTrue(repeated["cached"])
        self.assertEqual(result["context"]["selection"]["outcome_key"], body["outcome_key"])
        self.assertEqual(result["context"]["unverified_user_notes"], body["notes"])
        listing = self.client.get("/api/analyses").json()["analyses"]
        self.assertEqual(len(listing), 1)
        self.assertNotIn("context", listing[0])
        saved = self.client.get("/api/analyses/" + result["id"]).json()
        self.assertEqual(saved["observed_at"], document["observed_at"])
        self.assertNotIn("odds", saved["context"]["markets"][0]["outcomes"][2])

    def test_changed_snapshot_and_invalid_selection_do_not_call_ai(self):
        with patch("matchlab.api.qwen.explain_event") as completion:
            for body, code in (({"observed_at":"2000-01-01"},409), ({"market_key":"a"*32},422), ({"outcome_key":"a"*32},422)):
                response = self.client.post("/api/bookmakers/events/123/explain", json=body)
                self.assertEqual(response.status_code, code, response.text)
        completion.assert_not_called()

    def test_suspended_selection_is_not_sent_to_ai(self):
        value = event()
        value["markets"][0]["outcomes"][0].update(odds=None, state="suspended")
        self.repo.import_event_markets(value)
        document = self.repo.event_markets("123")
        market = document["markets"][0]
        with patch("matchlab.api.qwen.explain_event") as completion:
            response = self.client.post("/api/bookmakers/events/123/explain", json={"market_key":market["key"], "outcome_key":market["outcomes"][0]["key"]})
        self.assertEqual(response.status_code, 422)
        completion.assert_not_called()

    def test_public_errors_do_not_expose_model_name_or_connection_configuration(self):
        with patch("matchlab.api.qwen.explain_event", side_effect=ValueError("Qwen QWEN_BASE_URL private-test-key")):
            response = self.client.post("/api/bookmakers/events/123/explain", json={})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("qwen", response.text.lower())
        self.assertNotIn("private-test-key", response.text)
        self.assertEqual(self.client.get("/api/analyses").json()["analyses"], [])

    def test_full_context_keeps_all_outcomes_and_selection(self):
        value = event()
        value["markets"][0]["outcomes"] = [{"name":str(index), "odds":2.0} for index in range(150)]
        value["coverage"]["expected_outcomes"] = 150
        document = validate_event_markets(value)
        market = document["markets"][0]
        selected = market["outcomes"][-1]
        context = analysis_context(document, market_key=market["key"], outcome_key=selected["key"])
        self.assertEqual(context["coverage"]["analyzed_outcomes"], 150)
        self.assertEqual(context["coverage"]["stored_outcomes"], 150)
        self.assertIn(selected["key"], [item["key"] for item in context["markets"][0]["outcomes"]])

    def test_all_stored_markets_are_included_even_with_full_statistics(self):
        from copy import deepcopy
        value=event(); template=value['markets'][0]
        value['markets']=[{**deepcopy(template),'name':f'Рынок {index}'} for index in range(25)]
        value['coverage']['expected_outcomes']=75
        doc=validate_event_markets(value)
        ctx=analysis_context(doc,team_statistics={'match_phase':'prematch','sections':[]})
        self.assertEqual(ctx['coverage']['analyzed_markets'],25)
        self.assertEqual(ctx['coverage']['analyzed_outcomes'],75)

    def test_freshness_boundary_changes_analysis_cache_key(self):
        document = self.repo.event_markets("123")
        key = request_key(document, None, None, "")
        with patch("matchlab.event_analysis.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(minutes=5)
            self.assertNotEqual(key, request_key(document, None, None, ""))

    def test_new_analysis_instructions_do_not_reuse_old_cached_response(self):
        document = self.repo.event_markets("123")
        key = request_key(document, None, None, "")
        with patch("matchlab.event_analysis.ANALYSIS_VERSION", "future-instructions"):
            self.assertNotEqual(key, request_key(document, None, None, ""))


if __name__ == "__main__":
    unittest.main()
