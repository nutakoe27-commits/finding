"""Archive replication, stream catalogue and time-bounded participant facts."""

import json
import re
import unicodedata
import zlib
from datetime import UTC, datetime, timedelta

from winline.projection import FORMAT, age, identity
from winline.storage import digest, encode

from .bookmakers import instant
from .markets import headline_preview
from .sports import SPORTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_feed_rows (
 cycle TEXT NOT NULL,event_id TEXT NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(cycle,event_id));
CREATE TABLE IF NOT EXISTS archive_feed (
 id INTEGER PRIMARY KEY CHECK(id=1),cycle TEXT NOT NULL,source_at TEXT NOT NULL,received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_headers (event_id TEXT PRIMARY KEY,payload BLOB NOT NULL,source_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_details (
 event_id TEXT PRIMARY KEY,payload BLOB NOT NULL,received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_versions (
 source TEXT NOT NULL,cursor INTEGER NOT NULL,result_key TEXT NOT NULL,sport_id INTEGER NOT NULL,
 sport_label TEXT NOT NULL,observed_at TEXT NOT NULL,received_at TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(source,cursor));
CREATE INDEX IF NOT EXISTS archive_versions_sport ON archive_versions(sport_id,observed_at);
CREATE INDEX IF NOT EXISTS archive_versions_result ON archive_versions(result_key,observed_at);
CREATE TABLE IF NOT EXISTS archive_quarantined_results (
 source TEXT NOT NULL,cursor INTEGER NOT NULL,result_key TEXT NOT NULL,sport_id INTEGER NOT NULL,
 sport_label TEXT NOT NULL,observed_at TEXT NOT NULL,received_at TEXT NOT NULL,payload TEXT NOT NULL,
 reason TEXT NOT NULL,PRIMARY KEY(source,cursor));
CREATE INDEX IF NOT EXISTS archive_quarantined_result ON archive_quarantined_results(result_key);
CREATE TABLE IF NOT EXISTS archive_latest_results (
 result_key TEXT PRIMARY KEY,sport_id INTEGER NOT NULL,sport_label TEXT NOT NULL,
 start_at TEXT NOT NULL,member1 TEXT NOT NULL,member2 TEXT NOT NULL,score TEXT,
 observed_at TEXT NOT NULL,received_at TEXT NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS archive_results_date ON archive_latest_results(start_at);
CREATE TABLE IF NOT EXISTS archive_status (id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_watches (event_id TEXT PRIMARY KEY,requested_at TEXT NOT NULL);
"""


def now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def key(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def packed(value):
    return zlib.compress(json.dumps(value, ensure_ascii=False).encode())


def unpacked(value):
    return json.loads(zlib.decompress(value))


def label(value, size=500, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > size:
        raise ValueError("Некорректный текст архива")
    return value


def timestamp(value):
    at = instant(value)
    if at > datetime.now(UTC) + timedelta(minutes=2):
        raise ValueError("Часы источника опережают сервер")
    return at.isoformat(timespec="microseconds")


def event_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,20}", value):
        raise ValueError("Некорректный ID")
    return value


def validate_market(value, parent):
    if not isinstance(value, dict) or value.get("source_event_id") != parent:
        raise ValueError("Чужой рынок")
    if not re.fullmatch(r"[a-f0-9]{32}", value.get("key", "")):
        raise ValueError("Некорректный ключ рынка")
    label(value.get("name"))
    timestamp(value.get("observed_at"))
    outcomes = value.get("outcomes")
    if not isinstance(outcomes, list) or not 1 <= len(outcomes) <= 30:
        raise ValueError("Некорректные исходы")
    for item in outcomes:
        label(item.get("name"))
        if not re.fullmatch(r"[a-f0-9]{32}", item.get("key", "")):
            raise ValueError("Некорректный исход")
        price = item.get("odds")
        if price is not None and (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not 1 < price <= 100000
        ):
            raise ValueError("Некорректная цена")
        if item.get("state") not in ("open", "suspended", "unavailable"):
            raise ValueError("Некорректное состояние")
    return value


def rejection_reason(exc):
    # Only protocol-defined categories may leave this authenticated endpoint.
    # Never echo a request body, parser exception or arbitrary field value.
    return {
        "Некорректный текст архива": "invalid_text",
        "Часы источника опережают сервер": "clock_skew",
        "Некорректный ID": "invalid_event_id",
        "Чужой рынок": "market_event_mismatch",
        "Некорректный счётчик": "invalid_counter",
        "Неизвестное состояние worker": "invalid_worker_state",
        "Неизвестное задание": "invalid_worker_job",
        "Некорректный цикл": "invalid_cycle",
        "Получен не весь снимок": "incomplete_feed",
        "Слишком много рынков": "too_many_markets",
        "Некорректный курсор": "invalid_result_cursor",
        "Некорректный ключ результата": "invalid_result_key",
        "Результат относится к будущей встрече": "future_result",
        "Ключ результата не соответствует встрече": "result_identity_mismatch",
        "Курсор относится к другой версии результата": "result_cursor_conflict",
    }.get(str(exc), "invalid_packet")


def validate_event(value):
    parent = event_id(value.get("event_id"))
    if type(value.get("sport_id")) is not int or value["sport_id"] < 0:
        raise ValueError("Некорректный спорт")
    label(value.get("sport_label"), 100)
    label(value.get("competition"))
    if value.get("radar_id") is not None and type(value["radar_id"]) is not int:
        raise ValueError("Некорректная привязка события")
    members = value.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= 2:
        raise ValueError("Некорректные участники")
    for name in members:
        label(name, 200)
    timestamp(value.get("observed_at"))
    if value.get("kickoff_at") is not None:
        instant(value["kickoff_at"])
    if type(value.get("is_live")) is not bool:
        raise ValueError("Некорректная фаза")
    rank = value.get("featured_rank")
    if rank is not None and (type(rank) is not int or not 0 <= rank < 1000):
        raise ValueError("Некорректный порядок главных событий")
    if "red_card_presence" in value:
        cards = value["red_card_presence"]
        if (value["sport_id"] != 1 or not value["is_live"] or
            not isinstance(cards, dict) or not cards or
            any(key not in ("home", "away") or present is not True for key, present in cards.items())):
            raise ValueError("Некорректная отметка об удалении")
    for field in ("score", "clock", "period_scores"):
        label(value.get(field), 200, True)
    if value.get("primary"):
        validate_market(value["primary"], parent)
    return value


def ingest(repo, body):
    if body.get("format") != FORMAT:
        raise ValueError("Неизвестный формат архива")
    mode = body.get("mode")
    received = now()
    quarantined = 0
    with repo.connection() as db:
        if mode == "status":
            value = body["status"]
            timestamp(value["checked_at"])
            for n in ("events", "results", "lines", "source_cursor", "sent_cursor"):
                if type(value.get(n)) is not int or value[n] < 0:
                    raise ValueError("Некорректный счётчик")
            clean = {
                k: value.get(k)
                for k in (
                    "checked_at",
                    "events",
                    "results",
                    "lines",
                    "source_cursor",
                    "sent_cursor",
                    "last_snapshot_at",
                    "worker_state",
                    "worker_job",
                )
            }
            if clean["last_snapshot_at"]:
                timestamp(clean["last_snapshot_at"])
            if clean["worker_state"] not in (
                "collecting",
                "ok",
                "partial",
                "error",
                "storage_low",
                "backup_error",
                "stopped",
                "unknown",
                "check_ok",
                "check_failed",
            ):
                raise ValueError("Неизвестное состояние worker")
            if clean["worker_job"] not in ("fast", "full", "supplemental", None):
                raise ValueError("Неизвестное задание")
            db.execute(
                "INSERT OR REPLACE INTO archive_status VALUES (1,?,?)",
                (json.dumps(clean), received),
            )
        elif mode == "feed":
            cycle = body["cycle"]
            if not re.fullmatch(r"[a-f0-9]{64}", cycle):
                raise ValueError("Некорректный цикл")
            values = body["events"]
            if not isinstance(values, list) or not 1 <= len(values) <= 200:
                raise ValueError("Некорректный пакет")
            values = [validate_event(v) for v in values]
            db.executemany(
                "INSERT OR REPLACE INTO archive_feed_rows VALUES (?,?,?)",
                [(cycle, v["event_id"], packed(v)) for v in values],
            )
        elif mode == "commit":
            cycle = body["cycle"]
            source_at = timestamp(body["source_at"])
            expected = body["expected"]
            if (
                not re.fullmatch(r"[a-f0-9]{64}", cycle)
                or type(expected) is not int
                or not 0 <= expected <= 20000
            ):
                raise ValueError("Некорректный цикл")
            count = db.execute(
                "SELECT count(*) FROM archive_feed_rows WHERE cycle=?", (cycle,)
            ).fetchone()[0]
            if count != expected:
                raise ValueError("Получен не весь снимок")
            old = db.execute("SELECT source_at FROM archive_feed WHERE id=1").fetchone()
            if old and instant(old["source_at"]) > instant(source_at):
                return {"accepted": False, "reason": "older_feed"}
            db.execute(
                "INSERT OR REPLACE INTO archive_feed VALUES (1,?,?,?)",
                (cycle, source_at, received),
            )
            for header in db.execute(
                "SELECT event_id,payload FROM archive_feed_rows WHERE cycle=?", (cycle,)
            ).fetchall():
                db.execute(
                    "INSERT OR REPLACE INTO archive_headers VALUES (?,?,?)",
                    (header["event_id"], header["payload"], source_at),
                )
            db.execute("DELETE FROM archive_feed_rows WHERE cycle<>?", (cycle,))
        elif mode == "detail":
            value = validate_event(body["event"])
            markets = body["markets"]
            if not isinstance(markets, list) or len(markets) > 2000:
                raise ValueError("Слишком много рынков")
            for m in markets:
                validate_market(m, value["event_id"])
            db.execute(
                "INSERT OR REPLACE INTO archive_details VALUES (?,?,?)",
                (
                    value["event_id"],
                    packed(
                        {
                            "event": value,
                            "markets": markets,
                            "truncated": bool(body.get("truncated")),
                        }
                    ),
                    received,
                ),
            )
        elif mode == "results":
            source = body["source"]
            rows = body["rows"]
            if (
                not re.fullmatch(r"[a-f0-9]{64}", source)
                or not isinstance(rows, list)
                or len(rows) > 500
            ):
                raise ValueError("Некорректный пакет")
            for row in rows:
                r = row["data"]
                cursor = row["cursor"]
                observed = timestamp(row["observed_at"])
                if (
                    type(cursor) is not int
                    or cursor < 1
                    or type(row["sport_id"]) is not int
                ):
                    raise ValueError("Некорректный курсор")
                if not re.fullmatch(r"[a-f0-9]{64}", row["result_key"]):
                    raise ValueError("Некорректный ключ результата")
                for field in ("member1", "member2", "championship"):
                    label(r.get(field), 500)
                label(r.get("score"), 500, True)
                unidentified = r.get("date") is None or not (
                    r.get("member1") or r.get("member2")
                )
                future_result = r.get("date") is not None and instant(r["date"]) > instant(
                    observed
                ) + timedelta(minutes=2)
                result_identity = {
                    k: r.get(k)
                    for k in ("idRadar", "date", "championship", "member1", "member2")
                }
                if (
                    digest(encode([row["sport_id"], result_identity]))
                    != row["result_key"]
                ):
                    raise ValueError("Ключ результата не соответствует встрече")
                label(row["sport_label"], 100)
                payload = encode(r)
                for existing in db.execute(
                    """SELECT result_key,sport_id,sport_label,observed_at,payload
                    FROM archive_versions WHERE source=? AND cursor=? UNION ALL
                    SELECT result_key,sport_id,sport_label,observed_at,payload
                    FROM archive_quarantined_results WHERE source=? AND cursor=?""",
                    (source, cursor, source, cursor),
                ):
                    if tuple(existing) != (
                        row["result_key"], row["sport_id"], row["sport_label"], observed, payload,
                    ):
                        raise ValueError("Курсор относится к другой версии результата")
                if future_result:
                    # A malformed observation must not poison the whole batch.
                    # Preserve it outside BOTH result tables: even once its date
                    # has passed, it is not a confirmed historical result.
                    db.execute(
                        "INSERT OR IGNORE INTO archive_quarantined_results VALUES (?,?,?,?,?,?,?,?,?)",
                        (source, cursor, row["result_key"], row["sport_id"], row["sport_label"],
                         observed, received, payload, "future_result"),
                    )
                    quarantined += 1
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO archive_versions VALUES (?,?,?,?,?,?,?,?)",
                    (
                        source,
                        cursor,
                        row["result_key"],
                        row["sport_id"],
                        row["sport_label"],
                        observed,
                        received,
                        payload,
                    ),
                )
                # The source also emits empty result placeholders. Retain the
                # observation without turning it into a completed fixture.
                if unidentified:
                    continue
                old = db.execute(
                    "SELECT observed_at FROM archive_latest_results WHERE result_key=?",
                    (row["result_key"],),
                ).fetchone()
                if not old or instant(old["observed_at"]) < instant(observed):
                    db.execute(
                        "INSERT OR REPLACE INTO archive_latest_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            row["result_key"],
                            row["sport_id"],
                            row["sport_label"],
                            r["date"],
                            r["member1"],
                            r["member2"],
                            r.get("score"),
                            observed,
                            received,
                            payload,
                        ),
                    )
        else:
            raise ValueError("Неизвестный пакет")
    return {"accepted": True, **({"quarantined": quarantined} if quarantined else {})}


def overview(repo):
    with repo.connection() as db:
        row = db.execute("SELECT * FROM archive_status WHERE id=1").fetchone()
        value = json.loads(row["payload"]) if row else {}
        value.update(
            connected=bool(row and age(row["received_at"]) < 90),
            received_at=row["received_at"] if row else None,
            stored_results=db.execute(
                "SELECT count(*) FROM archive_latest_results"
            ).fetchone()[0],
            unidentified_results=db.execute(
                "SELECT count(DISTINCT v.result_key) FROM archive_versions v LEFT JOIN archive_latest_results r ON v.result_key=r.result_key WHERE r.result_key IS NULL"
            ).fetchone()[0],
            quarantined_results=db.execute(
                """SELECT count(DISTINCT q.result_key) FROM archive_quarantined_results q
                LEFT JOIN archive_latest_results r ON r.result_key=q.result_key
                WHERE r.result_key IS NULL"""
            ).fetchone()[0],
            sports=[
                dict(r)
                for r in db.execute(
                    "SELECT sport_id,sport_label,count(*) total FROM archive_latest_results GROUP BY sport_id,sport_label"
                )
            ],
        )
        value["source_stale"] = (
            not value.get("last_snapshot_at") or age(value["last_snapshot_at"]) > 45
        )
        value["available"] = bool(db.execute("SELECT 1 FROM archive_feed").fetchone())
        return value


def sport_key(name, sport_id):
    return next(
        (s.key for s in SPORTS if key(s.label) == key(name)), f"winline_{sport_id}"
    )


def document(repo, parent):
    with repo.connection() as db:
        row = db.execute(
            "SELECT r.payload FROM archive_feed_rows r JOIN archive_feed f ON r.cycle=f.cycle WHERE r.event_id=?",
            (parent,),
        ).fetchone()
        present = bool(row)
        if not row:
            row = db.execute(
                "SELECT payload FROM archive_headers WHERE event_id=?", (parent,)
            ).fetchone()
        if not row:
            return None
        e = unpacked(row["payload"])
        markets = [e["primary"]] if e.get("primary") else []
        detail = db.execute(
            "SELECT payload FROM archive_details WHERE event_id=?", (parent,)
        ).fetchone()
        if detail:
            d = unpacked(detail["payload"])
            de = d["event"]
            if (
                de["members"] == e["members"]
                and de["sport_id"] == e["sport_id"]
                and de["is_live"] == e["is_live"]
                and de.get("kickoff_at") == e.get("kickoff_at")
            ):
                markets = d["markets"]
                if e.get("primary"):
                    markets = [
                        m for m in markets if m["key"] != e["primary"]["key"]
                    ] + [e["primary"]]
    result = project(e, markets)
    result["source_present"] = present
    if present:
        from .popular_markets import companion
        legacy=repo.event_markets(parent)
        popular=companion(result,legacy)
        if popular:result.update(popular_markets=popular,popular_complete=legacy.get('popular_complete',False))
    if not present:
        result["phase"] = "unknown"
        result["is_stale"] = True
        for m in result["markets"]:
            m["is_stale"] = True
        with repo.connection() as db:
            db.create_function("norm", 1, key)
            matches = db.execute(
                """SELECT payload,observed_at,received_at FROM archive_latest_results
              WHERE sport_id=? AND norm(sport_label)=? AND norm(member1)=? AND norm(member2)=?
              AND abs(julianday(start_at)-julianday(?))*86400<120 LIMIT 2""",
                (
                    e["sport_id"],
                    key(e["sport_label"]),
                    key(result["home"] or ""),
                    key(result["away"] or ""),
                    e["kickoff_at"],
                ),
            ).fetchall()
        if len(matches) == 1:
            final = json.loads(matches[0]["payload"])
            result["phase"] = "finished"
            result["final_result"] = {
                "score_text": final.get("score"),
                "observed_at": matches[0]["observed_at"],
                "received_at": matches[0]["received_at"],
            }
        result["content_version"] = identity(
            [result["content_version"], result["phase"], result.get("final_result")]
        )
    return result


def project(e, markets=None):
    markets = (
        markets if markets is not None else ([e["primary"]] if e.get("primary") else [])
    )
    at = e["observed_at"]
    members = e["members"]
    phase = (
        "live"
        if e["is_live"]
        else "prematch"
        if e.get("kickoff_at") and instant(e["kickoff_at"]) > datetime.now(UTC)
        else "unknown"
    )
    for m in markets:
        m["age_seconds"] = age(m["observed_at"])
        m["is_stale"] = m["age_seconds"] > (45 if phase=='live' else 180)
    version = identity(
        [
            e["members"],
            e["kickoff_at"],
            phase,
            e["score"],
            e["clock"],
            e["period_scores"],
            e.get("red_card_presence"),
            [(m["key"], m["name"], m["parameter"], m["outcomes"]) for m in markets],
        ]
    )
    result = {
        "source": "winline",
        "source_kind": "stream",
        "source_event_id": e["event_id"],
        "featured_rank": e.get("featured_rank"),
        "sport": sport_key(e["sport_label"], e["sport_id"]),
        "sport_label": e["sport_label"],
        "archive_sport_id": e["sport_id"],
        "radar_id": e.get("radar_id"),
        "home": members[0],
        "away": members[1] if len(members) > 1 else None,
        "title": " — ".join(filter(None, members)),
        "event_kind": "match" if len(members) == 2 and members[1] else "outright",
        "competition": e["competition"],
        "phase": phase,
        "kickoff_at": e["kickoff_at"],
        "url": f"https://winline.ru/stavki/event/{e['event_id']}",
        "observed_at": at,
        "capture_started_at": at,
        "age_seconds": age(at),
        "is_stale": age(at) > (45 if phase=='live' else 180),
        "live_score": e["score"],
        "live_clock": e["clock"],
        "period_scores": e["period_scores"],
        "red_card_presence": e.get("red_card_presence"),
        "content_version": version,
        "source_present": True,
        "markets": markets,
        "primary_market_key": e["primary"]["key"] if e.get("primary") else None,
        "coverage": {
            "complete": False,
            "captured_markets": len(markets),
            "captured_outcomes": sum(len(m["outcomes"]) for m in markets),
            "expected_markets": e.get("expected_markets"),
            "scope": "observed_stream",
        },
    }
    result["headline"] = headline_preview(result)
    return result


def catalog(repo, **query):
    with repo.connection() as db:
        rows = db.execute(
            "SELECT r.payload FROM archive_feed_rows r JOIN archive_feed f ON r.cycle=f.cycle"
        ).fetchall()
    events = [project(unpacked(r["payload"])) for r in rows]
    summary = {
        "upcoming": sum(e["phase"] == "prematch" for e in events),
        "live": sum(e["phase"] == "live" for e in events),
        "fresh": sum(not e["is_stale"] for e in events),
        "last_observed_at": max((e["observed_at"] for e in events), default=None),
    }
    sports = {}
    for e in events:
        sport = sports.setdefault(
            e["sport"], {"key": e["sport"], "label": e["sport_label"], "total": 0}
        )
        sport["total"] += 1
    selected = [
        e for e in events if not query.get("sport") or e["sport"] == query["sport"]
    ]
    competitions = sorted({e["competition"] for e in selected})
    if not query.get("include_started", False):
        selected = [e for e in selected if e["phase"] == "prematch"]
    for name in ("phase", "competition"):
        value = query.get(name)
        if value:
            selected = [
                e
                for e in selected
                if e[name] == ("prematch" if value == "upcoming" else value)
            ]
    if query.get("search"):
        selected = [
            e
            for e in selected
            if key(query["search"]) in key(e["title"] + " " + e["competition"])
        ]
    if query.get("event_ids") is not None:
        selected = [e for e in selected if e["source_event_id"] in query["event_ids"]]
    if query.get("day"):
        selected = [
            e
            for e in selected
            if e["kickoff_at"]
            and (instant(e["kickoff_at"]) + timedelta(hours=3)).date().isoformat()
            == query["day"]
        ]
    selected.sort(
        key=lambda e: (
            e["observed_at"]
            if query.get("sort") == "freshness"
            else e["kickoff_at"] or "9999"
        ),
        reverse=query.get("sort") == "freshness",
    )
    offset, limit = query.get("offset", 0), query.get("limit", 100)
    compact = []
    for e in selected[offset : offset + limit]:
        value = {k: v for k, v in e.items() if k != "markets"}
        value["primary_odds"] = value.get("headline")
        compact.append(value)
    return {
        "events": compact,
        "total": len(selected),
        "offset": offset,
        "limit": limit,
        "summary": summary,
        "sports": list(sports.values()),
        "competitions": competitions,
        "source_kind": "stream",
    }


def watch(repo, parent):
    doc = document(repo, parent)
    if not doc or not doc.get("source_present", True):
        raise LookupError("Матч не найден в потоке")
    with repo.connection() as db:
        db.execute(
            "INSERT OR REPLACE INTO archive_watches VALUES (?,?)", (parent, now())
        )
    return {"queued": True}


def watches(repo):
    cutoff = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    with repo.connection() as db:
        return [
            int(r["event_id"])
            for r in db.execute(
                """SELECT event_id FROM (
                   SELECT event_id,requested_at FROM archive_watches WHERE requested_at>?
                   UNION ALL SELECT event_id,requested_at FROM statistics_request_jobs
                   WHERE state IN ('queued','running') AND requested_at>?
                   AND json_extract(event_data,'$.source_kind')='stream'
                   ) GROUP BY event_id ORDER BY max(requested_at) DESC LIMIT 4""",
                (cutoff,(datetime.now(UTC)-timedelta(minutes=5)).isoformat()),
            )
        ]


def results(repo, search="", sport=None, limit=50, offset=0):
    with repo.connection() as db:
        db.create_function("norm", 1, key)
        where = "WHERE 1"
        args = []
        if search:
            where += ' AND instr(norm(member1||" "||member2||" "||payload),?)>0'
            args.append(key(search))
        if sport is not None:
            where += " AND sport_id=?"
            args.append(sport)
        total = db.execute(
            "SELECT count(*) FROM archive_latest_results " + where, args
        ).fetchone()[0]
        rows = db.execute(
            "SELECT * FROM archive_latest_results "
            + where
            + " ORDER BY start_at DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        return {
            "results": [
                {
                    k: r[k]
                    for k in (
                        "result_key",
                        "sport_id",
                        "sport_label",
                        "start_at",
                        "member1",
                        "member2",
                        "score",
                        "observed_at",
                    )
                }
                for r in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


def history(repo, doc, as_of=None):
    at = instant(as_of or now())
    parent = doc["source_event_id"]
    native = document(repo, parent)
    if not native:
        return {
            "state": "unavailable",
            "reason": "История ещё не доставлена",
            "teams": [],
            "head_to_head": [],
        }
    if [key(doc.get("home") or ""), key(doc.get("away") or "")] != [
        key(native["home"] or ""),
        key(native["away"] or ""),
    ] or key(doc.get("sport_label") or "") != key(native["sport_label"]):
        return {
            "state": "mismatch",
            "reason": "Участники или вид спорта не совпадают с архивом",
            "teams": [],
            "head_to_head": [],
        }
    if (
        doc.get("kickoff_at")
        and native.get("kickoff_at")
        and abs(
            (instant(doc["kickoff_at"]) - instant(native["kickoff_at"])).total_seconds()
        )
        > 120
    ):
        return {
            "state": "mismatch",
            "reason": "Время встречи не совпадает с архивом",
            "teams": [],
            "head_to_head": [],
        }
    fixture_cutoff = (
        min(at, instant(doc["kickoff_at"])) if doc.get("kickoff_at") else at
    )
    with repo.connection() as db:
        rows = db.execute(
            """WITH ranked AS (
          SELECT *,row_number() OVER(PARTITION BY result_key ORDER BY observed_at DESC,received_at DESC) n
          FROM archive_versions WHERE sport_id=? AND observed_at<=?
          AND received_at<=?) SELECT * FROM ranked WHERE n=1""",
            (
                native["archive_sport_id"],
                at.isoformat(timespec="microseconds"),
                at.isoformat(timespec="microseconds"),
            ),
        ).fetchall()
    members = [doc.get("home") or "", doc.get("away") or ""]
    keys = [key(n) for n in members]
    eligible = []
    for r in rows:
        if key(r["sport_label"]) != key(native["sport_label"]):
            continue
        value = json.loads(r["payload"])
        if not value.get("date") or not (value.get("member1") or value.get("member2")):
            continue
        if native.get("radar_id") and value.get("idRadar") == native["radar_id"]:
            continue
        pair = [key(value["member1"]), key(value["member2"])]
        if instant(value["date"]) >= fixture_cutoff or not any(
            k and k in pair for k in keys
        ):
            continue
        eligible.append(
            {
                "result_key": r["result_key"],
                "start_at": value["date"],
                "members": [value["member1"], value["member2"]],
                "score_text": value.get("score"),
                "championship": value.get("championship"),
                "observed_at": r["observed_at"],
                "received_at": r["received_at"],
            }
        )
    eligible.sort(key=lambda r: instant(r["start_at"]), reverse=True)
    teams = []
    for member, k in zip(members, keys):
        rows = [r for r in eligible if k and k in [key(n) for n in r["members"]]]
        teams.append(
            {
                "name": member,
                "available_result_records": len(rows),
                "recent_results": rows[:5],
                "truncated": len(rows) > 5,
            }
        )
    head = [r for r in eligible if {key(n) for n in r["members"]} == set(keys)]
    payload = {
        "state": "ready" if eligible else "empty",
        "as_of": at.isoformat(),
        "teams": teams,
        "head_to_head": head[:3],
        "head_to_head_truncated": len(head) > 3,
        "source": "Winline — накопленная история",
        "binding": "event_id_participants_sport_kickoff",
        "mode": "live"
        if doc["phase"] == "live"
        else "prematch"
        if doc.get("kickoff_at") and instant(doc["kickoff_at"]) > at
        else "retrospective",
        "reason": None if eligible else "Совпадающие результаты ещё не накоплены",
    }
    payload["revision"] = identity([{k: v for k, v in payload.items() if k != "as_of"}])
    return payload
