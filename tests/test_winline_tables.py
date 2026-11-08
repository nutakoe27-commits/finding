"""Recorded live rejection and synthetic future variants of that table layout."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from matchlab.bookmakers import EPL, model_teams, normalize_capture

OBSERVED = "2026-11-08T09:30:00+00:00"  # Test clock, not the live report's omitted timestamp.
LIVE = json.loads((Path(__file__).parent / "fixtures/winline_live_report.json").read_text())


def record(example):
    context = example["parents"][-1]["text"]
    return {"source": "winline", "kind": "public_dom", "url": LIVE["url"], "observed_at": OBSERVED,
            "payload": {"text": "Футбол\nИсход\n" + context, "nodes": [
                {"tag": "a", "attrs": {"href": example["url"]}, "text": example["text"],
                 "winline_card": {"text": context, "tournament": context.splitlines()[0], "datetimes": []}}]}}


def future_record():
    # Synthetic change: add a future timestamp to the recorded no-clock card.
    result = record(LIVE["examples"][1])
    card = result["payload"]["nodes"][0]["winline_card"]
    card["text"] = card["text"].replace("+160\n", "+160\nСегодня 21:00\n")
    return result


def parsed(value):
    return normalize_capture({"format": "matchlab.bookmaker-capture.v1", "records": [value]})["snapshots"]


class WinlineTableTests(unittest.TestCase):
    def test_both_recorded_live_examples_are_rejected_without_inventing_kickoff(self):
        for example in LIVE["examples"]:
            self.assertEqual(parsed(record(example)), [])

    def test_in_play_clock_remains_rejected_even_with_a_future_date(self):
        value = record(LIVE["examples"][0])
        value["payload"]["nodes"][0]["winline_card"]["text"] += "\nСегодня 21:00"
        self.assertEqual(parsed(value), [])

    def test_full_match_triplet_not_handicap_total_or_half_is_selected(self):
        row, = parsed(future_record())
        self.assertEqual(row["odds"], {"home": 4.05, "draw": 4.1, "away": 1.68})
        self.assertEqual(row["competition"], "Нидерланды. Высшая Лига")
        self.assertEqual(row["kickoff_at"], "2026-11-08T18:00:00.000000+00:00")
        self.assertIsNone(model_teams(row))

    def test_explicit_english_tournament_identifies_epl_without_betboom(self):
        value = future_record()
        node = value["payload"]["nodes"][0]
        node["text"] = "Брайтон\nБрентфорд"
        card = node["winline_card"]
        card["text"] = card["text"].replace("Херенвен\nФейеноорд", node["text"])
        card["tournament"] = "Англия | Премьер-лига"
        row, = parsed(value)
        self.assertEqual(row["competition"], EPL)
        self.assertEqual(model_teams(row), ("Brighton", "Brentford"))
        card["tournament"] = "Англия | Чемпионшип"
        row, = parsed(value)
        self.assertIsNone(model_teams(row))

    def test_paused_or_missing_full_market_never_falls_back_to_half_or_page_triplet(self):
        for replacement in ("Матч\n-\n4.10\n1.68", "Другой рынок\n4.05\n4.10\n1.68"):
            value = future_record()
            value["payload"]["nodes"][0]["winline_card"]["text"] = value["payload"]["nodes"][0]["winline_card"]["text"].replace("Матч\n4.05\n4.10\n1.68", replacement)
            value["payload"]["text"] += "\nХеренвен\nФейеноорд\n2.10\n3.20\n3.40\nСегодня 21:00"
            self.assertEqual(parsed(value), [])

    def test_half_can_precede_match_without_changing_selected_market(self):
        value = future_record()
        card = value["payload"]["nodes"][0]["winline_card"]
        card["text"] = "1 тайм\n1.10\n1.20\n1.30\n" + card["text"]
        row, = parsed(value)
        self.assertEqual(row["odds"]["home"], 4.05)

    def test_card_boundaries_dates_and_timestamps_fail_closed(self):
        original = future_record()
        variants = ["Сегодня 21:00\nЗавтра 21:00", "Сегодня 10:00"]
        for replacement in variants:
            value = deepcopy(original)
            card = value["payload"]["nodes"][0]["winline_card"]
            card["text"] = card["text"].replace("Сегодня 21:00", replacement)
            self.assertEqual(parsed(value), [])
        node = original["payload"]["nodes"][0]
        node["text"] = "Другой клуб\nЕщё один клуб"
        self.assertEqual(parsed(original), [])

    def test_explicit_datetime_requires_timezone_and_future_time(self):
        value = future_record()
        card = value["payload"]["nodes"][0]["winline_card"]
        card["text"] = card["text"].replace("Сегодня 21:00\n", "")
        card["datetimes"] = ["2026-11-08T18:00:00+00:00"]
        self.assertEqual(len(parsed(value)), 1)
        for invalid in ("2026-11-08T18:00:00", "2026-11-08T01:00:00+00:00"):
            card["datetimes"] = [invalid]
            self.assertEqual(parsed(value), [])

    def test_unscoped_featured_carousel_is_not_promoted_to_football(self):
        value = future_record()
        del value["payload"]["nodes"][0]["winline_card"]
        value["payload"]["text"] += "\nХеренвен\nФейеноорд\n2.10\n3.20\n3.40\nСегодня 21:00"
        self.assertEqual(parsed(value), [])


if __name__ == "__main__":
    unittest.main()
