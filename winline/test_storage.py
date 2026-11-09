"""Regression checks for data identity, history and missing observations."""

import copy
import tempfile
import unittest
from pathlib import Path

from winline.query import read_query
from winline.storage import connect, save_results, save_snapshot


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.sqlite3"
        self.db = connect(self.path)
        self.capture = self.db.execute(
            "INSERT INTO captures(started_at,url) VALUES ('2026-01-01','https://winline.ru/')"
        ).lastrowid
        self.db.commit()
        self.data = {
            "sports": [{"id": 1, "name": "Футбол"}],
            "countries": [],
            "championships": [],
            "events": [
                {
                    "id": 100,
                    "idSport": 1,
                    "isLive": 0,
                    "members": ["Команда А", "Команда Б"],
                    "date": "2026-01-02T10:00:00.000Z",
                }
            ],
            "lines": [
                {
                    "id": 10,
                    "idEvent": 100,
                    "isLive": 0,
                    "idTipMarket": 491,
                    "V": [1.9, 1.8],
                    "koef": "",
                    "tipLine": {"R": ["1", "2"], "freeTextR": "Исход"},
                }
            ],
        }

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def count(self, table):
        return self.db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def test_same_snapshot_does_not_duplicate_history_but_refreshes_seen(self):
        save_snapshot(self.db, self.capture, self.data, "2026-01-01T01:00:00+00:00")
        data = copy.deepcopy(self.data)
        data["events"][0]["meta"] = {"revision": 100}
        save_snapshot(self.db, self.capture, data, "2026-01-01T02:00:00+00:00")
        self.assertEqual(self.count("events"), 1)
        self.assertEqual(self.count("event_history"), 1)
        self.assertEqual(self.count("line_history"), 1)
        self.assertEqual(
            self.db.execute("SELECT last_seen FROM events").fetchone()[0],
            "2026-01-01T02:00:00+00:00",
        )

    def test_light_observation_preserves_unrelated_markets_and_chunked_identity_lookups(
        self,
    ):
        expanded = copy.deepcopy(self.data)
        expanded["events"].append(
            {"id": 200, "idSport": 1, "isLive": 0, "members": ["В", "Г"]}
        )
        expanded["lines"].extend(
            dict(self.data["lines"][0], idEvent=200, id=i) for i in range(600)
        )
        save_snapshot(self.db, self.capture, expanded, "2026-01-01T01:00:00+00:00")
        histories = self.count("line_history")
        current = copy.deepcopy(self.data)
        current["lines"][0]["V"] = [1.5, 2.5]
        save_snapshot(self.db, self.capture, current, "2026-01-01T02:00:00+00:00")
        self.assertEqual(self.count("lines"), 601)
        self.assertEqual(self.count("line_history"), histories + 1)
        self.assertEqual(
            self.db.execute(
                "SELECT last_seen FROM lines WHERE event_id=200 LIMIT 1"
            ).fetchone()[0],
            "2026-01-01T01:00:00+00:00",
        )
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM latest_odds WHERE present_in_latest_snapshot"
            ).fetchone()[0],
            1,
        )

    def test_live_transition_and_price_change_are_preserved(self):
        save_snapshot(self.db, self.capture, self.data)
        data = copy.deepcopy(self.data)
        data["events"][0].update(isLive=1, score="1:0")
        data["lines"][0].update(isLive=1, V=[1.2, 4.0])
        save_snapshot(self.db, self.capture, data)
        self.assertEqual(self.count("event_history"), 2)
        self.assertEqual(self.count("line_history"), 2)
        self.assertEqual(self.count("lines"), 2)
        self.assertEqual(
            self.db.execute("SELECT score FROM matches").fetchone()[0], "1:0"
        )
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM latest_odds WHERE present_in_latest_snapshot"
            ).fetchone()[0],
            1,
        )

    def test_disappearing_event_is_not_inferred_to_be_finished(self):
        save_snapshot(self.db, self.capture, self.data)
        data = copy.deepcopy(self.data)
        data["events"][0]["id"] = 200
        data["lines"] = []
        save_snapshot(self.db, self.capture, data)
        row = self.db.execute("SELECT * FROM matches WHERE id=100").fetchone()
        self.assertEqual(row["present_in_latest_snapshot"], 0)
        self.assertEqual(self.count("results"), 0)

    def test_line_id_is_scoped_to_event_and_live_mode(self):
        data = copy.deepcopy(self.data)
        data["events"].append(
            {"id": 200, "isLive": 0, "idSport": 1, "members": ["В", "Г"]}
        )
        data["lines"].append(dict(data["lines"][0], idEvent=200))
        save_snapshot(self.db, self.capture, data)
        self.assertEqual(self.count("lines"), 2)

    def test_result_score_correction_does_not_create_duplicate_match(self):
        row = {
            "idRadar": 555,
            "date": "2026-01-01T10:00:00.000Z",
            "member1": "А",
            "member2": "Б",
            "score": "1:0",
            "championship": "Лига",
        }
        save_results(self.db, self.capture, 1, 0, [row])
        save_results(self.db, self.capture, 1, 0, [row])
        save_results(self.db, self.capture, 1, 0, [dict(row, score="2:0")])
        self.assertEqual(self.count("results"), 1)
        self.assertEqual(self.count("result_history"), 2)
        self.assertEqual(
            self.db.execute("SELECT score FROM results").fetchone()[0], "2:0"
        )

    def test_empty_snapshot_is_rejected(self):
        with self.assertRaises(ValueError):
            save_snapshot(self.db, self.capture, dict(self.data, events=[]))

    def test_malformed_snapshot_rolls_back_without_poisoning_current_view(self):
        save_snapshot(self.db, self.capture, self.data)
        data = copy.deepcopy(self.data)
        data["events"][0]["members"] = ["Changed"]
        data["lines"].append({"id": 99})
        with self.assertRaises(KeyError):
            save_snapshot(self.db, self.capture, data)
        self.assertEqual(self.count("snapshots"), 1)
        self.assertEqual(self.count("event_history"), 1)

    def test_read_query_rejects_writes_and_searches_cyrillic(self):
        save_snapshot(self.db, self.capture, self.data)
        rows = read_query(
            "SELECT id FROM events WHERE instr(casefold(members_json),?)>0",
            ["команда"],
            self.path,
        )
        self.assertEqual(rows, [{"id": 100}])
        import sqlite3

        with self.assertRaises(sqlite3.OperationalError):
            read_query("DELETE FROM events", database=self.path)


if __name__ == "__main__":
    unittest.main()
