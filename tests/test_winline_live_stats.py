"""Signals from the live feed must not invent negative or sport-specific facts."""
import json
import unittest
from datetime import UTC, datetime

from matchlab import archive
from matchlab.event_analysis import analysis_context, request_key
from matchlab.qwen import limited_event_summary
from matchlab.statistics import analysis_statistics
from winline.projection import event


def source(cards, sport=1, live=True):
    return {"id": 101, "radar_id": None, "sport_id": sport, "country_id": 1,
            "championship_id": 1, "start_at": None, "is_live": live,
            "score": "0:0", "clock": "1Т 12'", "period_scores": None,
            "last_seen": datetime.now(UTC).isoformat(),
            "raw_json": json.dumps({"members": ["Команда А", "Команда Б"], "cards": cards})}


def projection(cards, sport=1, live=True):
    return event(source(cards, sport, live), {1: "Футбол", 4: "Хоккей"}, {}, {})


class LiveStatisticsTests(unittest.TestCase):
    def test_match_statistics_are_preserved_and_prioritized_for_the_model(self):
        snapshot = {"state": "ok", "statistics_id": "123", "statistics_url": "https://winline.ru/stats/match/123",
                    "observed_at": datetime.now(UTC).isoformat(), "match_phase": "live", "header": "Команда А — Команда Б",
                    "sections": [{"title": "Статистика сезона", "text": "Сезон"},
                                 {"title": "Статистика матча", "text": "Удары в створ: 5 — 3"}]}
        sections = analysis_statistics(snapshot)["sections"]
        self.assertEqual(sections[0]["text"], "Удары в створ: 5 — 3")
        self.assertEqual(len(sections), 2)

    def test_only_positive_football_live_signals_are_passed(self):
        self.assertEqual(projection({"R1": 1, "R2": 0})["red_card_presence"], {"home": True})
        self.assertEqual(projection({"R1": 0, "R2": 2})["red_card_presence"], {"away": True})
        for value in (projection({"R1": 0, "R2": 0}), projection({"R1": 255, "R2": True}),
                      projection({"R1": 1}, sport=4), projection({"R1": 1}, live=False)):
            self.assertNotIn("red_card_presence", value)

    def test_validation_projection_cache_and_automatic_summary(self):
        no_cards = projection({"R1": 0})
        live = projection({"R1": 1})
        archive.validate_event(live)
        baseline, document = archive.project(no_cards), archive.project(live)
        self.assertNotEqual(baseline["content_version"], document["content_version"])
        self.assertNotEqual(request_key(baseline, None, None, ""), request_key(document, None, None, ""))
        context = analysis_context(document)
        self.assertEqual(context["event"]["red_card_presence"], {"home": True})
        self.assertIn("Удаление отмечено у: Команда А", limited_event_summary(context))
        for invalid in ({"home": False}, {"home": 1}, {}, {"other": True}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                archive.validate_event({**live, "red_card_presence": invalid})
        with self.assertRaises(ValueError):
            archive.validate_event({**live, "sport_id": 4})
        with self.assertRaises(ValueError):
            archive.validate_event({**live, "is_live": False})


if __name__ == "__main__":
    unittest.main()
