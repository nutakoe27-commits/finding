from datetime import date, timedelta
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
import httpx

from account_helpers import signed_in

from matchlab.api import create_app
from matchlab.backtest import metrics, run_backtest, save_report
from matchlab.config import Settings
from matchlab.data import import_file, parse_csv
from matchlab.model import GoalModel
from matchlab.qwen import check_connection, explain
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
        signed_in(self.client,Repository(self.settings.database))

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
        self.assertIn("prognozich ai", self.client.get("/").text)
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
    def response(self, body, status=200):
        return httpx.Response(status, json=body, request=httpx.Request("POST", "http://127.0.0.1:23333/v1/chat/completions"))

    def invoke(self, response, settings=None):
        settings = settings or Settings(Path("/unused"), "http://127.0.0.1:23333/v1", "local-model")
        with patch("matchlab.qwen.httpx.Client") as client:
            mock = client.return_value.__enter__.return_value
            mock.post.return_value = response
            result = explain(settings, {"probabilities": {"home": .5, "draw": .2, "away": .3}})
            return result, mock.post.call_args

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
        self.assertNotIn("chat_template_kwargs", kwargs["json"])

    def test_vllm_thinking_can_be_disabled_explicitly(self):
        settings = Settings(Path("/unused"), "http://127.0.0.1:23333/v1", "local-model", qwen_enable_thinking=False, qwen_max_tokens=2048)
        result, args = self.invoke(self.response({"choices": [{"message": {"content": "Ответ"}, "finish_reason": "stop"}]}), settings)
        self.assertEqual(result, "Ответ")
        self.assertEqual(args.kwargs["json"]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(args.kwargs["json"]["max_tokens"], 2048)
        self.assertFalse(args.kwargs["json"]["stream"])

    def test_reasoning_only_response_has_actionable_error(self):
        with self.assertRaisesRegex(ValueError, "QWEN_ENABLE_THINKING=false"):
            self.invoke(self.response({"choices": [{"message": {"content": None, "reasoning_content": "private reasoning"}, "finish_reason": "stop"}]}))

    def test_token_exhaustion_is_distinct_from_connection_failure(self):
        with self.assertRaisesRegex(ValueError, "лимита токенов"):
            self.invoke(self.response({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}))

    def test_invalid_json_is_distinct_from_wrong_schema(self):
        html = httpx.Response(200, text="<html>private-test-key</html>", request=httpx.Request("POST", "http://127.0.0.1:23333/v1/chat/completions"))
        with self.assertRaisesRegex(ValueError, "не JSON") as error:
            self.invoke(html)
        self.assertNotIn("private-test-key", str(error.exception))
        with self.assertRaisesRegex(ValueError, "не содержит сообщение"):
            self.invoke(self.response({"result": "not a chat completion"}))

    def test_incomplete_thinking_is_not_exposed(self):
        with self.assertRaisesRegex(ValueError, "только рассуждения") as error:
            self.invoke(self.response({"choices": [{"message": {"content": "<think>private reasoning"}}]}))
        self.assertNotIn("private reasoning", str(error.exception))

    def test_http_200_error_envelopes_are_not_reported_as_endpoint_incompatibility(self):
        for body in ({'error': {'type': 'InternalServerError', 'message': 'EngineDeadError private-test-key'}},
                     {'object': 'error', 'type': 'InternalServerError', 'message': 'private-test-key', 'code': 500}):
            with self.subTest(body_type=tuple(body)), self.assertRaisesRegex(ValueError, 'объект ошибки') as error:
                self.invoke(self.response(body))
            self.assertNotIn('private-test-key', str(error.exception))

    def test_connection_failure_is_sanitized_and_identified(self):
        settings = Settings(Path("/unused"), "http://host.docker.internal:23333/v1", "local-model")
        with patch("matchlab.qwen.httpx.Client") as client:
            client.return_value.__enter__.return_value.post.side_effect = httpx.ConnectError("DNS error; private-test-key")
            with self.assertRaisesRegex(ValueError, "127.0.0.1") as error:
                explain(settings, {})
        self.assertNotIn("private-test-key", str(error.exception))

    def test_url_credentials_are_rejected_without_echoing_them(self):
        for url in ("http://user:private-test-key@localhost:23333/v1", "http://localhost:23333/v1?key=private-test-key", "localhost:23333/v1"):
            with self.subTest(url=url), patch("matchlab.qwen.httpx.Client") as client:
                with self.assertRaises(ValueError) as error:
                    explain(Settings(Path("/unused"), url, "local-model"), {})
                self.assertNotIn("private-test-key", str(error.exception))
                client.assert_not_called()

    def test_diagnostics_require_both_discovery_and_real_completion(self):
        settings = Settings(Path("/unused"), "http://127.0.0.1:23333/v1", "local-model", "private-test-key", qwen_enable_thinking=False)
        with patch("matchlab.qwen.httpx.Client") as client:
            mock = client.return_value.__enter__.return_value
            mock.get.return_value = self.response({"data": [{"id": "local-model"}]})
            mock.post.return_value = self.response({"choices": [{"message": {"content": "ОК"}, "finish_reason": "stop"}]})
            report = check_connection(settings)
            self.assertEqual(report["chat_completions"], "ok")
            self.assertEqual(report["response_characters"], 2)
            self.assertTrue(mock.get.called)
            self.assertTrue(mock.post.called)
            self.assertNotIn("private-test-key", json.dumps(report))

    def test_wrong_model_is_diagnosed_before_generation(self):
        settings = Settings(Path("/unused"), "http://127.0.0.1:23333/v1", "wrong-model")
        with patch("matchlab.qwen.httpx.Client") as client:
            mock = client.return_value.__enter__.return_value
            mock.get.return_value = self.response({"data": [{"id": "actual-model"}]})
            with self.assertRaisesRegex(ValueError, "QWEN_MODEL не найден"):
                check_connection(settings)
            mock.post.assert_not_called()

    def test_upstream_error_does_not_expose_response_or_key(self):
        settings = Settings(Path("/unused"), "http://127.0.0.1:8000/v1", "local-model", "private-test-key")
        mock = MagicMock()
        mock.post.return_value = httpx.Response(401, text="private-test-key", request=httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions"))
        with patch("matchlab.qwen.httpx.Client") as client:
            client.return_value.__enter__.return_value = mock
            with self.assertRaisesRegex(ValueError, "HTTP 401") as error:
                explain(settings, {})
        self.assertNotIn("private-test-key", str(error.exception))


class QwenSettingsTests(unittest.TestCase):
    def test_vllm_env_settings_are_parsed(self):
        with patch("matchlab.config.load_dotenv"), patch.dict("os.environ", {
            "QWEN_ENABLE_THINKING": "false", "QWEN_MAX_TOKENS": "2048",
        }, clear=True):
            settings = Settings.from_env()
            self.assertIs(settings.qwen_enable_thinking, False)
            self.assertEqual(settings.qwen_max_tokens, 2048)

    def test_invalid_thinking_value_is_rejected(self):
        with patch("matchlab.config.load_dotenv"), patch.dict("os.environ", {
            "QWEN_ENABLE_THINKING": "maybe",
        }, clear=True):
            with self.assertRaisesRegex(ValueError, "true или false"):
                Settings.from_env()


class QwenCLITests(unittest.TestCase):
    def test_cli_uses_real_http_and_does_not_create_database(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, payload):
                encoded = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                calls.append((self.path, None))
                self.reply({"data": [{"id": "test-vllm-model"}]})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, payload))
                self.reply({"choices": [{"message": {"content": "ОК"}, "finish_reason": "stop"}]})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory) / "should-not-exist"
                environment = dict(os.environ, QWEN_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1",
                                   QWEN_MODEL="test-vllm-model", QWEN_API_KEY="private-test-key",
                                   QWEN_ENABLE_THINKING="false", QWEN_MAX_TOKENS="2048",
                                   MATCHLAB_DATA_DIR=str(data_dir), NO_PROXY="127.0.0.1")
                import sys
                result = subprocess.run([sys.executable, "-m", "matchlab", "check-qwen"],
                                        env=environment, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["chat_completions"], "ok")
                self.assertNotIn("private-test-key", result.stdout + result.stderr)
                self.assertFalse(data_dir.exists())
                self.assertEqual([item[0] for item in calls], ["/v1/models", "/v1/chat/completions"])
                self.assertEqual(calls[1][1]["chat_template_kwargs"], {"enable_thinking": False})
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
