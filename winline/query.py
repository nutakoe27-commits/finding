#!/usr/bin/env python3
"""Query locally stored Winline data without accessing the network."""

import argparse
import csv
import io
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

try:
    from .storage import DEFAULT_DB
except ImportError:
    from storage import DEFAULT_DB


def read_query(sql, parameters=(), database=DEFAULT_DB):
    """Run a read-only query, returning a list of dictionaries."""
    uri = "file:" + quote(str(Path(database).resolve())) + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.create_function(
            "casefold", 1, lambda x: str(x or "").casefold(), deterministic=True
        )
        return [dict(row) for row in db.execute(sql, parameters)]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database", type=Path, default=DEFAULT_DB)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--output", type=Path, help="Путь для JSON/CSV; по умолчанию stdout")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("stats", help="Количество записей и время сбора")
    sub.add_parser("coverage", help="Статусы и фактический охват запусков")
    for cmd in ["events", "results"]:
        s = sub.add_parser(cmd)
        s.add_argument(
            "--sport", type=int, help="ID спорта; справочник доступен через SQL"
        )
        s.add_argument("--team", help="Часть названия участника, без учёта регистра")
        s.add_argument("--from-date", help="Нижняя граница start_at, ISO UTC")
        s.add_argument(
            "--to-date", help="Верхняя граница start_at (исключительно), ISO UTC"
        )
        s.add_argument("--limit", type=int, default=100)
        if cmd == "events":
            s.add_argument("--live", action="store_true")
            s.add_argument(
                "--include-missing",
                action="store_true",
                help="Включить ранее наблюдавшиеся события, отсутствующие в последнем снимке",
            )
    s = sub.add_parser("supplemental", help="Рынки игроков и долгосрочные исходы")
    s.add_argument("--event", type=int, required=True)
    s = sub.add_parser("odds")
    s.add_argument("--event", type=int, required=True)
    s.add_argument("--include-missing", action="store_true")
    s = sub.add_parser("history")
    s.add_argument("--event", type=int, required=True)
    s.add_argument(
        "--line", type=int, help="История линии; без ключа — история события"
    )
    s = sub.add_parser("sql", help="Произвольный SQL в соединении только для чтения")
    s.add_argument("statement")
    args = p.parse_args()
    params = []
    if args.command == "stats":
        sql = """SELECT (SELECT count(*) FROM events) events_ever_seen,
          (SELECT count(*) FROM matches WHERE present_in_latest_snapshot) current_events,
          (SELECT count(*) FROM matches WHERE present_in_latest_snapshot AND is_live) live_events,
          (SELECT count(*) FROM events
           WHERE snapshot_id=(SELECT max(id) FROM snapshots) AND is_live=0) prematch_events,
          (SELECT count(*) FROM latest_odds WHERE present_in_latest_snapshot) current_lines,
          (SELECT sum(json_array_length(odds_json)) FROM latest_odds
           WHERE present_in_latest_snapshot) current_outcomes,
          (SELECT count(DISTINCT sport_id) FROM matches
           WHERE present_in_latest_snapshot) current_sports,
          (SELECT sum(json_array_length(raw_json)) FROM supplemental
           WHERE kind='players') supplemental_player_lines,
          (SELECT sum(json_array_length(raw_json)) FROM supplemental
           WHERE kind='outrights') supplemental_outright_events,
          (SELECT count(*) FROM results) archived_results,
          (SELECT min(start_at) FROM results) oldest_result,
          (SELECT max(start_at) FROM results) newest_result,
          (SELECT max(observed_at) FROM snapshots) last_snapshot,
          (SELECT count(*) FROM result_requests WHERE error IS NOT NULL) failed_result_requests,
          (SELECT count(*) FROM market_requests WHERE error IS NOT NULL) failed_market_requests"""
    elif args.command == "coverage":
        sql = """SELECT r.*,c.started_at,c.finished_at,c.error,
          (SELECT count(*) FROM market_requests m
           WHERE m.capture_id=r.capture_id AND m.error IS NULL) AS successful_markets,
          (SELECT count(*) FROM market_requests m
           WHERE m.capture_id=r.capture_id AND m.error IS NOT NULL) AS failed_markets,
          (SELECT count(*) FROM result_requests q
           WHERE q.capture_id=r.capture_id AND q.error IS NULL) AS successful_result_requests,
          (SELECT count(*) FROM result_requests q
           WHERE q.capture_id=r.capture_id AND q.error IS NOT NULL) AS failed_result_requests
          FROM collection_runs r JOIN captures c ON c.id=r.capture_id ORDER BY r.capture_id DESC"""
    elif args.command in ("events", "results"):
        if args.limit < 1:
            p.error("--limit должно быть положительным")
        table = "matches" if args.command == "events" else "results"
        where = []
        if args.command == "events":
            if not args.include_missing:
                where.append("present_in_latest_snapshot=1")
            if args.live:
                where.append("is_live=1")
        if args.sport is not None:
            where.append("sport_id=?")
            params.append(args.sport)
        if args.team:
            field = (
                "members_json"
                if args.command == "events"
                else "member1 || ' ' || member2"
            )
            where.append(f"instr(casefold({field}),?)>0")
            params.append(args.team.casefold())
        for field, op in [("from_date", ">="), ("to_date", "<")]:
            value = getattr(args, field)
            if value:
                where.append(f"start_at{op}?")
                params.append(value)
        sql = (
            f"SELECT * FROM {table}"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY start_at LIMIT ?"
        )
        params.append(args.limit)
    elif args.command == "supplemental":
        sql = "SELECT * FROM supplemental WHERE event_id=?"
        params = [args.event]
    elif args.command == "odds":
        sql = "SELECT * FROM latest_odds WHERE event_id=?"
        params.append(args.event)
        if not args.include_missing:
            sql += " AND present_in_latest_snapshot=1"
    elif args.command == "history":
        if args.line is None:
            sql = "SELECT * FROM event_history WHERE event_id=? ORDER BY id"
            params = [args.event]
        else:
            sql = (
                "SELECT * FROM line_history WHERE event_id=? AND line_id=? ORDER BY id"
            )
            params = [args.event, args.line]
    else:
        sql = args.statement
    try:
        rows = read_query(sql, params, args.database)
    except (sqlite3.Error, OSError) as e:
        print(f"Ошибка чтения базы: {e}", file=sys.stderr)
        return 1
    if args.format == "json":
        # Decode stored JSON columns for convenient use by other programs.
        for row in rows:
            for k in list(row):
                if k.endswith("_json") and row[k] is not None:
                    row[k[:-5]] = json.loads(row.pop(k))
        output = json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    else:
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        output = buf.getvalue()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
