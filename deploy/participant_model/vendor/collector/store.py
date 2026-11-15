"""Append-only observations and strictly pre-match input snapshots."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


def now():
    return datetime.now(timezone.utc).isoformat()


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def check_observation_times(value, cutoff):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("observed_at", "attempted_at", "capture_started_at") and item:
                if instant(item) > cutoff:
                    raise ValueError("Source observation is later than the sample cutoff")
            check_observation_times(item, cutoff)
    elif isinstance(value, list):
        for item in value:
            check_observation_times(item, cutoff)


def sample_blocker(event, statistics, as_of):
    """Eligibility of a timed input; never relabel an unknown provider phase."""
    cutoff = instant(as_of)
    if event.get("phase") != "prematch":
        return "event_not_prematch"
    if not event.get("kickoff_at"):
        return "kickoff_missing"
    if instant(event["kickoff_at"]) <= cutoff:
        return "kickoff_passed"
    if not statistics or statistics.get("state") != "ok":
        return "statistics_not_ok"
    phase = statistics.get("match_phase")
    if phase not in ("prematch", "unknown"):
        return "statistics_not_prematch"
    if statistics.get("capture_kind", "detailed") != "detailed":
        return "statistics_not_detailed"
    if statistics.get("source_event_id") != event["source_event_id"]:
        raise ValueError("Statistics belongs to a different event")
    # The upstream parser reports unknown for scheduled widgets. Its observed
    # link binding plus the bookmaker's pre-match event and future kickoff
    # establish availability before the event, not a provider phase claim.
    if phase == "unknown":
        identifier = str(statistics.get("statistics_id") or "")
        expected = "https://winline.ru/stats/match/" + identifier
        if (not identifier.isdecimal() or len(identifier) > 20 or
                statistics.get("binding") != "observed_event_link" or
                event.get("statistics_link_status") != "found" or
                statistics.get("statistics_url") != expected or
                event.get("statistics_url") != expected or
                not event.get("url") or statistics.get("event_url") != event["url"]):
            return "statistics_phase_unconfirmed_binding"
    source_match = (statistics.get("structured_feed") or {}).get("match") or {}
    state = source_match.get("status")
    if isinstance(state, dict):
        state = state.get("_id", state.get("id", state.get("name")))
    state = str(state or "").strip().casefold()
    if (state in ("finished", "ended", "closed", "ft", "100", "live", "in_progress", "inprogress") or
            source_match.get("isLive") is True or source_match.get("is_live") is True):
        return "statistics_source_not_prematch"
    check_observation_times({"event": event, "statistics": statistics}, cutoff)
    if (cutoff - instant(statistics["observed_at"])).total_seconds() > 3600:
        return "statistics_stale"
    return None


def widget_blocker(event,statistics,widget):
    if not widget:
        return None
    if (str(widget.get('event_id'))!=str(event['source_event_id']) or
            str(widget.get('statistics_id'))!=str(statistics.get('statistics_id'))):
        return 'widget_identity_mismatch'
    if widget.get('sport')!=event.get('sport') or not widget.get('profile',{}).get('identity_verified'):
        return 'widget_sport_unverified'
    source_match=widget.get('match') or {}
    status=source_match.get('status') or {}
    status_id=status.get('_id') if isinstance(status,dict) else status
    if status_id not in (None,0,'0') or any((source_match.get('result') or {}).get(k) is not None for k in ('home','away')):
        return 'widget_source_started'
    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
 observed_at TEXT NOT NULL, received_at TEXT NOT NULL, source_revision TEXT NOT NULL,
 sha256 TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(kind,entity_id,observed_at,sha256));
CREATE INDEX IF NOT EXISTS observations_entity ON observations(kind,entity_id,observed_at);
CREATE INDEX IF NOT EXISTS observations_kind_id ON observations(kind,id);
CREATE INDEX IF NOT EXISTS observations_outcome_key ON observations(json_extract(payload,'$.result_key')) WHERE kind='event_outcome';
CREATE TABLE IF NOT EXISTS cursors (
 table_name TEXT PRIMARY KEY, row_id INTEGER NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS samples (
 id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, as_of TEXT NOT NULL,
 kickoff_at TEXT NOT NULL, source_revision TEXT NOT NULL, sha256 TEXT NOT NULL,
 payload TEXT NOT NULL, UNIQUE(event_id,as_of,sha256));
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 state TEXT NOT NULL, details TEXT NOT NULL);
"""


class Store:
    def __init__(self, path, revision):
        self.path = Path(path).resolve()
        self.revision = revision
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                owner = db.execute("SELECT value FROM metadata WHERE key='owner'").fetchone() if "metadata" in tables else None
                if not owner or owner[0] != "doscbot.fresh-collector.v1":
                    raise ValueError("Refusing an existing database not created by this collector")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            with db:
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('owner','doscbot.fresh-collector.v1')")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def observe(self, db, kind, entity, at, payload):
        at = instant(at).isoformat()
        return db.execute(
            "INSERT OR IGNORE INTO observations "
            "(kind,entity_id,observed_at,received_at,source_revision,sha256,payload) "
            "VALUES (?,?,?,?,?,?,?)",
            (kind, str(entity), at, now(), self.revision, digest(payload), encoded(payload)),
        ).rowcount

    def observation(self, kind, entity, at, payload):
        with closing(self.connect()) as db, db:
            return self.observe(db, kind, entity, at, payload)

    def save_sample(self, event, statistics, history, as_of, *,widget=None):
        """Only source facts known before kickoff enter the sample table."""
        cutoff = instant(as_of)
        if sample_blocker(event, statistics, as_of):
            return False
        # Finished/live target scores and generated answers are never features.
        event_keys = ("source_event_id", "sport", "sport_label", "home", "away",
                      "competition", "phase", "kickoff_at", "url", "statistics_url",
                      "statistics_link_status", "observed_at", "capture_started_at",
                      "parser_version", "coverage", "markets", "popular_markets",
                      "popular_observed_at", "popular_complete")
        payload = {"event": {k: event[k] for k in event_keys if k in event},
                   "team_statistics": statistics, "archive_history": history,
                   "as_of": cutoff.isoformat(), "format": "doscbot.source-input.v1",
                   "eligibility": {"policy": "observed-input-before-kickoff-v2",
                                   "event_phase": event["phase"],
                                   "statistics_phase": statistics["match_phase"],
                                   "basis": "observed_event_link_before_kickoff" if statistics["match_phase"] == "unknown"
                                   else "explicit_prematch_statistics"}}
        if widget:
            if widget_blocker(event,statistics,widget):
                return False
            payload['widget_data']=widget
            payload['format']='doscbot.source-input.v2'

        check_observation_times(payload, cutoff)
        with closing(self.connect()) as db, db:
            return bool(db.execute(
                "INSERT OR IGNORE INTO samples "
                "(event_id,as_of,kickoff_at,source_revision,sha256,payload) VALUES (?,?,?,?,?,?)",
                (str(event["source_event_id"]), cutoff.isoformat(), event["kickoff_at"],
                 self.revision, digest(payload), encoded(payload)),
            ).rowcount)

    def begin_run(self):
        with closing(self.connect()) as db, db:
            return db.execute(
                "INSERT INTO runs(started_at,state,details) VALUES (?,?,?)",
                (now(), "running", "{}"),
            ).lastrowid

    def finish_run(self, run_id, state, details):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE runs SET finished_at=?,state=?,details=? WHERE id=?",
                       (now(), state, encoded(details), run_id))

    def status(self):
        with closing(self.connect()) as db:
            counts = {r["kind"]: r["n"] for r in db.execute(
                "SELECT kind,count(*) n FROM observations GROUP BY kind")}
            latest = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            return {"database": str(self.path), "observations": counts,
                    "samples": db.execute("SELECT count(*) FROM samples").fetchone()[0],
                    "last_run": dict(latest) if latest else None}

    def export(self, destination, kind="samples"):
        """Exclusive creation prevents accidental overwrite of an existing export."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        try:
            with destination.open("x", encoding="utf-8") as output:
                try:
                    with closing(self.connect()) as db:
                        query = ("SELECT id,source_revision,sha256,payload FROM samples ORDER BY as_of,id"
                                 if kind == "samples" else
                                 "SELECT * FROM observations ORDER BY observed_at,id")
                        for row in db.execute(query):
                            record = dict(row)
                            record["payload"] = json.loads(record["payload"])
                            output.write(encoded(record) + "\n")
                            count += 1
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
        except FileExistsError:
            raise ValueError("Export already exists; choose a new path") from None
        return count
