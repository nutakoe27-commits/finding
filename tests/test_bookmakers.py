from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import asyncio

from fastapi.testclient import TestClient

from matchlab.api import create_app
from matchlab.bookmakers import FORMAT, kickoff, load_snapshots, model_teams, normalize_capture, validate_snapshot
from matchlab.capture import Capture, safe_url
from matchlab.config import Settings
from matchlab.storage import Repository
from tests.test_matchlab import training_rows

FIXTURE = Path(__file__).parent / "fixtures/bookmakers_public_sample.json"
FROZEN = datetime(2026, 11, 8, 9, 30, tzinfo=timezone.utc)


def sample():
    return json.loads(FIXTURE.read_text())


class AdapterTests(unittest.TestCase):
    def test_recorded_betboom_and_winline_match_counts(self):
        document = normalize_capture(sample())
        self.assertEqual(document["report"]["sources"]["betboom"]["events"], 15)
        self.assertEqual(document["report"]["sources"]["winline"]["events"], 7)
        self.assertFalse(document["report"]["binary_feed_decoded"])
        self.assertTrue(all(row["market"] == "1x2_90m" for row in document["snapshots"]))

    def test_brighton_prices_and_moscow_time_match_actual_capture(self):
        rows = normalize_capture(sample())["snapshots"]
        selected = [row for row in rows if row["source"] == "betboom" and row["home"] == "Брайтон"]
        self.assertTrue(selected)
        for row in selected:
            self.assertEqual(row["odds"], {"home": 1.9, "draw": 3.85, "away": 3.75})
            self.assertEqual(row["kickoff_at"], "2026-11-08T14:00:00.000000+00:00")
            self.assertEqual(model_teams(row), ("Brighton", "Brentford"))
        self.assertTrue(any(row["source_event_id"] == "6300646" for row in selected))

    def test_early_win_and_half_time_are_not_substituted_for_90_minutes(self):
        original = sample()
        detail = next(r for r in original["records"] if r["url"].endswith("6300646"))
        self.assertIn("Досрочная победа", detail["payload"]["text"])
        self.assertIn("1-й тайм: Исход", detail["payload"]["text"])
        original["records"] = [detail]
        rows = normalize_capture(original)["snapshots"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["odds"]["home"], 1.9)
        changed = deepcopy(original)
        changed["records"][0]["payload"]["text"] = detail["payload"]["text"].replace("Исход\nОсновное время", "Другой рынок\nОсновное время", 1)
        self.assertFalse(normalize_capture(changed)["snapshots"])

    def test_mixed_winline_homepage_does_not_import_hockey_or_live(self):
        rows = normalize_capture(sample())["snapshots"]
        names = {row["home"] for row in rows if row["source"] == "winline"}
        self.assertNotIn("Нефтехимик", names)
        self.assertNotIn("Амур", names)
        self.assertNotIn("Юньнань Юкун", names)
        self.assertIn("Манчестер Юнайтед", names)
        no_reference = sample()
        no_reference["records"] = [r for r in no_reference["records"] if r["source"] == "winline"]
        self.assertFalse(normalize_capture(no_reference)["snapshots"])

    def test_market_sources_remain_separate(self):
        rows = normalize_capture(sample())["snapshots"]
        bb = next(r for r in rows if r["source"] == "betboom" and r["home"] == "Крылья Советов")
        wl = next(r for r in rows if r["source"] == "winline" and r["home"] == "Крылья Советов")
        self.assertEqual(bb["fixture_key"], wl["fixture_key"])
        self.assertNotEqual(bb["event_key"], wl["event_key"])
        self.assertEqual(bb["odds"]["home"], 2.55)
        self.assertEqual(wl["odds"]["home"], 2.5)
        self.assertIsNone(model_teams(bb))

    def test_non_epl_and_unknown_teams_have_no_epl_prediction(self):
        rows = normalize_capture(sample())["snapshots"]
        self.assertIsNone(model_teams(next(r for r in rows if r["home"] == "Ковентри Сити")))
        self.assertIsNone(model_teams(next(r for r in rows if r["home"] == "Наполи")))

    def test_relative_dates_month_names_and_year_boundary(self):
        self.assertEqual(kickoff("21 ноября в 15:30", "2026-11-08T09:00:00+00:00"), "2026-11-21T12:30:00.000000+00:00")
        self.assertTrue(kickoff("2 января в 12:00", "2026-12-31T10:00:00+00:00").startswith("2027-01-02"))
        with self.assertRaises(ValueError):
            kickoff("Сегодня в 10:00", "2026-11-08T09:00:00+00:00")

    def test_invalid_normalized_records_fail_closed(self):
        row = normalize_capture(sample())["snapshots"][0]
        invalid = [{"odds": {"home": float("nan"), "draw": 2, "away": 3}}, {"odds": {"home": 1.9}},
                   {"kickoff_at": row["observed_at"]}, {"observed_at": "2026-11-08T09:00:00"},
                   {"url": "https://evil.example/"}, {"sport": "hockey"}, {"market": "1st_half"}]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_snapshot({**row, **fields})

    def test_empty_capture_does_not_count_as_scrape(self):
        with self.assertRaises(ValueError):
            load_snapshots({"format": "matchlab.bookmaker-capture.v1", "records": []})

    def test_signed_media_paths_are_redacted_and_private_ip_json_is_excluded(self):
        for url in ("https://example.test/media/hdntl=private-media-signature/index.m3u8",
                    "https://example.test/media/eyJabc.def.signature/asset.json"):
            redacted = safe_url(url)
            self.assertNotIn("private-media-signature", redacted)
            self.assertNotIn("eyJabc.def.signature", redacted)
        capture = Capture(["winline"])
        self.assertFalse(capture.add("winline", "http_json", "https://winline.ru/api/v2/getip", {"my_ip": "private-ip"}, False))


class BookmakerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.temp.name))
        self.repo = Repository(self.settings.database)
        self.repo.initialize()
        self.storage_clock = patch("matchlab.storage.datetime")
        self.storage_clock.start().now.return_value = FROZEN
        self.api_clock = patch("matchlab.api.datetime")
        self.api_clock.start().now.return_value = FROZEN
        rows = training_rows()
        mapping = {"North": "Brighton", "South": "Brentford", "East": "Sunderland", "West": "Chelsea"}
        for index, row in enumerate(rows):
            row.update(date=(datetime(2026, 1, 1) + timedelta(days=index)).date().isoformat(), season="2526",
                       home=mapping[row["home"]], away=mapping[row["away"]])
        self.repo.replace_season("2526", rows, "test training fixture", "training-revision")
        self.revision = self.repo.revision()
        self.result = self.repo.import_bookmakers(sample())
        self.client = TestClient(create_app(self.settings))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.api_clock.stop()
        self.storage_clock.stop()
        self.temp.cleanup()

    def test_import_is_idempotent_and_does_not_change_training_revision(self):
        self.assertGreater(self.result["inserted"], 0)
        second = self.repo.import_bookmakers(sample())
        self.assertEqual(second["inserted"], 0)
        self.assertTrue(second["already_imported"])
        self.assertEqual(self.repo.revision(), self.revision)
        self.assertEqual(len(self.repo.bookmaker_quotes()), 22)

    def test_invalid_batch_rolls_back_and_preserves_previous_data(self):
        before = self.repo.bookmaker_status()
        rows = normalize_capture(sample())["snapshots"][:2]
        rows[1]["odds"]["home"] = 0
        with self.assertRaises(ValueError):
            self.repo.import_bookmakers({"format": FORMAT, "snapshots": rows})
        self.assertEqual(self.repo.bookmaker_status(), before)
        with self.assertRaises(ValueError):
            self.repo.import_bookmakers({"format": FORMAT, "snapshots": []})

    def test_latest_quote_updates_without_mutating_history(self):
        row = next(r for r in self.repo.bookmaker_quotes() if r["source"] == "betboom" and r["home"] == "Брайтон")
        changed = {**row, "observed_at": "2026-11-08T09:29:00+00:00", "odds": {**row["odds"], "home": 2.05}}
        self.repo.import_bookmakers({"format": FORMAT, "snapshots": [changed]})
        self.assertEqual(self.repo.bookmaker_quote(row["id"])["odds"]["home"], 1.9)
        latest = next(r for r in self.repo.bookmaker_quotes() if r["event_key"] == row["event_key"])
        self.assertEqual(latest["odds"]["home"], 2.05)

    def test_future_observation_rejected(self):
        row = deepcopy(self.repo.bookmaker_quotes()[0])
        row["observed_at"] = "2026-11-08T10:00:00+00:00"
        row["kickoff_at"] = "2026-11-09T10:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "будущем"):
            self.repo.import_bookmakers({"format": FORMAT, "snapshots": [row]})

    def test_api_lists_prices_staleness_and_supported_teams(self):
        response = self.client.get("/api/bookmakers")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["quotes"]
        self.assertEqual(len(rows), 22)
        row = next(r for r in rows if r["home"] == "Брайтон")
        self.assertEqual(row["model_teams"], ["Brighton", "Brentford"])
        self.assertTrue(row["prediction_available"])
        self.assertTrue(row["is_stale"])
        self.assertFalse(next(r for r in rows if r["home"] == "Наполи")["prediction_available"])
        filtered = self.client.get("/api/bookmakers?source=winline").json()["quotes"]
        self.assertEqual(len(filtered), 7)

    def test_forecast_uses_saved_odds_and_never_trains_after_observation(self):
        row = next(r for r in self.repo.bookmaker_quotes() if r["source"] == "betboom" and r["home"] == "Брайтон")
        response = self.client.post("/api/forecast", json={"home": "Brighton", "away": "Brentford", "quote_id": row["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        forecast = response.json()
        self.assertEqual(forecast["market"]["source"], "betboom")
        self.assertEqual(forecast["market"]["odds"], row["odds"])
        self.assertEqual(forecast["market"]["observed_at"], row["observed_at"])
        self.assertEqual(forecast["training"]["before"], "2026-11-08")
        self.assertLess(forecast["training"]["last_match"], "2026-11-08")
        self.assertEqual(self.repo.forecast(forecast["id"])["quote_id"], row["id"])

    def test_mismatched_teams_manual_prices_and_started_matches_are_rejected(self):
        row = next(r for r in self.repo.bookmaker_quotes() if r["home"] == "Брайтон")
        base = {"home": "Brighton", "away": "Brentford", "quote_id": row["id"]}
        self.assertEqual(self.client.post("/api/forecast", json={**base, "away": "Chelsea"}).status_code, 422)
        self.assertEqual(self.client.post("/api/forecast", json={**base, "odds": row["odds"]}).status_code, 422)
        with patch("matchlab.api.datetime") as clock:
            clock.now.return_value = FROZEN + timedelta(days=1)
            self.assertEqual(self.client.post("/api/forecast", json=base).status_code, 409)


class SshTransportTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("scrape_bookmakers", Path(__file__).parent.parent / "scripts/scrape_bookmakers.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_ssh_transfer_uses_stdin_and_quotes_remote_directory(self):
        with patch.object(self.module.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"inserted":1,"events":1}'
            result = self.module.upload({"format": FORMAT, "snapshots": []}, "root@vps", "/root/project with space")
            args = run.call_args
            self.assertIn("BatchMode=yes", args.args[0])
            self.assertIn("cd '/root/project with space'", args.args[0][-1])
            self.assertIn("--stdin", args.args[0][-1])
            self.assertEqual(json.loads(args.kwargs["input"])["format"], FORMAT)
            self.assertEqual(result["inserted"], 1)

    def test_ssh_target_cannot_inject_options(self):
        with patch.object(self.module.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                self.module.upload({}, "-oProxyCommand=anything", "/root/matchlab")
            run.assert_not_called()

    def test_remote_import_failure_is_not_misreported_as_ssh_auth(self):
        with patch.object(self.module.subprocess, "run") as run:
            run.return_value.returncode = 1
            with self.assertRaisesRegex(ValueError, "импорта на VPS"):
                self.module.upload({}, "root@vps", "/root/matchlab")
            run.return_value.returncode = 255
            with self.assertRaisesRegex(ValueError, "подключиться по SSH"):
                self.module.upload({}, "root@vps", "/root/matchlab")

    def test_continuous_batches_keep_history_and_do_not_erase_on_empty_batch(self):
        async def simulated_collect(*args, **kwargs):
            await kwargs["on_batch"](sample())
            await kwargs["on_batch"]({"format": "matchlab.bookmaker-capture.v1", "records": []})
            await kwargs["on_batch"](sample())
        with tempfile.TemporaryDirectory() as directory, patch.object(self.module, "collect", simulated_collect):
            output = Path(directory) / "latest.json"
            args = SimpleNamespace(output=output, seconds=1, interval=30, executable=None, headless=True, ssh_target=None, remote_dir="/root/matchlab")
            result = asyncio.run(self.module.run(args))
            self.assertEqual(result, 0)
            self.assertTrue(json.loads(output.read_text())["snapshots"])
            self.assertEqual(len(output.with_suffix(".history.jsonl").read_text().splitlines()), 2)
