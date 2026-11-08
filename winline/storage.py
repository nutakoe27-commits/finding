"""SQLite storage for public sports data. No account data is stored."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "winline.sqlite3"


def now():
    return datetime.now(UTC).isoformat()


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k not in ("meta", "$loki")}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def encode(value):
    return json.dumps(
        clean(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def connect(path=DEFAULT_DB):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS captures (
      id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, url TEXT NOT NULL,
      finished_at TEXT, title TEXT, page_text TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS frames (
      id INTEGER PRIMARY KEY, capture_id INTEGER NOT NULL REFERENCES captures(id),
      received_at TEXT NOT NULL, payload BLOB NOT NULL, is_binary INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS frames_capture ON frames(capture_id,id);
    CREATE TABLE IF NOT EXISTS collection_runs (
      capture_id INTEGER PRIMARY KEY REFERENCES captures(id), settings_json TEXT NOT NULL,
      sdk_version TEXT,status TEXT NOT NULL,planned_market_requests INTEGER DEFAULT 0,
      planned_result_requests INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS snapshots (
      id INTEGER PRIMARY KEY, capture_id INTEGER NOT NULL REFERENCES captures(id),
      observed_at TEXT NOT NULL, events_count INTEGER, lines_count INTEGER);
    CREATE TABLE IF NOT EXISTS sports (id INTEGER PRIMARY KEY,name TEXT,raw_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS countries (id INTEGER PRIMARY KEY,name TEXT,raw_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS championships (
      id INTEGER PRIMARY KEY,name TEXT,raw_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY, sport_id INTEGER, country_id INTEGER, championship_id INTEGER,
      radar_id INTEGER, start_at TEXT, members_json TEXT NOT NULL, is_live INTEGER NOT NULL,
      score TEXT, period_scores TEXT, clock TEXT, source_state INTEGER,
      first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, snapshot_id INTEGER NOT NULL,
      raw_json TEXT NOT NULL, content_hash TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS events_filter ON events(is_live,sport_id,start_at);
    CREATE INDEX IF NOT EXISTS events_seen ON events(snapshot_id);
    CREATE INDEX IF NOT EXISTS events_radar ON events(radar_id);
    CREATE TABLE IF NOT EXISTS event_history (
      id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, snapshot_id INTEGER NOT NULL,
      observed_at TEXT NOT NULL, raw_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS event_history_event ON event_history(event_id,observed_at);
    CREATE TABLE IF NOT EXISTS lines (
      event_id INTEGER NOT NULL, line_id INTEGER NOT NULL, is_live INTEGER NOT NULL,
      market_id INTEGER, market_template TEXT, parameter TEXT, odds_json TEXT NOT NULL,
      labels_json TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
      snapshot_id INTEGER NOT NULL, raw_json TEXT NOT NULL, content_hash TEXT NOT NULL,
      PRIMARY KEY(event_id,line_id,is_live));
    CREATE INDEX IF NOT EXISTS lines_market ON lines(event_id,market_id);
    CREATE TABLE IF NOT EXISTS line_history (
      id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL,line_id INTEGER NOT NULL,
      is_live INTEGER NOT NULL,
      snapshot_id INTEGER NOT NULL,observed_at TEXT NOT NULL,raw_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS line_history_event
      ON line_history(event_id,line_id,is_live,observed_at);
    CREATE TABLE IF NOT EXISTS result_catalog (
      sport_id INTEGER NOT NULL, source_day_ms INTEGER NOT NULL, sport_name TEXT,
      last_seen TEXT NOT NULL, PRIMARY KEY(sport_id,source_day_ms));
    CREATE TABLE IF NOT EXISTS result_requests (
      capture_id INTEGER NOT NULL, sport_id INTEGER NOT NULL, source_day_ms INTEGER NOT NULL,
      observed_at TEXT NOT NULL, row_count INTEGER, error TEXT,
      PRIMARY KEY(capture_id,sport_id,source_day_ms));
    CREATE TABLE IF NOT EXISTS results (
      result_key TEXT PRIMARY KEY,sport_id INTEGER NOT NULL,radar_id INTEGER,
      start_at TEXT,championship TEXT,member1 TEXT,member2 TEXT,score TEXT,
      first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
      raw_json TEXT NOT NULL,content_hash TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS results_filter ON results(sport_id,start_at);
    CREATE INDEX IF NOT EXISTS results_radar ON results(radar_id);
    CREATE TABLE IF NOT EXISTS result_history (
      id INTEGER PRIMARY KEY,result_key TEXT NOT NULL,
      observed_at TEXT NOT NULL,raw_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS market_requests (
      capture_id INTEGER NOT NULL,event_id INTEGER NOT NULL,kind TEXT NOT NULL,
      observed_at TEXT NOT NULL, row_count INTEGER,error TEXT,
      PRIMARY KEY(capture_id,event_id,kind));
    CREATE TABLE IF NOT EXISTS supplemental (
      event_id INTEGER NOT NULL,kind TEXT NOT NULL,last_seen TEXT NOT NULL,
      capture_id INTEGER NOT NULL,raw_json TEXT NOT NULL,content_hash TEXT NOT NULL,
      PRIMARY KEY(event_id,kind));
    CREATE TABLE IF NOT EXISTS supplemental_history (
      id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL,kind TEXT NOT NULL,
      observed_at TEXT NOT NULL,raw_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS supplemental_history_event
      ON supplemental_history(event_id,kind,observed_at);
    CREATE VIEW IF NOT EXISTS matches AS
      SELECT e.id,e.sport_id,s.name sport,c.name country,t.name championship,
        e.start_at,e.members_json,e.is_live,e.score,e.period_scores,e.clock,
        e.radar_id,e.first_seen,e.last_seen,e.snapshot_id,
        e.snapshot_id=(SELECT max(id) FROM snapshots) AS present_in_latest_snapshot
      FROM events e LEFT JOIN sports s ON s.id=e.sport_id
      LEFT JOIN countries c ON c.id=e.country_id
      LEFT JOIN championships t ON t.id=e.championship_id;
    CREATE VIEW IF NOT EXISTS latest_odds AS
      SELECT l.*,l.snapshot_id=(SELECT max(id) FROM snapshots) AS present_in_latest_snapshot
      FROM lines l;
    """)
    return db


def save_snapshot(db, capture_id, data, observed_at=None):
    with db:
        return _save_snapshot(db, capture_id, data, observed_at)


def _save_snapshot(db, capture_id, data, observed_at=None):
    at = observed_at or now()
    if not data.get("events"):
        raise ValueError("Пустой снимок событий: полнота не подтверждена")
    for table in ("sports", "countries", "championships"):
        db.executemany(
            f"INSERT INTO {table}(id,name,raw_json) VALUES (?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,raw_json=excluded.raw_json",
            [(v["id"], v.get("name"), encode(v)) for v in data[table]],
        )
    snapshot = db.execute(
        "INSERT INTO snapshots(capture_id,observed_at,events_count,lines_count) VALUES (?,?,?,?)",
        (capture_id, at, len(data["events"]), len(data["lines"])),
    ).lastrowid
    old_events = {
        r["id"]: r for r in db.execute("SELECT id,content_hash,first_seen FROM events")
    }
    for e in data["events"]:
        raw = encode(e)
        h = digest(raw)
        old = old_events.get(e["id"])
        if old is None or old["content_hash"] != h:
            db.execute(
                "INSERT INTO event_history(event_id,snapshot_id,observed_at,raw_json) "
                "VALUES (?,?,?,?)",
                (e["id"], snapshot, at, raw),
            )
        db.execute(
            """INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET sport_id=excluded.sport_id,country_id=excluded.country_id,
          championship_id=excluded.championship_id,radar_id=excluded.radar_id,start_at=excluded.start_at,
          members_json=excluded.members_json,is_live=excluded.is_live,score=excluded.score,
          period_scores=excluded.period_scores,clock=excluded.clock,source_state=excluded.source_state,
          last_seen=excluded.last_seen,snapshot_id=excluded.snapshot_id,raw_json=excluded.raw_json,content_hash=excluded.content_hash""",
            (
                e["id"],
                e.get("idSport"),
                e.get("idCountry"),
                e.get("idChampionship"),
                e.get("idRadar"),
                e.get("date"),
                encode(e.get("members", [])),
                int(bool(e.get("isLive"))),
                e.get("score"),
                e.get("setScores"),
                e.get("time"),
                e.get("state"),
                old["first_seen"] if old else at,
                at,
                snapshot,
                raw,
                h,
            ),
        )
    old_lines = {
        (r["event_id"], r["line_id"], r["is_live"]): r
        for r in db.execute(
            "SELECT event_id,line_id,is_live,first_seen,content_hash FROM lines"
        )
    }
    for line in data["lines"]:
        e_id, l_id, live = line["idEvent"], line["id"], int(bool(line.get("isLive")))
        raw = encode(line)
        h = digest(raw)
        old = old_lines.get((e_id, l_id, live))
        if old is None or old["content_hash"] != h:
            db.execute(
                "INSERT INTO line_history(event_id,line_id,is_live,snapshot_id,"
                "observed_at,raw_json) "
                "VALUES (?,?,?,?,?,?)",
                (e_id, l_id, live, snapshot, at, raw),
            )
        tip = line.get("tipLine") or {}
        db.execute(
            """INSERT INTO lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(event_id,line_id,is_live) DO UPDATE SET market_id=excluded.market_id,
          market_template=excluded.market_template,parameter=excluded.parameter,odds_json=excluded.odds_json,
          labels_json=excluded.labels_json,last_seen=excluded.last_seen,snapshot_id=excluded.snapshot_id,
          raw_json=excluded.raw_json,content_hash=excluded.content_hash""",
            (
                e_id,
                l_id,
                live,
                line.get("idTipMarket"),
                tip.get("freeTextR"),
                line.get("koef"),
                encode(line.get("V", [])),
                encode(tip.get("R", [])),
                old["first_seen"] if old else at,
                at,
                snapshot,
                raw,
                h,
            ),
        )
    return snapshot


def save_results(db, capture_id, sport_id, day_ms, rows, error=None):
    with db:
        _save_results(db, capture_id, sport_id, day_ms, rows, error)


def _save_results(db, capture_id, sport_id, day_ms, rows, error=None):
    at = now()
    if error is None:
        for r in rows:
            # Results have a different identifier domain from current event IDs.
            identity = {
                k: r.get(k)
                for k in ("idRadar", "date", "championship", "member1", "member2")
            }
            key = digest(encode([sport_id, identity]))
            raw = encode(r)
            h = digest(raw)
            old = db.execute(
                "SELECT content_hash FROM results WHERE result_key=?", (key,)
            ).fetchone()
            if old is None or old["content_hash"] != h:
                db.execute(
                    "INSERT INTO result_history(result_key,observed_at,raw_json) VALUES (?,?,?)",
                    (key, at, raw),
                )
            db.execute(
                """INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(result_key) DO UPDATE SET
              score=excluded.score,last_seen=excluded.last_seen,
              raw_json=excluded.raw_json,content_hash=excluded.content_hash""",
                (
                    key,
                    sport_id,
                    r.get("idRadar"),
                    r.get("date"),
                    r.get("championship"),
                    r.get("member1"),
                    r.get("member2"),
                    r.get("score"),
                    at,
                    at,
                    raw,
                    h,
                ),
            )
    db.execute(
        "INSERT OR REPLACE INTO result_requests VALUES (?,?,?,?,?,?)",
        (capture_id, sport_id, day_ms, at, None if error else len(rows), error),
    )
