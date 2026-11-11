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
from winline.browser import SNAPSHOT
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

    def test_http_rejection_reports_operation_and_safe_reason_without_credentials(self):
        import httpx
        from winline.sync import checked_response,error_status
        request=httpx.Request('POST','http://localhost:18081/api/bookmakers/archive/ingest?token=PRIVATE_TOKEN',headers={'X-Matchlab-Collector-Key':'PRIVATE_KEY'})
        response=httpx.Response(422,request=request,headers={'X-Matchlab-Archive-Reason':'result_cursor_conflict'},json={'detail':'PRIVATE_BODY'})
        with self.assertRaises(httpx.HTTPStatusError) as raised:
            checked_response(response,'results')
        diagnostic=error_status(raised.exception)
        self.assertEqual(diagnostic['http_status'],422)
        self.assertEqual(diagnostic['operation'],'results')
        self.assertEqual(diagnostic['receiver_reason'],'result_cursor_conflict')
        self.assertNotIn('PRIVATE',json.dumps(diagnostic))
        response.headers['X-Matchlab-Archive-Reason']='PRIVATE_ARBITRARY_HEADER'
        self.assertNotIn('receiver_reason',error_status(raised.exception))

    def test_receiver_returns_a_safe_protocol_reason_without_echoing_payload(self):
        bad={'format':'matchlab.archive.v1','mode':'status','status':{'checked_at':now()}}
        response=self.client.post('/api/bookmakers/archive/ingest',json=bad)
        self.assertEqual(response.status_code,422)
        self.assertEqual(response.headers['X-Matchlab-Archive-Reason'],'invalid_counter')
        self.assertEqual(response.json()['detail'],'Некорректный пакет архива')

    def test_featured_order_reaches_receiver_and_clears_on_next_source_snapshot(self):
        self.data["events"][0]["_featured_rank"] = 5
        save_snapshot(self.db, self.capture, self.data)
        state = {}
        cycle(self.client, self.path, self.root, state)
        data = self.client.get("/api/bookmakers/featured-events").json()
        self.assertEqual([(e["source_event_id"],e["featured_rank"]) for e in data["events"]],[("101",5)])
        self.data["events"][0]["_featured_rank"] = None
        save_snapshot(self.db, self.capture, self.data)
        cycle(self.client, self.path, self.root, state)
        self.assertEqual(self.client.get("/api/bookmakers/featured-events").json()["events"],[])

    def test_future_result_no_longer_blocks_cursor_or_following_completed_results(self):
        from datetime import datetime,timedelta,timezone
        suspicious=result()['data'];suspicious['date']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        good=result()['data']
        save_results(self.db,self.capture,1,0,[suspicious,good])
        state={}
        first=cycle(self.client,self.path,self.root,state)
        self.assertEqual(first['state'],'ok')
        self.assertEqual((first['cursor'],first['pending']),(2,0))
        overview=self.client.get('/api/archive/status').json()
        self.assertTrue(overview['connected'])
        self.assertEqual(overview['quarantined_results'],1)
        self.assertEqual(overview['stored_results'],1)
        self.assertEqual(overview['sent_cursor'],2)
        # Repeating the cycle keeps the held row and does not duplicate results.
        cycle(self.client,self.path,self.root,state)
        corrected=deepcopy(good);corrected['score']='3:1'
        save_results(self.db,self.capture,1,0,[corrected])
        last=cycle(self.client,self.path,self.root,state)
        self.assertEqual(last['cursor'],3)
        self.assertEqual(self.client.get('/api/archive/results').json()['results'][0]['score'],'3:1')

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

    def test_light_ticks_preserve_pending_market_batches_until_the_next_observation(
        self,
    ):
        self.root.joinpath("requests.json").write_text(
            json.dumps({"at": 1000, "events": [101]})
        )
        page = Mock()
        options = []

        def evaluate(script, *args):
            if script == SNAPSHOT:
                options.append(args[0])
                return json.dumps(self.data)
            return None

        page.evaluate.side_effect = evaluate
        session = SourceSession(page, self.db, self.capture, self.path, light=True)
        with (
            patch("winline.session.time.monotonic", return_value=100),
            patch("winline.session.time.time", return_value=1001),
        ):
            session.tick(event_ids=[102])
        with patch("winline.session.time.monotonic", return_value=105):
            self.assertIsNone(session.tick(event_ids=[103, 104]))
        with (
            patch("winline.session.time.monotonic", return_value=111),
            patch("winline.session.time.time", return_value=1011),
        ):
            session.tick()
        self.assertEqual(
            options,
            [
                {"all_lines": False, "event_ids": [101, 102]},
                {"all_lines": False, "event_ids": [101, 103, 104]},
            ],
        )
        self.assertEqual(session.pending, set())
