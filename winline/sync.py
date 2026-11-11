"""Replicate live projections and incremental result history through the existing tunnel."""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

from .projection import ARCHIVE_REJECTION_CODES, FORMAT, event, market
from .worker import write_status

STOP = False


def checked_response(response, operation):
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        exc.archive_operation = operation
        raise
    return response


def error_status(exc):
    value = {"state": "error", "at": datetime.now(UTC).isoformat(),
             "error_type": type(exc).__name__}
    if isinstance(exc, httpx.HTTPStatusError):
        value["http_status"] = exc.response.status_code
        operation = getattr(exc, "archive_operation", None)
        if operation in {"watch", "feed", "commit", "detail", "results", "status"}:
            value["operation"] = operation
        reason = exc.response.headers.get("X-Matchlab-Archive-Reason")
        if reason in ARCHIVE_REJECTION_CODES:
            value["receiver_reason"] = reason
    return value


def read_feed(database, watch):
    uri = "file:" + quote(str(database.resolve())) + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        snapshot = db.execute(
            "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        first = db.execute(
            "SELECT started_at FROM captures ORDER BY id LIMIT 1"
        ).fetchone()
        if not snapshot or not first:
            return None
        source = hashlib.sha256(first["started_at"].encode()).hexdigest()
        tables = {
            name: {
                r["id"]: r["name"] for r in db.execute("SELECT id,name FROM " + name)
            }
            for name in ["sports", "countries", "championships"]
        }
        headers = []
        details = []
        for r in db.execute(
            "SELECT * FROM events WHERE snapshot_id=?", (snapshot["id"],)
        ):
            value = event(
                r, tables["sports"], tables["countries"], tables["championships"]
            )
            headers.append(value)
            if r["id"] in watch:
                markets = []
                unresolved = 0
                for line in db.execute(
                    "SELECT raw_json,last_seen FROM lines WHERE event_id=? AND is_live=? ORDER BY last_seen DESC LIMIT 2001",
                    (r["id"], r["is_live"]),
                ):
                    m = market(json.loads(line["raw_json"]), line["last_seen"])
                    if m:
                        markets.append(m)
                    else:
                        unresolved += 1
                details.append(
                    {
                        "event": value,
                        "markets": markets[:2000],
                        "truncated": len(markets) > 2000 or unresolved > 0,
                    }
                )
        return {
            "source": source,
            "cycle": hashlib.sha256(f"{source}:{snapshot['id']}".encode()).hexdigest(),
            "source_at": snapshot["observed_at"],
            "events": headers,
            "details": details,
            "counts": {
                name: db.execute("SELECT count(*) FROM " + name).fetchone()[0]
                for name in ["events", "results", "lines"]
            },
            "max_cursor": db.execute(
                "SELECT coalesce(max(id),0) FROM result_history"
            ).fetchone()[0],
        }


def result_batch(database, cursor):
    uri = "file:" + quote(str(database.resolve())) + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [
            {
                "cursor": r["id"],
                "result_key": r["result_key"],
                "sport_id": r["sport_id"],
                "sport_label": r["name"] or f"Спорт {r['sport_id']}",
                "observed_at": r["observed_at"],
                "data": json.loads(r["raw_json"]),
            }
            for r in db.execute(
                """SELECT h.*,r.sport_id,s.name FROM result_history h
                  JOIN results r ON r.result_key=h.result_key LEFT JOIN sports s ON s.id=r.sport_id
                  WHERE h.id>? ORDER BY h.id LIMIT 300""",
                (cursor,),
            )
        ]


def cycle(client, database, state_dir, state):
    reply = client.get("/api/bookmakers/archive/watch")
    checked_response(reply, "watch")
    watch = reply.json()["events"]
    write_status(state_dir / "requests.json", {"at": time.time(), "events": watch})
    feed = read_feed(database, watch)
    if feed is not None:
        feed["counts"]["events"] = len(feed["events"])
    if feed is None:
        return {"state": "waiting_data"}
    if (
        state.get("database_id") != feed["source"]
        or state.get("cursor", 0) > feed["max_cursor"]
    ):
        state.clear()
        state.update(
            database_id=feed["source"],
            source=hashlib.sha256((feed["source"] + uuid4().hex).encode()).hexdigest(),
            cursor=0,
        )
    feed["cycle"] = hashlib.sha256(
        (state["source"] + feed["cycle"]).encode()
    ).hexdigest()

    def send(mode, **fields):
        payload = {"format": FORMAT, "mode": mode, **fields}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 1900000:
            raise ValueError("Archive packet exceeds bounded size")
        response = client.post("/api/bookmakers/archive/ingest", json=payload)
        checked_response(response, mode)
        return response.json()

    if state.get("feed") != feed["cycle"]:
        for start in range(0, len(feed["events"]), 150):
            send(
                "feed", cycle=feed["cycle"], events=feed["events"][start : start + 150]
            )
        send(
            "commit",
            cycle=feed["cycle"],
            source_at=feed["source_at"],
            expected=len(feed["events"]),
        )
        state["feed"] = feed["cycle"]
        write_status(state_dir / "sync-state.json", state)
    for detail in feed["details"]:
        while len(json.dumps(detail, ensure_ascii=False).encode()) > 1800000:
            detail["markets"] = detail["markets"][:-10]
            detail["truncated"] = True
        send("detail", **detail)
    # A bounded archive burst keeps live delivery from waiting for the initial backfill.
    deadline = time.monotonic() + 4
    while state.get("cursor", 0) < feed["max_cursor"] and time.monotonic() < deadline:
        rows = result_batch(database, state.get("cursor", 0))
        if not rows:
            break
        send("results", source=state["source"], rows=rows)
        state["cursor"] = rows[-1]["cursor"]
        write_status(state_dir / "sync-state.json", state)
    try:
        worker = json.loads((state_dir / "status.json").read_text())
    except (OSError, ValueError):
        worker = {}
    send(
        "status",
        status={
            "checked_at": datetime.now(UTC).isoformat(),
            **feed["counts"],
            "source_cursor": feed["max_cursor"],
            "sent_cursor": state.get("cursor", 0),
            "last_snapshot_at": feed["source_at"],
            "worker_state": worker.get("state", "unknown"),
            "worker_job": worker.get("job"),
        },
    )
    return {
        "state": "ok",
        "at": datetime.now(UTC).isoformat(),
        "events": len(feed["events"]),
        "cursor": state.get("cursor", 0),
        "pending": feed["max_cursor"] - state.get("cursor", 0),
        "source_at": feed["source_at"],
    }


def stop(*_):
    global STOP
    STOP = True


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", type=Path, default=Path("/var/lib/winline-archive"))
    p.add_argument("--api-url", default="http://127.0.0.1:18081")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=10)
    args = p.parse_args()
    parsed = urlsplit(args.api_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        p.error("Use the existing local SSH tunnel URL")
    token = os.environ.get("BOOKMAKER_INGEST_TOKEN", "")
    if not token:
        p.error("BOOKMAKER_INGEST_TOKEN is not configured")
    if args.interval < 5:
        p.error("Minimum interval is 5 seconds")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock = (args.state_dir / "sync.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        p.exit(1, "Archive sync is already running\n")
    saved = args.state_dir / "sync-state.json"
    state = json.loads(saved.read_text()) if saved.exists() else {}
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with httpx.Client(
        base_url=args.api_url,
        headers={"X-Matchlab-Collector-Key": token},
        timeout=20,
        trust_env=False,
    ) as client:
        while not STOP:
            try:
                value = cycle(
                    client, args.state_dir / "winline.sqlite3", args.state_dir, state
                )
                code = 0
            except (
                OSError,
                ValueError,
                KeyError,
                sqlite3.Error,
                httpx.HTTPError,
            ) as exc:
                value = error_status(exc)
                code = 1
            write_status(args.state_dir / "sync-status.json", value)
            print(json.dumps(value, ensure_ascii=False), flush=True)
            if args.once:
                return code
            for _ in range(args.interval if not code else 30):
                if STOP:
                    break
                time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
