"""Public, bounded representations shared by the archive bridge and web service."""

import hashlib
import json
import math
from datetime import UTC, datetime

FORMAT = "matchlab.archive.v1"


def identity(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:32]


def market(line, observed_at):
    display = line.get("_display") or {}
    if not display.get("valid"):
        return None
    name = display.get("name", "")
    if not name or len(name) > 500:
        return None
    key = identity(
        [
            line["idEvent"],
            line["id"],
            bool(line.get("isLive")),
            line.get("idTipMarket"),
            line.get("koef"),
            display.get("name"),
            display.get("labels"),
        ]
    )
    outcomes = []
    for index, value in enumerate(line.get("V") or []):
        labels = display.get("labels") or []
        if index >= len(labels) or not labels[index] or len(labels[index]) > 500:
            return None
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value <= 100000
        )
        outcomes.append(
            {
                "key": identity([key, index]),
                "name": labels[index],
                "parameter": None,
                "odds": value if valid and value > 1 else None,
                "state": "open" if valid and value > 1 else "suspended",
            }
        )
    if not outcomes:
        return None
    return {
        "key": key,
        "name": name,
        "group": "Полученные рынки",
        "period": None,
        "parameter": str(line.get("koef") or "") or None,
        "variant_id": None,
        "outcomes": outcomes,
        "observed_at": observed_at,
        "source_line_id": line["id"],
        "source_event_id": str(line["idEvent"]),
    }


def event(row, sports, countries, championships):
    raw = json.loads(row["raw_json"])
    members = raw.get("members") or []
    primary = (
        market(raw["_main_line"], row["last_seen"]) if raw.get("_main_line") else None
    )
    competition = ". ".join(
        filter(
            None,
            [
                countries.get(row["country_id"]),
                championships.get(row["championship_id"]),
            ],
        )
    )
    return {
        "event_id": str(row["id"]),
        "radar_id": row["radar_id"],
        "sport_id": row["sport_id"],
        "sport_label": sports.get(row["sport_id"]) or f"Спорт {row['sport_id']}",
        "members": members[:2],
        "competition": competition or "Турнир не указан",
        "kickoff_at": row["start_at"],
        "is_live": bool(row["is_live"]),
        "score": row["score"],
        "clock": row["clock"],
        "period_scores": row["period_scores"],
        "observed_at": row["last_seen"],
        "primary": primary,
        "expected_markets": raw.get("lineCount")
        if row["is_live"]
        else raw.get("count_add_line"),
    }


def age(timestamp):
    return max(
        0,
        int((datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds()),
    )
