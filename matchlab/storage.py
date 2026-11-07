from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
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
