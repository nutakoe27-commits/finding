"""Exercise the actual source SQLite -> authenticated receiver -> UI API bridge."""

import json
import tempfile
import unittest
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from test_archive import TOKEN, header, result

from matchlab.api import create_app
from matchlab.config import Settings
from winline.session import SourceSession
from winline.storage import connect, now, save_results, save_snapshot
from winline.sync import cycle


class ArchiveSyncTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.root / "winline.sqlite3"
        self.db = connect(self.path)
        self.stack.callback(self.db.close)
        self.capture = self.db.execute(
            "INSERT INTO captures(started_at,url) VALUES (?,?)",
            (now(), "https://winline.ru/"),
        ).lastrowid
        self.db.commit()
        h = header(live=True)
        line = {
            "id": 10,
            "idEvent": 101,
            "isLive": 1,
            "idTipMarket": 1,
            "V": [1.9, 3.4, 4.2],
            "_display": {
                "valid": True,
                "name": "Исход 1X2 (осн.время)",
                "labels": ["1", "X", "2"],
            },
        }
        self.data = {
            "sports": [{"id": 1, "name": "Футбол"}],
            "countries": [],
            "championships": [],
            "lines": [line],
            "events": [
                {
                    "id": 101,
                    "idSport": 1,
                    "isLive": 1,
                    "members": h["members"],
                    "date": h["kickoff_at"],
                    "score": "0:0",
                    "_main_line": line,
                }
            ],
        }
        save_snapshot(self.db, self.capture, self.data)
        self.client = self.stack.enter_context(
            TestClient(
                create_app(Settings(self.root / "vps", bookmaker_ingest_token=TOKEN)),
                client=("127.0.0.1", 1000),
                headers={"X-Matchlab-Collector-Key": TOKEN},
            )
        )

    def tearDown(self):
        self.stack.close()

    def test_replicates_all_events_results_selected_markets_and_replays_without_duplicates(
        self,
    ):
        row = result()["data"]
        save_results(self.db, self.capture, 1, 0, [row])
        state = {}
        first = cycle(self.client, self.path, self.root, state)
        self.assertEqual(first["cursor"], 1)
        self.assertEqual(self.client.get("/api/archive/results").json()["total"], 1)
        self.assertEqual(
            self.client.post("/api/archive/events/101/watch", json={}).status_code, 200
        )
        self.data["events"][0]["score"] = "2:1"
        self.data["lines"][0]["V"][0] = 2.4
        save_snapshot(self.db, self.capture, self.data)
        corrected = deepcopy(row)
        corrected["score"] = "3:0"
        save_results(self.db, self.capture, 1, 0, [corrected])
        second = cycle(self.client, self.path, self.root, state)
        self.assertEqual(second["cursor"], 2)
        self.assertEqual(
            json.loads((self.root / "requests.json").read_text())["events"], [101]
        )
        doc = self.client.get("/api/archive/events/101/markets").json()
        self.assertEqual(doc["live_score"], "2:1")
        self.assertEqual(doc["headline"]["outcomes"][0]["odds"], 2.4)
        cycle(self.client, self.path, self.root, state)
        self.assertEqual(self.client.get("/api/archive/results").json()["total"], 1)
        self.assertEqual(
            self.client.get("/api/archive/results").json()["results"][0]["score"], "3:0"
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM result_history").fetchone()[0], 2
        )
        self.assertEqual(
            json.loads((self.root / "sync-state.json").read_text())["cursor"], 2
        )

    def test_source_tick_keeps_observations_and_selected_refreshes_moving_between_maintenance_requests(
        self,
    ):
        (self.root / "requests.json").write_text(
            json.dumps({"at": 1000, "events": [101]})
        )
        page = Mock()
        page.evaluate.side_effect = lambda script, *args: (
            json.dumps(self.data) if not args else None
        )
        session = SourceSession(page, self.db, self.capture, self.path)
        with (
            patch("winline.session.time.monotonic", return_value=100),
            patch("winline.session.time.time", return_value=1001),
        ):
            session.tick()
        self.data["events"][0]["score"] = "1:0"
        with patch("winline.session.time.monotonic", return_value=105):
            self.assertIsNone(session.tick())
        with (
            patch("winline.session.time.monotonic", return_value=111),
            patch("winline.session.time.time", return_value=1011),
        ):
            session.tick()
        self.assertEqual(
            self.db.execute("SELECT score FROM events WHERE id=101").fetchone()[0],
            "1:0",
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM snapshots").fetchone()[0], 3
        )
