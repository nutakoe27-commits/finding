"""Short read-only SQLite snapshots for a concurrent terminal monitor."""

from collections import Counter
from contextlib import closing
import fcntl
import json
from pathlib import Path
import sqlite3
import time

from .store import now


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True, timeout=.3)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def locked(state):
    try:
        with (state / "collector.lock").open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except FileNotFoundError:
        return False
    except PermissionError:
        return None


def latest(db, kind, fields, limit=20, offset=0):
    # Read IDs backwards on (kind,id), deduplicate, then project only the
    # visible payloads. Do not group/sort all historical versions on refresh.
    identifiers, seen, before = [], set(), 2**63-1
    while len(identifiers) < limit:
        batch = db.execute("SELECT id,entity_id FROM observations WHERE kind=? AND id<? ORDER BY id DESC LIMIT 256",
                           (kind, before)).fetchall()
        if not batch:
            break
        for row in batch:
            before = row["id"]
            if row["entity_id"] in seen:
                continue
            seen.add(row["entity_id"])
            if len(seen) > offset:
                identifiers.append(row["id"])
                if len(identifiers) == limit:
                    break
    if not identifiers:
        return []
    projection = ",".join(f"json_extract(o.payload,'$.{field}') AS {alias}" for alias,field in fields.items())
    return [dict(r) for r in db.execute(
        f"SELECT o.id,o.entity_id,o.observed_at,o.received_at,{projection} FROM observations o "
        f"WHERE o.id IN ({','.join('?' for _ in identifiers)}) ORDER BY o.id DESC", identifiers)]


class MonitorReader:
    def __init__(self, state):
        self.state = Path(state).resolve()
        self.path = self.state / "dataset.sqlite3"
        self.cursor = 0
        self.counts = Counter()
        self.inode = None
        self.cache, self.cache_at = {}, 0

    def read(self, *, page=0, size=20, event_id=None):
        data = {"availability": "missing", "at": now(), "database": str(self.path),
                "counts": {}, "totals": {}, "runtime": {}, "runs": [], "matches": [],
                "results": [], "coverage": [], "outcomes": [], "queue": [],
                "writer": locked(self.state), "page": page, "page_size": size, "detail": None}
        if not self.path.is_file():
            return data
        if self.path.resolve().parent != self.state:
            raise ValueError("Monitor database must remain inside its own state directory")
        try:
            with closing(readonly(self.path)) as db:
                owner = db.execute("SELECT value FROM metadata WHERE key='owner'").fetchone()
                if not owner or owner[0] != "doscbot.fresh-collector.v1":
                    raise ValueError("Monitor requires this collector's own dataset.sqlite3")
                maximum = db.execute("SELECT coalesce(max(id),0) FROM observations").fetchone()[0]
                inode = self.path.stat().st_ino
                if inode != self.inode or maximum < self.cursor:
                    self.cursor, self.counts, self.cache_at = 0, Counter(), 0
                self.inode = inode
                # New rows only: refreshing a dashboard must not repeatedly
                # scan all historical odds and dictionary versions.
                self.counts.update({r["kind"]: r["n"] for r in db.execute(
                    "SELECT kind,count(*) n FROM observations WHERE id>? AND id<=? GROUP BY kind", (self.cursor, maximum))})
                self.cursor = maximum
                if time.monotonic()-self.cache_at >= 15:
                    self.cache = {"samples": db.execute("SELECT count(*) FROM samples").fetchone()[0],
                        "results": db.execute("SELECT count(DISTINCT entity_id) FROM observations WHERE kind='result'").fetchone()[0],
                        "detailed": db.execute("SELECT count(DISTINCT entity_id) FROM observations WHERE kind='market_document'").fetchone()[0],
                        "linked": db.execute("SELECT count(DISTINCT entity_id) FROM observations WHERE kind='event_outcome'").fetchone()[0]}
                    self.cache_at = time.monotonic()
                metadata = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM metadata WHERE key IN ('runtime','source_archive')")}
                data.update(availability="ready", counts=dict(self.counts), totals=dict(self.cache),
                            runtime=json.loads(metadata.get("runtime", "{}")))
                data["runs"] = [{**dict(r), "details": json.loads(r["details"])} for r in db.execute(
                    "SELECT * FROM runs ORDER BY id DESC LIMIT 20")]
                data["matches"] = latest(db, "market_document", {"home":"home", "away":"away", "kickoff":"kickoff_at",
                    "sport":"sport", "competition":"competition", "phase":"phase"}, size, page*size)
                data["coverage"] = latest(db, "widget_data", {"coverage_json":"coverage", "groups_json":"views.groups",
                    "missing_json":"views.missing", "statistics_id":"statistics_id"}, size, page*size)
                for item in data["coverage"]:
                    for key in ("coverage_json", "groups_json", "missing_json"):
                        item[key.removesuffix("_json")] = json.loads(item.pop(key) or ("[]" if key == "missing_json" else "{}"))
                data["outcomes"] = latest(db, "event_outcome", {"home":"home", "away":"away", "score":"score_text",
                    "kickoff":"kickoff_at", "result_key":"result_key", "method":"method"}, limit=max(100, size))
                data["results"] = latest(db, "result", {"home":"record.member1", "away":"record.member2", "score":"record.score",
                    "start_at":"record.date", "competition":"record.championship", "sport_id":"sport_id"}, size, page*size)
                for row in data["results"]:
                    link = db.execute("SELECT entity_id FROM observations WHERE kind='event_outcome' "
                        "AND json_extract(payload,'$.result_key')=? ORDER BY id DESC LIMIT 1", (row["entity_id"],)).fetchone()
                    row["event_id"] = link[0] if link else None
                # Only enrich visible events, with indexed entity lookups.
                names = {m["entity_id"]: m for m in data["matches"]}
                for item in data["coverage"]:
                    if item["entity_id"] not in names:
                        row = db.execute("SELECT json_extract(payload,'$.home') home,json_extract(payload,'$.away') away "
                            "FROM observations WHERE kind='market_document' AND entity_id=? ORDER BY id DESC LIMIT 1", (item["entity_id"],)).fetchone()
                        if row:
                            item.update(dict(row))
                    else:
                        item.update({k:names[item["entity_id"]][k] for k in ("home", "away")})
                for item in data["matches"]:
                    row = db.execute("SELECT json_extract(payload,'$.score_text') score,json_extract(payload,'$.method') method "
                        "FROM observations WHERE kind='event_outcome' AND entity_id=? ORDER BY id DESC LIMIT 1", (item["entity_id"],)).fetchone()
                    item["outcome"] = dict(row) if row else None
                    item["samples"] = db.execute("SELECT count(*) FROM samples WHERE event_id=?", (item["entity_id"],)).fetchone()[0]
                    coverage = db.execute("SELECT json_extract(payload,'$.coverage.complete') FROM observations "
                        "WHERE kind='widget_data' AND entity_id=? ORDER BY id DESC LIMIT 1", (item["entity_id"],)).fetchone()
                    item["complete"] = bool(coverage[0]) if coverage else None
                if event_id:
                    data["detail"] = self.detail(db, event_id)
                archive = self.state / "source/winline.sqlite3"
                if (archive.resolve().is_relative_to(self.state) and
                        metadata.get("source_archive") == str(archive.resolve()) and archive.is_file()):
                    self.archive(data, archive, size, page)
                else:
                    data["archive_state"] = "not_available"
            data["database_bytes"] = sum(p.stat().st_size for p in (self.path, Path(str(self.path)+"-wal")) if p.exists())
        except sqlite3.Error as error:
            data.update(availability="error", error=type(error).__name__)
        return data

    def archive(self, data, path, size, page):
        try:
            with closing(readonly(path)) as db:
                data["totals"]["catalog"] = db.execute("SELECT count(*) FROM events WHERE snapshot_id=(SELECT max(id) FROM snapshots)").fetchone()[0]
                data["totals"]["results"] = db.execute("SELECT count(*) FROM results").fetchone()[0]
                source_at = db.execute("SELECT observed_at FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
                data["source_at"] = source_at[0] if source_at else None
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='doscbot_result_jobs'").fetchone()
                if exists:
                    data["queue"] = [dict(r) for r in db.execute("SELECT * FROM doscbot_result_jobs ORDER BY "
                        "CASE WHEN failures>0 THEN 0 ELSE 1 END,retry_after LIMIT 8")]
                data["archive_state"] = "ready"
        except sqlite3.Error:
            data["archive_state"] = "temporarily_unavailable"

    def detail(self, db, identifier):
        result = {"event_id": identifier}
        for kind in ("market_document", "widget_data", "event_outcome", "team_statistics",'event_diagnostic'):
            row = db.execute("SELECT payload FROM observations WHERE kind=? AND entity_id=? ORDER BY id DESC LIMIT 1",
                             (kind, identifier)).fetchone()
            result[kind] = json.loads(row[0]) if row else None
        return result
