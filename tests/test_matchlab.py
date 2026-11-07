from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
import httpx

from matchlab.api import create_app
from matchlab.backtest import metrics, run_backtest, save_report
from matchlab.config import Settings
from matchlab.data import import_file, parse_csv
from matchlab.model import GoalModel
from matchlab.qwen import explain
from matchlab.storage import Repository


def training_rows():
    # Deterministic test data, not used by the app or reported backtest.
    teams = ["North", "South", "East", "West"]
    rows = []
    start = date(2023, 8, 1)
    for i in range(180):
        home, away = teams[i % 4], teams[(i + 1 + (i // 4) % 3) % 4]
        rows.append(dict(date=(start + timedelta(days=i)).isoformat(), season="2324",
                         home=home, away=away, home_goals=(i * 7 + 2) % 4, away_goals=(i * 3) % 3))
    return rows


class ModelTests(unittest.TestCase):
    def test_future_and_same_day_results_cannot_change_prediction(self):
        cutoff = date(2024, 2, 1)
        original = GoalModel(training_rows(), cutoff).predict("North", "South")
        poisoned = training_rows() + [dict(date="2024-02-01", season="2324", home="North", away="South", home_goals=30, away_goals=0),
                                      dict(date="2024-03-01", season="2324", home="North", away="South", home_goals=30, away_goals=0)]
        result = GoalModel(poisoned, cutoff).predict("North", "South")
        self.assertEqual(result["probabilities"], original["probabilities"])
        self.assertLess(result["training"]["last_match"], cutoff.isoformat())

    def test_probabilities_and_scorelines_are_valid(self):
        model = GoalModel(training_rows(), date(2024, 2, 1))
        result = model.predict("North", "South")
        self.assertAlmostEqual(sum(result["probabilities"].values()), 1)
        self.assertTrue(all(0 < p < 1 for p in result["probabilities"].values()))
        self.assertGreater(result["expected_goals"]["home"], 0)
        self.assertEqual(len(result["scorelines"]), 5)
        self.assertGreater(result["scorelines"][0]["probability"], result["scorelines"][-1]["probability"])

    def test_unknown_team_and_self_match_are_rejected(self):
        model = GoalModel(training_rows(), date(2024, 2, 1))
        for teams in [("North", "Unknown"), ("North", "North")]:
            with self.assertRaises(ValueError):
                model.predict(*teams)

    def test_insufficient_history_rejected(self):
        with self.assertRaises(ValueError):
            GoalModel(training_rows()[:10], date(2024, 2, 1))


class DataTests(unittest.TestCase):
    def test_iso_and_day_first_csv(self):
        for day in ["2024-08-16", "16/08/2024", "16/08/24"]:
            content = f"Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n{day},Arsenal,Chelsea,2,1,H\n".encode()
            self.assertEqual(parse_csv(content, "2425")[0]["date"], "2024-08-16")

    def test_fixture_without_result_is_not_training_data(self):
        self.assertEqual(parse_csv(b"Date,HomeTeam,AwayTeam,FTHG,FTAG\n2024-08-16,Arsenal,Chelsea,,\n", "2425"), [])

    def test_wrong_scores_dates_and_duplicates_are_rejected(self):
        header = "Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
        invalid = ["2024-08-16,A,B,2,1,A\n", "2024-08-16,A,A,1,1,D\n", "2024-08-16,A,B,-1,0,A\n",
                   "2020-08-16,A,B,1,1,D\n", "2024-08-16,A,B,1,1,D\n2024-08-16,A,B,1,1,D\n"]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                parse_csv((header + row).encode(), "2425")

    def test_empty_import_preserves_previous_season_and_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(Path(directory) / "test.sqlite3")
            repo.initialize()
            repo.replace_season("2324", training_rows(), "test", "initial")
            revision = repo.revision()
            empty = Path(directory) / "empty.csv"
            empty.write_text("Date,HomeTeam,AwayTeam,FTHG,FTAG\n")
            with self.assertRaises(ValueError):
                import_file(repo, empty, "2324")
            self.assertEqual(len(repo.matches()), 180)
            self.assertEqual(revision, repo.revision())

    def test_failed_transaction_preserves_existing_season(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(Path(directory) / "test.sqlite3")
            repo.initialize()
            repo.replace_season("2324", training_rows(), "test", "initial")
            duplicated = [training_rows()[0]] * 2
            import sqlite3
            with self.assertRaises(sqlite3.IntegrityError):
                repo.replace_season("2324", duplicated, "bad", "bad")
            self.assertEqual(len(repo.matches()), 180)

    def test_training_snapshot_has_strict_cutoff_and_source_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(Path(directory) / "test.sqlite3")
            repo.initialize()
            repo.replace_season("2324", training_rows(), "test", "initial")
            rows, revision = repo.snapshot(date(2023, 9, 1))
            self.assertEqual(revision, repo.revision())
            self.assertEqual(len(rows), 31)
            self.assertTrue(all(row["date"] < "2023-09-01" for row in rows))


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.temp.name))
        self.repo = Repository(self.settings.database)
        self.repo.initialize()
        self.repo.replace_season("2324", training_rows(), "test", "fixture")
        self.client = TestClient(create_app(self.settings))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def forecast(self, **overrides):
        return self.client.post("/api/forecast", json={"home": "North", "away": "South", "as_of": "2024-02-01", **overrides})

    def test_functional_forecast_persists_and_records_provenance(self):
        result = self.forecast()
        self.assertEqual(result.status_code, 200)
        body = result.json()
        self.assertTrue(body["historical_scenario"])
        self.assertFalse(body["fixture_verified"])
        self.assertEqual(body["dataset_revision"], self.repo.revision())
        self.assertIsNone(body["market"])
        self.assertEqual(self.repo.forecast(body["id"])["probabilities"], body["probabilities"])
        self.assertEqual(len(self.client.get("/api/forecasts").json()["forecasts"]), 1)
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_invalid_teams_or_odds_return_422(self):
        invalid = [{"away": "North"}, {"away": "Missing"}, {"odds": {"home": 2}},
                   {"odds": {"home": 2, "draw": 3, "away": 4}},
                   {"as_of": "2023-08-02"}]
        for values in invalid:
            with self.subTest(values=values):
                self.assertEqual(self.forecast(**values).status_code, 422)

    def test_current_manual_market_math_and_provenance(self):
        # Current-date fixture data is used solely in this isolated test DB.
        rows = training_rows()
        offset = date.today() - date(2024, 2, 1)
        for row in rows:
            row["date"] = (date.fromisoformat(row["date"]) + offset).isoformat()
        self.repo.replace_season("2324", rows, "test", "shifted")
        result = self.forecast(as_of=date.today().isoformat(), odds={"home": 2, "draw": 3, "away": 4})
        self.assertEqual(result.status_code, 200)
        market = result.json()["market"]
        self.assertAlmostEqual(sum(market["probabilities"].values()), 1)
        self.assertAlmostEqual(market["overround"], 1 / 2 + 1 / 3 + 1 / 4 - 1)
        self.assertFalse(market["profitability_verified"])
        self.assertIn("manual", market["source"])
        self.assertEqual(market["observed_at"], result.json()["generated_at"])
        invalid = self.forecast(as_of=date.today().isoformat(), odds={"home": 0, "draw": 3, "away": 4})
        self.assertEqual(invalid.status_code, 422)

    def test_changed_data_invalidates_backtest(self):
        save_report({"dataset_revision": self.repo.revision()}, self.settings.report)
        self.assertTrue(self.client.get("/api/backtest").json()["available"])
        self.repo.replace_season("2324", training_rows(), "test", "different-digest")
        self.assertFalse(self.client.get("/api/backtest").json()["available"])

    def test_qwen_is_optional_and_page_is_served(self):
        self.assertEqual(self.client.post("/api/explain", json={"forecast_id": "missing"}).status_code, 503)
        self.assertFalse(self.client.get("/api/status").json()["qwen"]["configured"])
        self.assertIn("Matchlab", self.client.get("/").text)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)

    def test_health_not_ready_without_training_data(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(Settings(Path(directory)))) as client:
                self.assertEqual(client.get("/api/health").status_code, 503)


class BacktestTests(unittest.TestCase):
    def test_real_holdout_count_and_month_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(Path(directory) / "test.sqlite3")
            repo.initialize()
            rows = training_rows()
            repo.replace_season("2324", rows, "test", "fixture")
            holdout = [dict(date="2024-08-10", season="2425", home="North", away="South", home_goals=1, away_goals=0),
                       dict(date="2024-08-11", season="2425", home="East", away="West", home_goals=0, away_goals=0),
                       dict(date="2024-08-12", season="2425", home="New", away="West", home_goals=5, away_goals=0)]
            repo.replace_season("2425", holdout, "test", "holdout")
            report = run_backtest(repo, ["2425"])
            self.assertEqual(report["model"]["matches"], 2)
            self.assertEqual(report["skipped_matches"], 1)
            self.assertEqual(report["selected_matches"], 3)
            self.assertLess(report["monthly"][0]["training_last_match"], "2024-08-01")
            self.assertFalse(report["market_comparison"]["available"])
            self.assertFalse(report["profitability_verified"])
            self.assertEqual(sum(b["count"] for b in report["calibration"]), 6)
            changed = [dict(row, home_goals=20) for row in holdout]
            repo.replace_season("2425", changed, "test", "changed")
            next_report = run_backtest(repo, ["2425"])
            # Predictions/calibration x-coordinates stay fixed when test results change.
            self.assertEqual([b["predicted"] for b in report["calibration"]], [b["predicted"] for b in next_report["calibration"]])

    def test_zero_test_run_cannot_pass(self):
        with self.assertRaises(ValueError):
            metrics([], [])

    def test_metric_definition(self):
        self.assertEqual(metrics([[1, 0, 0]], [0])["brier"], 0)
        self.assertEqual(metrics([[0, 1, 0]], [0])["brier"], 2)


class QwenTests(unittest.TestCase):
    def test_forwarded_openai_endpoint_and_probabilities(self):
        settings = Settings(Path("/unused"), "http://127.0.0.1:8000/v1", "local-model", "private-test-key")
        forecast = {"probabilities": {"home": .5, "draw": .2, "away": .3}}
        mock = MagicMock()
        mock.post.return_value = httpx.Response(200, json={"choices": [{"message": {"content": "<think>hidden</think>Статистический комментарий"}}]}, request=httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions"))
        with patch("matchlab.qwen.httpx.Client") as client:
            client.return_value.__enter__.return_value = mock
            result = explain(settings, forecast, "Непроверенная заметка")
        self.assertEqual(result, "Статистический комментарий")
        args, kwargs = mock.post.call_args
        self.assertEqual(args[0], "http://127.0.0.1:8000/v1/chat/completions")
        self.assertEqual(json.loads(kwargs["json"]["messages"][1]["content"])["forecast"], forecast)
        self.assertEqual(kwargs["json"]["model"], "local-model")

    def test_upstream_error_does_not_expose_response_or_key(self):
        settings = Settings(Path("/unused"), "http://127.0.0.1:8000/v1", "local-model", "private-test-key")
        mock = MagicMock()
        mock.post.return_value = httpx.Response(401, text="private-test-key", request=httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions"))
        with patch("matchlab.qwen.httpx.Client") as client:
            client.return_value.__enter__.return_value = mock
            with self.assertRaisesRegex(ValueError, "HTTP 401") as error:
                explain(settings, {})
        self.assertNotIn("private-test-key", str(error.exception))


if __name__ == "__main__":
    unittest.main()
