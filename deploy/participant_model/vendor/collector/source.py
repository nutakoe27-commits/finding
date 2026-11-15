"""Read finding's parser code, never its database, configuration or model."""

from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from .store import digest, instant
from .profiles import accepts, balanced

ROOT = Path(__file__).resolve().parent.parent
PIN = json.loads((ROOT / "finding-source.json").read_text())


def load_finding(path):
    path = Path(path).resolve()
    head = subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    if head != PIN["commit"]:
        raise ValueError("finding revision differs from finding-source.json")
    changes = subprocess.check_output(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"], text=True)
    if changes.strip():
        raise ValueError("finding has modified tracked files; use a clean parser checkout")
    sys.path.insert(0, str(path))
    return path, head


def source_connection(path):
    path = Path(path).resolve()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


HISTORY_TABLES = {
    "event_history": ("event", ("event_id",)),
    "line_history": ("market", ("event_id", "line_id", "is_live")),
    "result_history": ("result", ("result_key",)),
    "supplemental_history": ("supplemental", ("event_id", "kind")),
}


def sync_archive(store, path):
    """Incremental, transactional copy from this collector's OWN fresh archive."""
    counts = {}
    with closing(source_connection(path)) as source, closing(store.connect()) as target:
        for table, (kind, keys) in HISTORY_TABLES.items():
            checkpoint = target.execute("SELECT * FROM cursors WHERE table_name=?", (table,)).fetchone()
            after = checkpoint["row_id"] if checkpoint else 0
            if checkpoint:
                row = source.execute(f"SELECT * FROM {table} WHERE id=?", (after,)).fetchone()
                if row is None or digest(dict(row)) != checkpoint["sha256"]:
                    raise ValueError("Fresh source archive was replaced; use a new state directory")
            counts[kind] = 0
            rows = source.execute(f"SELECT * FROM {table} WHERE id>? ORDER BY id", (after,))
            while batch := rows.fetchmany(500):
                with target:
                    for row in batch:
                        payload = json.loads(row["raw_json"])
                        if kind == "result":
                            sport = source.execute("SELECT sport_id FROM results WHERE result_key=?", (row["result_key"],)).fetchone()
                            if not sport:
                                raise ValueError("Result history has no source sport identity")
                            payload = {"sport_id": sport[0], "record": payload}
                        entity = ":".join(str(row[k]) for k in keys)
                        counts[kind] += store.observe(
                            target, kind, entity, row["observed_at"], payload)
                    last = batch[-1]
                    target.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?)",
                                   (table, last["id"], digest(dict(last))))
        # Keep source dictionaries so unknown sport/market identifiers remain interpretable.
        latest = source.execute("SELECT observed_at FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if latest:
            with target:
                for table in ("sports", "countries", "championships"):
                    for row in source.execute(f"SELECT id,raw_json FROM {table}"):
                        store.observe(target, table, row["id"], latest[0], json.loads(row["raw_json"]))
    return counts


def candidates(path, sport, limit, cutoff, last_attempts=None, event_id=None, link_statuses=None):
    """Only source-observed upcoming matches; no guessed statistics IDs."""
    from matchlab.archive import sport_key
    from matchlab.sports import match_url
    from winline.projection import event

    selected = []
    with closing(source_connection(path)) as db:
        dictionaries = [{r["id"]: r["name"] for r in db.execute(f"SELECT id,name FROM {table}")}
                        for table in ("sports", "countries", "championships")]
        for row in db.execute("SELECT * FROM events WHERE is_live=0 AND "
                              "snapshot_id=(SELECT max(id) FROM snapshots) "
                              "ORDER BY julianday(start_at),id"):
            value = event(row, *dictionaries)
            if event_id is not None and str(value["event_id"]) != str(event_id):
                continue
            key = sport_key(value["sport_label"], value["sport_id"])
            if not accepts(sport, key):
                continue
            if not value["kickoff_at"] or instant(value["kickoff_at"]) <= instant(cutoff):
                continue
            members = value["members"]
            if len(members) != 2 or not all(isinstance(x, str) and x for x in members):
                continue
            metadata = {"source_event_id": str(value["event_id"]), "sport": key,
                        "native_radar_id":value.get('radar_id'),
                        "sport_label": value["sport_label"], "home": members[0], "away": members[1],
                        "competition": value["competition"], "kickoff_at": value["kickoff_at"],
                        "phase": "prematch", "source_kind": "stream"}
            url = match_url(metadata)
            if url:
                selected.append({**metadata, "url": url})
    last_attempts = last_attempts or {}
    link_statuses = link_statuses or {}
    refresh_before = instant(cutoff) - timedelta(minutes=30)
    def priority(event):
        identifier = event['source_event_id']
        status = link_statuses.get(identifier)
        radar = event.get('native_radar_id')
        # This is an availability hint only. Statistics identity still comes
        # from an observed event link; never build a widget URL from radar.
        promising = status=='found' or (status not in ('missing','ambiguous') and isinstance(radar,int) and radar>0)
        attempted = last_attempts.get(identifier, '')
        # Once refreshed, a rich event rejoins normal rotation so unknown and
        # missing links still get opportunities rather than starving forever.
        due = not attempted or instant(attempted) <= refresh_before
        return (0 if promising and due else 1, attempted, instant(event['kickoff_at']))
    selected.sort(key=priority)
    return balanced(selected, limit)
