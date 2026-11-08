import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from matchlab.capture import Capture, FORMAT, redact, safe_url, save_document, sports_json, sports_text, summary


class CapturePrivacyTests(unittest.TestCase):
    def test_url_query_credentials_and_fragment_are_not_saved(self):
        result = safe_url("https://user:private-password@feed.example:443/events?token=private-token&sport=football#private-fragment")
        self.assertIn("feed.example", result)
        for value in ("private-password", "private-token", "private-fragment", "football"):
            self.assertNotIn(value, result)
        self.assertIn("/events", result)

    def test_nested_credentials_are_redacted_but_sports_fields_remain(self):
        original = {"authorization": "private-key", "events": [{"id": 12, "homeTeam": "Arsenal", "awayTeam": "Chelsea",
                    "odds": [2.1, 3.2, 3.4], "nested": {"sessionId": "private-session", "email": "private@email.test"}}]}
        sanitized = redact(original)
        encoded = json.dumps(sanitized)
        self.assertNotIn("private-", encoded)
        self.assertEqual(sanitized["events"][0]["odds"], [2.1, 3.2, 3.4])
        self.assertEqual(original["authorization"], "private-key")

    def test_private_account_endpoints_are_excluded(self):
        capture = Capture(["betboom"])
        for path in ("/api/account/info", "/api/bets_history_v3_ws/v1", "/api/login"):
            url = "https://example.test" + path
            capture.endpoint("betboom", "http", url)
            self.assertFalse(capture.add("betboom", "http_json", url, {"events": [], "odds": []}, True))
        self.assertEqual(capture.records, [])
        self.assertEqual(capture.endpoints, {})

    def test_binary_feeds_are_not_misrepresented_as_parsed_data(self):
        capture = Capture(["betboom"])
        capture.frame("betboom", "wss://example.test/feed", b"\x00\x12protobuf")
        capture.frame("betboom", "wss://example.test/feed", "not json")
        self.assertEqual(capture.stats["betboom"]["binary_websocket_frames"], 1)
        self.assertEqual(capture.stats["betboom"]["non_json_websocket_frames"], 1)
        self.assertFalse(capture.records)

    def test_dom_snapshots_are_deduplicated_and_do_not_read_form_values(self):
        capture = Capture(["winline"])
        text = "Футбол Арсенал — Челси П1 2.10 Х 3.20 П2 3.40"
        nodes = [{"tag": "a", "attrs": {"href": "https://winline.ru/event/12?token=private-token"}, "text": text}]
        capture.dom("winline", "https://winline.ru/football", text, nodes)
        capture.dom("winline", "https://winline.ru/football", text, nodes)
        self.assertEqual(len(capture.records), 1)
        self.assertNotIn("private-token", json.dumps(capture.document()))
        self.assertTrue(capture.records[0]["sports_candidate"])

    def test_same_json_updates_last_observed_without_duplicate_bodies(self):
        capture = Capture(["winline"])
        for _ in range(3):
            capture.add("winline", "http_json", "https://winline.ru/api/sports", {"events": [], "odds": []}, True)
        self.assertEqual(len(capture.records), 1)
        self.assertEqual(capture.records[0]["repeat_count"], 3)
        self.assertIn("last_observed_at", capture.records[0])

    def test_budget_limits_are_reported(self):
        capture = Capture(["betboom"])
        with patch("matchlab.capture.MAX_TOTAL_BYTES", 10):
            self.assertFalse(capture.add("betboom", "http_json", "https://betboom.ru/api/sports", {"events": ["too big"]}, True))
        self.assertEqual(capture.stats["betboom"]["payload_limit"], 1)
        self.assertFalse(capture.records)

    def test_export_is_exclusive_and_reports_unvalidated_adapter(self):
        capture = Capture(["betboom"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            save_document(capture.document(), path)
            doc = json.loads(path.read_text())
            self.assertEqual(doc["format"], FORMAT)
            self.assertFalse(summary(doc)["normalized_matches"])
            with self.assertRaises(FileExistsError):
                save_document(capture.document(), path)
            with self.assertRaises(ValueError):
                summary({"format": "wrong"})

    def test_analytics_are_not_counted_as_sport_payloads(self):
        self.assertFalse(sports_json({"events": [{"type": "pageview", "timestamp": "now"}]}))
        self.assertTrue(sports_json({"events": [{"participants": ["Arsenal", "Chelsea"]}]}))
        self.assertFalse(sports_text("Футбол: новости и статьи"))
        self.assertTrue(sports_text("Футбол Arsenal Chelsea П1 Х П2"))
