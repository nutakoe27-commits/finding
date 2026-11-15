"""Conservative result links, kept separate from pre-match samples."""

from collections import defaultdict
from contextlib import closing
import json
import unicodedata

from .source import source_connection
from .store import digest, instant, now


def name_key(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def positive_id(value):
    try:
        return int(value) if int(value) > 0 else None
    except (TypeError, ValueError):
        return None


def choose_result(event, candidates):
    """Keep participant orientation and reject conflicting IDs or ambiguity."""
    members = json.loads(event["members_json"])
    if len(members) != 2 or not all(name_key(x) for x in members) or not event["start_at"]:
        return None, "identity_missing"
    matched = []
    for row in candidates:
        if row["sport_id"] != event["sport_id"] or not row["start_at"] or not str(row["score"] or "").strip():
            continue
        if [name_key(row["member1"]), name_key(row["member2"])] != [name_key(x) for x in members]:
            continue
        try:
            if abs((instant(row["start_at"]) - instant(event["start_at"])).total_seconds()) > 900:
                continue
        except (ValueError, TypeError):
            continue
        radar, result_radar = positive_id(event["radar_id"]), positive_id(row["radar_id"])
        if radar and result_radar and radar != result_radar:
            continue
        matched.append((row, "sport_radar_names_time" if radar and radar == result_radar else "sport_names_time_unique"))
    if not matched:
        return None, "result_not_found"
    if len(matched) != 1:
        return None, "ambiguous_result"
    return matched[0]


def sync_outcomes(store, archive):
    """Link only this collector's own archive; corrections append new versions."""
    with closing(store.connect()) as db:
        owner = db.execute("SELECT value FROM metadata WHERE key='source_archive'").fetchone()
        if not owner or owner[0] != str(archive.resolve()):
            raise ValueError("Only this collector's own fresh source archive may be used")
        # A result label is useful for detailed observations and catalog events
        # alike. Native events retain observed records after removal from line.
    report = {"linked": 0, "new_versions": 0, "pending": 0, "ambiguous": 0}
    current = instant(now())
    with closing(source_connection(archive)) as db, closing(store.connect()) as target, target:
        index = defaultdict(list)
        for row in db.execute("SELECT r.*,h.observed_at AS result_observed_at FROM results r "
                              "JOIN (SELECT result_key,max(id) id FROM result_history GROUP BY result_key) latest "
                              "ON latest.result_key=r.result_key JOIN result_history h ON h.id=latest.id"):
            index[(row["sport_id"], name_key(row["member1"]), name_key(row["member2"]))].append(row)
        for event in db.execute("SELECT id,sport_id,radar_id,start_at,members_json FROM events WHERE start_at IS NOT NULL"):
            try:
                if instant(event["start_at"]) >= current:
                    continue
                members = json.loads(event["members_json"])
                if len(members) != 2:
                    continue
                rows = index.get((event["sport_id"], name_key(members[0]), name_key(members[1])), [])
                result, method = choose_result(event, rows)
            except (ValueError, TypeError):
                report["pending"] += 1
                continue
            if result is None:
                report["ambiguous" if method == "ambiguous_result" else "pending"] += 1
                continue
            payload = {"format": "doscbot.event-outcome.v1", "event_id": str(event["id"]),
                "sport_id": event["sport_id"], "radar_id": event["radar_id"],
                "home": members[0], "away": members[1], "kickoff_at": event["start_at"],
                "result_key": result["result_key"], "score_text": result["score"],
                "result_start_at": result["start_at"], "competition": result["championship"],
                "result_observed_at": result["result_observed_at"], "method": method,
                "source_result_sha256": digest(json.loads(result["raw_json"])),
                "scope": "ResultService score text; no market settlement or training feature"}
            report["linked"] += 1
            report["new_versions"] += int(store.observe(target, "event_outcome", event["id"], result["result_observed_at"], payload))
            if report["linked"] % 200 == 0:
                target.commit()
    return report
