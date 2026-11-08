import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from dotenv import dotenv_values

from matchlab.api import create_app
from matchlab.bookmakers import FORMAT, normalize_capture
from matchlab.config import Settings
from matchlab.server_scraper import MinuteCollector, Publisher, local_api, run
from matchlab.storage import Repository
from matchlab.worker_protocol import parse_sources, source_state, validate_status
from tests.test_bookmakers import FROZEN, sample

ROOT = Path(__file__).resolve().parent.parent
KEY = "test-only-collector-key-not-a-secret-123"
FEEDS = {"betboom": "wss://ru-ws.sporthub.bet/api/tree_ws/v1", "winline": "wss://wss.winline.ru/data_ng"}


def connected_sample():
    document = sample()
    document["transport"] = {source: [{"url": url, "connected": True, "last_frame_at": FROZEN.isoformat()}] for source, url in FEEDS.items()}
    return document


def football_page():
    # Synthetic layout contract, not a recording of the live football route.
    return {"source": "winline", "kind": "public_dom",
            "url": "https://winline.ru/stavki/sport/futbol/", "observed_at": FROZEN.isoformat(),
            "payload": {"text": "Футбол\nП1\nX\nП2\nТест Хозяева\nТест Гости\n2.10\n3.20\n3.40\nСегодня 21:00\n+100",
                        "nodes": [{"tag": "a", "attrs": {"href": "https://winline.ru/stavki/event/12345678"},
                                   "text": "Тест Хозяева\nТест Гости",
                                   "winline_card": {"text": "Тест Хозяева\nТест Гости\nСегодня 21:00\nМатч\n2.10\n3.20\n3.40",
                                                    "tournament": "", "datetimes": []}}]}}


class LocalPublisher:
    def __init__(self, client):
        self.client = client
        self.calls = []
        self.fail_ingest = False

    async def request(self, method, path, body=None):
        if self.fail_ingest and path.endswith("/ingest"):
            raise ValueError("test failure")
        response = self.client.request(method, path, json=body, headers={"X-Matchlab-Collector-Key": KEY})
        self.calls.append((path, deepcopy(body)))
        response.raise_for_status()
        return response.json()


class ServerCollectorTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(self.temp)
        self.client = self.stack.enter_context(TestClient(create_app(Settings(self.path, bookmaker_ingest_token=KEY)), client=("127.0.0.1", 1000)))
        self.repo = Repository(self.path / "matchlab.sqlite3")
        for module in ("storage", "worker_protocol", "server_scraper", "api"):
            clock = self.stack.enter_context(patch(f"matchlab.{module}.datetime", wraps=datetime))
            clock.now.return_value = FROZEN
        self.publisher = LocalPublisher(self.client)
        self.worker = MinuteCollector(self.publisher, 60, self.path / "worker")
        self.stack.enter_context(patch("sys.stdout", new=io.StringIO()))

    def tearDown(self):
        self.stack.close()

    def test_real_sample_publishes_one_snapshot_per_event_and_keeps_history(self):
        async def run():
            await self.worker.bootstrap()
            await self.worker.process(connected_sample())
            self.assertEqual(self.worker.last_status["state"], "ok")
            self.assertEqual(self.worker.complete_batches, 1)
            quotes = self.repo.bookmaker_quotes()
            self.assertEqual(len(quotes), 22)
            status = self.repo.bookmaker_status()
            self.assertEqual(sum(item["snapshots"] for item in status["sources"]), 22)
            self.assertTrue(status["worker"]["heartbeat_recent"])
            self.assertIsNotNone(status["worker"]["last_success_at"])
            await self.worker.process(connected_sample())
            self.assertEqual(sum(item["snapshots"] for item in self.repo.bookmaker_status()["sources"]), 22)
        asyncio.run(run())

    def test_disconnected_or_blocked_site_never_restamps_frozen_dom(self):
        async def run():
            await self.worker.bootstrap()
            await self.worker.process(connected_sample())
            before = self.repo.bookmaker_status()
            failed = connected_sample()
            for connections in failed["transport"].values():
                connections[0]["connected"] = False
            failed["navigation"] = {source: {"status": 403} for source in FEEDS}
            for record in failed["records"]:
                record["observed_at"] = FROZEN.isoformat()
            await self.worker.process(failed)
            status = self.repo.bookmaker_status()
            self.assertEqual(status["sources"], before["sources"])
            self.assertEqual(status["worker"]["state"], "blocked")
            self.assertEqual(status["worker"]["last_success_at"], before["worker"]["last_success_at"])
        asyncio.run(run())

    def test_winline_can_use_cached_football_identity_without_reemitting_betboom_prices(self):
        async def run():
            await self.worker.process(connected_sample())
            # Restart the daemon: identities are recovered from the receiver.
            restarted = MinuteCollector(self.publisher, 60, self.path / "worker2")
            await restarted.bootstrap()
            partial = connected_sample()
            partial["transport"]["betboom"][0]["connected"] = False
            partial["navigation"] = {"betboom": {"status": 403}}
            await restarted.process(partial)
            posted = [body for path, body in self.publisher.calls if path.endswith("/ingest")][-1]
            self.assertEqual({row["source"] for row in posted["snapshots"]}, {"winline"})
            self.assertEqual(restarted.last_status["state"], "partial")
            self.assertEqual(restarted.last_status["sources"]["winline"]["events"], 7)
            self.assertEqual(restarted.complete_batches, 0)
        asyncio.run(run())

    def test_winline_only_works_without_betboom_quotes_and_keeps_unknown_league_explicit(self):
        async def check():
            worker = MinuteCollector(self.publisher, 60, self.path / "winline", sources=("winline",))
            await worker.bootstrap()
            document = connected_sample()
            document["records"] = [football_page()]
            document["transport"].pop("betboom")
            await worker.process(document)
            self.assertEqual(worker.last_status["state"], "ok")
            self.assertEqual(set(worker.last_status["sources"]), {"winline"})
            self.assertEqual(worker.complete_batches, 1)
            quotes = self.repo.bookmaker_quotes()
            self.assertEqual(len(quotes), 1)
            self.assertEqual(quotes[0]["competition"], "Турнир не указан")
            self.assertEqual(quotes[0]["competition_origin"], "winline_football_page")
            self.assertFalse(self.client.get("/api/bookmakers").json()["quotes"][0]["prediction_available"])
            # Losing the selected source still fails; the skip is not a bypass.
            document["transport"] = {}
            document["navigation"] = {"winline": {"status": 403}}
            await worker.process(document)
            self.assertEqual(worker.last_status["state"], "blocked")
            self.assertEqual(len(self.repo.bookmaker_quotes()), 1)
        asyncio.run(check())

    def test_football_page_scope_never_transfers_to_homepage_or_other_sports(self):
        record = football_page()
        for url in ("https://winline.ru/", "https://winline.ru/stavki/sport/hokkey/",
                    "https://example.test/stavki/sport/futbol/"):
            record["url"] = url
            self.assertEqual(normalize_capture({"format": "matchlab.bookmaker-capture.v1", "records": [record]})["snapshots"], [])

    def test_winline_only_preflight_opens_only_the_selected_source(self):
        async def fake_browser(sources, seconds, **kwargs):
            self.assertEqual(sources, ["winline"])
            self.assertEqual(kwargs["entrypoints"]["winline"], "https://winline.ru/stavki")
            self.assertTrue(kwargs["winline_prematch"])
            document = connected_sample()
            document["records"] = [football_page()]
            await kwargs["on_batch"](document)
        args = SimpleNamespace(api_url="http://127.0.0.1:8080", interval=60, sources=("winline",),
                               state_dir=self.path / "preflight-winline", once=True, sample_seconds=30, executable=None)
        with patch("matchlab.server_scraper.Publisher", return_value=self.publisher), \
                patch("matchlab.server_scraper.collect", side_effect=fake_browser), \
                patch.dict("os.environ", {"BOOKMAKER_INGEST_TOKEN": KEY}):
            self.assertEqual(asyncio.run(run(args)), 0)

    def test_source_selection_rejects_empty_duplicate_and_unknown_sources(self):
        self.assertEqual(parse_sources("winline"), ("winline",))
        self.assertEqual(parse_sources("winline,betboom"), ("betboom", "winline"))
        for value in ("", "winline,winline", "other", "winline,", "winline\nBOOKMAKER_API_URL=other"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_sources(value)

    def test_empty_or_failed_upload_does_not_claim_success(self):
        async def run():
            await self.worker.bootstrap()
            empty = connected_sample()
            empty["records"] = []
            await self.worker.process(empty)
            self.assertEqual(self.worker.last_status["state"], "error")
            self.assertEqual(self.worker.last_status["sources"]["winline"]["state"], "empty")
            self.publisher.fail_ingest = True
            with self.assertRaises(ValueError):
                await self.worker.process(connected_sample())
            self.assertEqual(self.repo.bookmaker_status()["worker"]["state"], "error")
            self.assertIsNone(self.repo.bookmaker_status()["worker"]["last_success_at"])
            self.assertEqual(self.repo.bookmaker_quotes(), [])
        asyncio.run(run())

    def test_preflight_fails_if_last_batch_loses_access_and_browser_does_not_inherit_keys(self):
        async def fake_browser(sources, seconds, **kwargs):
            self.assertNotIn("BOOKMAKER_INGEST_TOKEN", kwargs["browser_env"])
            self.assertNotIn("QWEN_API_KEY", kwargs["browser_env"])
            await kwargs["on_batch"](connected_sample())
            failed = connected_sample()
            failed["transport"] = {}
            failed["navigation"] = {source: {"status": 403} for source in FEEDS}
            await kwargs["on_batch"](failed)
        args = SimpleNamespace(api_url="http://127.0.0.1:8080", interval=10,
                               state_dir=self.path / "preflight", once=True, sample_seconds=30, executable=None)
        with patch("matchlab.server_scraper.Publisher", return_value=self.publisher), \
                patch("matchlab.server_scraper.collect", side_effect=fake_browser), \
                patch.dict("os.environ", {"BOOKMAKER_INGEST_TOKEN": KEY, "QWEN_API_KEY": "test-placeholder"}):
            self.assertEqual(asyncio.run(run(args)), 2)
        self.assertEqual(self.repo.bookmaker_status()["worker"]["state"], "blocked")

    def test_receiver_auth_size_and_invalid_batches_preserve_data(self):
        headers = {"X-Matchlab-Collector-Key": KEY}
        body = normalize_capture(sample())
        self.assertEqual(self.client.post("/api/bookmakers/ingest", json=body).status_code, 401)
        self.assertEqual(self.client.post("/api/bookmakers/ingest", json=body, headers=headers).status_code, 200)
        before = self.repo.bookmaker_status()["sources"]
        self.assertEqual(self.client.post("/api/bookmakers/ingest", json={"format": FORMAT, "snapshots": []}, headers=headers).status_code, 422)
        self.assertEqual(self.client.post("/api/bookmakers/ingest", content="x" * (2 * 1024 * 1024 + 1), headers=headers).status_code, 413)
        self.assertEqual(self.client.post("/api/bookmakers/ingest", content="[", headers=headers).status_code, 400)
        self.assertEqual(self.repo.bookmaker_status()["sources"], before)
        with TestClient(create_app(Settings(self.path, bookmaker_ingest_token=KEY)), client=("203.0.113.8", 2000)) as external:
            self.assertEqual(external.post("/api/bookmakers/ingest", json=body, headers=headers).status_code, 403)
        with TestClient(create_app(Settings(self.path)), client=("127.0.0.1", 2000)) as disabled:
            self.assertEqual(disabled.post("/api/bookmakers/ingest", json=body, headers=headers).status_code, 503)

    def test_missing_socket_stale_socket_or_analytics_socket_cannot_confirm_freshness(self):
        document = connected_sample()
        self.assertEqual(source_state(document, "betboom"), "ok")
        item = document["transport"]["betboom"][0]
        item["last_frame_at"] = (FROZEN - timedelta(minutes=4)).isoformat()
        self.assertEqual(source_state(document, "betboom"), "disconnected")
        item.pop("last_frame_at")
        self.assertEqual(source_state(document, "betboom"), "disconnected")
        item.update(url="wss://ru-ws.sporthub.bet/analytics", last_frame_at=FROZEN.isoformat())
        self.assertEqual(source_state(document, "betboom"), "disconnected")

    def test_status_has_no_free_form_payload_and_detects_stopped_heartbeat(self):
        status = {"state": "ok", "interval_seconds": 60, "sources": {}, "key": "must-be-discarded"}
        self.assertNotIn("key", validate_status(status))
        self.repo.save_worker_status(status)
        with patch("matchlab.storage.datetime", wraps=datetime) as clock:
            clock.now.return_value = FROZEN + timedelta(minutes=4)
            self.assertFalse(self.repo.bookmaker_status()["worker"]["heartbeat_recent"])
        for changes in ({"state": "anything"}, {"state": []}, {"interval_seconds": True}, {"sources": {"unknown": {}}},
                        {"sources": {"betboom": {"state": {}}}}):
            with self.assertRaises(ValueError):
                validate_status({**status, **changes})


class InstallerTests(unittest.TestCase):
    def test_current_vps_memory_is_rejected_before_mutations_and_swap_does_not_count(self):
        with tempfile.TemporaryDirectory() as folder:
            info = Path(folder) / "meminfo"
            info.write_text("MemTotal: 984064 kB\nMemAvailable: 202752 kB\nSwapFree: 1153433 kB\n")
            result = subprocess.run(["bash", "scripts/install_server_scraper.sh", "--dry-run", "--meminfo", str(info)], cwd=ROOT, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("доступно 198 МиБ", result.stderr)
            info.write_text("MemAvailable: 2097152 kB\n")
            result = subprocess.run(["bash", "scripts/install_server_scraper.sh", "--dry-run", "--meminfo", str(info)], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_receiver_setup_preserves_qwen_settings_is_private_and_reuses_key(self):
        spec = importlib.util.spec_from_file_location("receiver_setup", ROOT / "scripts/configure_bookmaker_receiver.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app_env, token = root / ".env", root / "token.env"
            original = "# local config\nQWEN_MODEL=example-model\nQWEN_API_KEY=test-placeholder\nQWEN_BASE_URL=http://127.0.0.1:23333/v1\n"
            app_env.write_text(original)
            module.configure(app_env, token)
            configured = app_env.read_text()
            self.assertTrue(configured.startswith(original))
            self.assertEqual(app_env.stat().st_mode & 0o777, 0o600)
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)
            self.assertEqual(set(dotenv_values(token)), {"BOOKMAKER_INGEST_TOKEN"})
            self.assertEqual(len(list((root / ".local/receiver-backups").iterdir())), 1)
            module.configure(app_env, token)
            self.assertEqual(app_env.read_text(), configured)
            self.assertEqual(len(list((root / ".local/receiver-backups").iterdir())), 1)
            token.write_text("UNRELATED=value\n")
            with self.assertRaises(ValueError):
                module.configure(app_env, token)
            self.assertEqual(app_env.read_text(), configured)

    def test_worker_publisher_accepts_only_loopback_and_never_embedded_credentials(self):
        self.assertEqual(local_api("http://127.0.0.1:18081/"), "http://127.0.0.1:18081")
        for url in ("http://example.com", "https://127.0.0.1", "http://user:password@127.0.0.1", "http://127.0.0.1/api", "http://127.0.0.1?token=test"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                local_api(url)
        with self.assertRaises(ValueError):
            Publisher("http://127.0.0.1:8080", "")
