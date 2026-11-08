from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import zlib
from typing import Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS matches (
                    date TEXT NOT NULL,
                    season TEXT NOT NULL,
                    home TEXT NOT NULL,
                    away TEXT NOT NULL,
                    home_goals INTEGER NOT NULL CHECK(home_goals >= 0),
                    away_goals INTEGER NOT NULL CHECK(away_goals >= 0),
                    PRIMARY KEY(date, home, away)
                );
                CREATE INDEX IF NOT EXISTS matches_date ON matches(date);
                CREATE TABLE IF NOT EXISTS sources (
                    season TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forecasts (
                    id TEXT PRIMARY KEY,
                    generated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bookmaker_quotes (
                    id TEXT PRIMARY KEY,
                    event_key TEXT NOT NULL,
                    fixture_key TEXT NOT NULL,
                    source TEXT NOT NULL,
                    kickoff_at TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS bookmaker_quote_event ON bookmaker_quotes(event_key, observed_at DESC);
                CREATE TABLE IF NOT EXISTS bookmaker_imports (
                    id TEXT PRIMARY KEY,
                    imported_at TEXT NOT NULL,
                    quote_count INTEGER NOT NULL,
                    report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bookmaker_worker (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bookmaker_event_markets (
                    event_id TEXT PRIMARY KEY,
                    phase TEXT NOT NULL,
                    kickoff_at TEXT,
                    observed_at TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    market_count INTEGER NOT NULL,
                    outcome_count INTEGER NOT NULL,
                    complete INTEGER NOT NULL,
                    metadata TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS bookmaker_market_kickoff ON bookmaker_event_markets(kickoff_at);
            """)

    def replace_season(self, season: str, rows: list[dict], source: str, digest: str) -> None:
        if not rows:
            raise ValueError("В файле нет завершённых матчей; существующие данные сохранены.")
        with self.connection() as db:
            db.execute("DELETE FROM matches WHERE season = ?", (season,))
            db.executemany(
                "INSERT INTO matches VALUES (:date, :season, :home, :away, :home_goals, :away_goals)",
                rows,
            )
            db.execute(
                "INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?)",
                (season, source, digest, utc_now(), len(rows)),
            )

    def matches(self, before: date | None = None, since: date | None = None) -> list[dict]:
        clauses, params = [], []
        if before is not None:
            clauses.append("date < ?")
            params.append(before.isoformat())
        if since is not None:
            clauses.append("date >= ?")
            params.append(since.isoformat())
        query = "SELECT * FROM matches"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY date, home, away"
        with self.connection() as db:
            return [dict(row) for row in db.execute(query, params)]

    def revision(self) -> str:
        with self.connection() as db:
            return self._revision(db)

    @staticmethod
    def _revision(db: sqlite3.Connection) -> str:
        values = [tuple(row) for row in db.execute(
            "SELECT season, sha256 FROM sources ORDER BY season"
        )]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    def snapshot(self, before: date | None = None) -> tuple[list[dict], str]:
        # A read transaction ties training data to its exact source hashes even
        # if a separate CLI process is importing the next season concurrently.
        with self.connection() as db:
            db.execute("BEGIN")
            query = "SELECT * FROM matches"
            params = ()
            if before is not None:
                query += " WHERE date < ?"
                params = (before.isoformat(),)
            query += " ORDER BY date, home, away"
            rows = [dict(row) for row in db.execute(query, params)]
            return rows, self._revision(db)

    def status(self) -> dict:
        with self.connection() as db:
            stats = dict(db.execute(
                "SELECT COUNT(*) AS matches, MIN(date) AS first_match, MAX(date) AS last_match FROM matches"
            ).fetchone())
            stats["sources"] = [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY season")]
        last = stats["last_match"]
        stats["stale_days"] = (date.today() - date.fromisoformat(last)).days if last else None
        stats["revision"] = self.revision()
        return stats

    def save_forecast(self, payload: dict) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO forecasts VALUES (?, ?, ?)", (
                payload["id"], payload["generated_at"], json.dumps(payload, ensure_ascii=False),
            ))

    def forecast(self, forecast_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM forecasts WHERE id = ?", (forecast_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def recent_forecasts(self, limit: int = 20) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM forecasts ORDER BY generated_at DESC LIMIT ?", (limit,))
            return [json.loads(row[0]) for row in rows]

    def import_bookmakers(self, document: dict) -> dict:
        from .bookmakers import instant, load_snapshots

        parsed = load_snapshots(document)
        rows = parsed["snapshots"]
        if len(rows) > 10000:
            raise ValueError("Слишком много снимков в одном импорте.")
        current = datetime.now(timezone.utc)
        if any(instant(row["observed_at"]) > current + timedelta(minutes=2) for row in rows):
            raise ValueError("Время получения снимка находится в будущем. Проверьте часы компьютера-сборщика.")
        batch_id = hashlib.sha256(json.dumps(sorted(row["id"] for row in rows)).encode()).hexdigest()
        with self.connection() as db:
            previous = db.execute("SELECT report FROM bookmaker_imports WHERE id = ?", (batch_id,)).fetchone()
            if previous:
                return {**json.loads(previous[0]), "inserted": 0, "already_imported": True}
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO bookmaker_quotes VALUES (?, ?, ?, ?, ?, ?, ?)", [
                (row["id"], row["event_key"], row["fixture_key"], row["source"], row["kickoff_at"], row["observed_at"], json.dumps(row, ensure_ascii=False))
                for row in rows
            ])
            report = {"batch_id": batch_id, "inserted": db.total_changes - before, "parsed_snapshots": len(rows),
                      "events": len({row["event_key"] for row in rows}), "report": parsed["report"]}
            db.execute("INSERT INTO bookmaker_imports VALUES (?, ?, ?, ?)",
                       (batch_id, utc_now(), report["inserted"], json.dumps(report, ensure_ascii=False)))
            return report

    def bookmaker_quote(self, quote_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM bookmaker_quotes WHERE id = ?", (quote_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def bookmaker_quotes(self, *, include_started: bool = False, source: str | None = None) -> list[dict]:
        from .bookmakers import iso
        query = """WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY event_key ORDER BY observed_at DESC) AS position
            FROM bookmaker_quotes
        ) SELECT payload FROM ranked WHERE position = 1"""
        params = []
        if not include_started:
            query += " AND kickoff_at > ?"
            params.append(iso(datetime.now(timezone.utc)))
        if source:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY kickoff_at, fixture_key, source"
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute(query, params)]

    def bookmaker_status(self) -> dict:
        with self.connection() as db:
            sources = [dict(row) for row in db.execute("""SELECT source, COUNT(*) AS snapshots,
                COUNT(DISTINCT event_key) AS events, MAX(observed_at) AS last_observed_at
                FROM bookmaker_quotes GROUP BY source ORDER BY source""")]
            state = db.execute("SELECT payload FROM bookmaker_worker WHERE id = 1").fetchone()
        worker = json.loads(state[0]) if state else None
        if worker:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(worker["last_attempt_at"])).total_seconds()
            worker["heartbeat_recent"] = age <= max(180, worker["interval_seconds"] * 3)
        return {"sources": sources, "worker": worker, "live_connection_verified": False, "market": "1x2_90m"}

    def save_worker_status(self, value: dict) -> dict:
        from .worker_protocol import validate_status
        status = validate_status(value)
        with self.connection() as db:
            row = db.execute("SELECT payload FROM bookmaker_worker WHERE id = 1").fetchone()
            previous = json.loads(row[0]) if row else {}
            status["last_attempt_at"] = utc_now()
            status["last_success_at"] = status["last_attempt_at"] if status["state"] == "ok" else previous.get("last_success_at")
            db.execute("INSERT OR REPLACE INTO bookmaker_worker VALUES (1, ?)", (json.dumps(status),))
        return status

    def import_event_markets(self, value: dict) -> dict:
        from .markets import validate_event_markets
        document = validate_event_markets(value)
        if datetime.fromisoformat(document["observed_at"]) > datetime.now(timezone.utc) + timedelta(minutes=2):
            raise ValueError("Время наблюдения находится в будущем.")
        event_id, observed = document["source_event_id"], document["observed_at"]
        metadata = {key: item for key, item in document.items() if key != "markets"}
        content = {key: item for key, item in document.items() if key not in ("observed_at", "capture_started_at")}
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        with self.connection() as db:
            previous = db.execute("SELECT observed_at, content_hash FROM bookmaker_event_markets WHERE event_id = ?", (event_id,)).fetchone()
            if previous and observed < previous["observed_at"]:
                return {"updated": False, "reason": "older_observation", "source_event_id": event_id}
            if previous and not document['coverage']['complete']:
                return {"updated": False, "reason": "incomplete_snapshot", "source_event_id": event_id}
            if previous and digest == previous["content_hash"]:
                db.execute("UPDATE bookmaker_event_markets SET observed_at = ?, metadata = ? WHERE event_id = ?",
                           (observed, json.dumps(metadata, ensure_ascii=False), event_id))
                return {"updated": True, "changed": False, "source_event_id": event_id, "coverage": document["coverage"]}
            # Keep the latest full market document, compressed. Repeating a
            # whole book every minute must not create unbounded full archives.
            db.execute("""INSERT INTO bookmaker_event_markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET phase=excluded.phase, kickoff_at=excluded.kickoff_at,
                observed_at=excluded.observed_at, changed_at=excluded.changed_at,
                market_count=excluded.market_count, outcome_count=excluded.outcome_count,
                complete=excluded.complete, metadata=excluded.metadata, content_hash=excluded.content_hash,
                payload=excluded.payload""", (event_id, document["phase"], document["kickoff_at"], observed, observed,
                len(document["markets"]), document["coverage"]["captured_outcomes"], int(document["coverage"]["complete"]),
                json.dumps(metadata, ensure_ascii=False), digest, zlib.compress(encoded)))
        return {"updated": True, "changed": True, "source_event_id": event_id, "coverage": document["coverage"]}

    def event_markets(self, event_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT observed_at, changed_at, metadata, payload FROM bookmaker_event_markets WHERE event_id = ?", (event_id,)).fetchone()
        if not row:
            return None
        return {**json.loads(zlib.decompress(row["payload"])), **json.loads(row["metadata"]),
                "observed_at": row["observed_at"], "changed_at": row["changed_at"]}

    def market_events(self, *, include_started=False, limit=100, offset=0) -> dict:
        query, params = "FROM bookmaker_event_markets", []
        if not include_started:
            query += " WHERE phase NOT IN ('live', 'finished') AND (kickoff_at IS NULL OR kickoff_at > ?)"
            params.append(datetime.now(timezone.utc).isoformat(timespec='microseconds'))
        with self.connection() as db:
            total = db.execute("SELECT COUNT(*) " + query, params).fetchone()[0]
            rows = db.execute("SELECT metadata, observed_at, changed_at " + query + " ORDER BY COALESCE(kickoff_at, '9999'), event_id LIMIT ? OFFSET ?", [*params, limit, offset])
            events = [{**json.loads(row["metadata"]), "observed_at": row["observed_at"], "changed_at": row["changed_at"]} for row in rows]
        return {"events": events, "total": total, "offset": offset, "limit": limit}
