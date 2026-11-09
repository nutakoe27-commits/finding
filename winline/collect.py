#!/usr/bin/env python3
"""Collect current events, odds and available finished results from Winline."""

import argparse
import gzip
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import Error, sync_playwright

try:
    from .browser import INSTALL, PLUS_BATCH, RESULT_REQUEST, proxy_ca_args
    from .session import SourceSession
    from .storage import DEFAULT_DB, connect, now, save_results
except ImportError:
    from browser import INSTALL, PLUS_BATCH, RESULT_REQUEST, proxy_ca_args
    from session import SourceSession
    from storage import DEFAULT_DB, connect, now, save_results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds", type=int, default=20, help="Наблюдать поток после выгрузки, секунд"
    )
    parser.add_argument(
        "--interval", type=float, default=10, help="Интервал снимков потока, секунд"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--url",
        choices=["https://winline.ru/", "https://winline.ru/live"],
        default="https://winline.ru/",
    )
    parser.add_argument("--chromium", help="Путь к установленному Chromium")
    parser.add_argument(
        "--proxy-ca", help="PEM сертификат доверенного прокси (проверка по SPKI)"
    )
    parser.add_argument(
        "--ignore-https-errors",
        action="store_true",
        help="Диагностика: отключить проверку сертификатов",
    )
    parser.add_argument(
        "--expand-markets",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Запрашивать дополнительные обычные рынки каждого прематч события",
    )
    parser.add_argument(
        "--market-batch-size",
        type=int,
        default=4,
        help="Не более 4 запросов рынков одновременно",
    )
    parser.add_argument(
        "--event-limit",
        type=int,
        default=0,
        help="Ограничить запросы дополнительных рынков; 0 = все",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=30,
        help="Дней архива от последнего дня источника; 0 = пропустить",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.35,
        help="Пауза между архивными/дополнительными запросами",
    )
    parser.add_argument(
        "--raw-frames",
        action="store_true",
        help="Дополнительно сохранять бинарные сообщения",
    )
    parser.add_argument(
        "--snapshot-files", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument(
        "--light-snapshots",
        action="store_true",
        help="Частые снимки: события, основные рынки и запрошенные дополнительные рынки",
    )
    args = parser.parse_args()
    if (
        not 0 <= args.seconds <= 86400
        or not 0 <= args.history_days <= 366
        or args.interval < 1
        or args.request_delay < 0.1
        or args.event_limit < 0
        or not 1 <= args.market_batch_size <= 4
    ):
        parser.error("Неверные интервалы, предел событий или число дней")
    db = connect(args.database)
    capture = db.execute(
        "INSERT INTO captures(started_at,url) VALUES (?,?)", (now(), args.url)
    ).lastrowid
    db.execute(
        "INSERT INTO collection_runs(capture_id,settings_json,status) VALUES (?,?,?)",
        (capture, json.dumps(vars(args), default=str, ensure_ascii=False), "running"),
    )
    db.commit()
    failed = False
    last_data = None
    frame_count = 0
    snapshot_count = 0
    save_path = args.database.parent / "snapshots" / f"capture-{capture}.json.gz"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        ca = args.proxy_ca
        if ca is None:
            managed_ca = "/usr/local/share/ca-certificates/environment-proxy-ca.crt"
            # Only auto-trust the configured managed proxy CA, never a downloaded CA.
            if (
                os.environ.get("NODE_EXTRA_CA_CERTS") == managed_ca
                and Path(managed_ca).is_file()
            ):
                ca = managed_ca
        with sync_playwright() as p:
            opts = {"headless": True, "args": proxy_ca_args(ca)}
            if args.chromium:
                opts["executable_path"] = args.chromium
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if proxy:
                opts["proxy"] = {"server": proxy}
            browser = p.chromium.launch(**opts)
            page = browser.new_page(ignore_https_errors=args.ignore_https_errors)
            page.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in ("image", "media", "font")
                    else route.continue_()
                ),
            )
            if args.raw_frames:

                def on_socket(ws):
                    if not ws.url.startswith("wss://wss.winline.ru/data_ng?"):
                        return

                    def on_frame(payload):
                        nonlocal frame_count
                        binary = isinstance(payload, bytes)
                        db.execute(
                            "INSERT INTO frames(capture_id,received_at,payload,is_binary) "
                            "VALUES (?,?,?,?)",
                            (
                                capture,
                                now(),
                                payload if binary else payload.encode(),
                                binary,
                            ),
                        )
                        frame_count += 1

                    ws.on("framereceived", on_frame)

                page.on("websocket", on_socket)
            page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_function(
                "window.apiWlb?.getEvents().toArray().length > 0 && "
                "window.apiWlb.client.getNumOfLinesPrematch() > 0",
                timeout=45000,
            )
            page.evaluate(INSTALL)
            # Allow the initial prematch and live packets to finish independently.
            page.wait_for_timeout(3000)

            session = SourceSession(
                page,
                db,
                capture,
                args.database,
                args.interval,
                light=args.light_snapshots,
            )

            def snapshot(force=True, event_ids=()):
                nonlocal last_data, snapshot_count
                value = session.tick(force=force, event_ids=event_ids)
                if value is None:
                    return
                last_data = value
                snapshot_count += 1
                if args.snapshot_files:
                    temporary = save_path.with_suffix(".tmp")
                    with gzip.open(
                        temporary, "wt", encoding="utf-8", compresslevel=3
                    ) as f:
                        json.dump(last_data, f, ensure_ascii=False)
                    temporary.replace(save_path)
                print(
                    f"Снимок {snapshot_count}: событий {len(last_data['events'])}, "
                    f"линий {len(last_data['lines'])}",
                    flush=True,
                )

            snapshot()
            db.execute(
                "UPDATE collection_runs SET sdk_version=? WHERE capture_id=?",
                (str(last_data.get("sdkVersion")), capture),
            )
            db.commit()
            if args.history_days:
                page.evaluate("window.apiWlb.getService('ResultService').getPreData()")
                page.wait_for_function(
                    "window.__winlineReader.pre !== null", timeout=20000
                )
                catalog = page.evaluate(
                    "JSON.parse(JSON.stringify(window.__winlineReader.pre))"
                )
                pairs = []
                for sport_id, sport in catalog["sports"].items():
                    dates = sport.get("dates", [])
                    dates = [int(d) for d in dates]
                    for day in dates:
                        db.execute(
                            "INSERT OR REPLACE INTO result_catalog VALUES (?,?,?,?)",
                            (int(sport_id), day, sport.get("name"), now()),
                        )
                        if day >= max(dates) - (args.history_days - 1) * 86400000:
                            pairs.append((int(sport_id), day))
                db.execute(
                    "UPDATE collection_runs SET planned_result_requests=? WHERE capture_id=?",
                    (len(pairs), capture),
                )
                db.commit()
                pairs.sort(key=lambda p: (-p[1], p[0]))
                print(
                    f"Архив: запланировано {len(pairs)} запросов спорт/день", flush=True
                )
                for i, (sport, day) in enumerate(pairs, 1):
                    try:
                        rows = page.evaluate(
                            RESULT_REQUEST,
                            {"sport": sport, "day": day, "timeout": 15000},
                        )
                        save_results(db, capture, sport, day, rows)
                    except Error as exc:
                        save_results(db, capture, sport, day, [], error=str(exc))
                        failed = True
                        print(
                            f"Ошибка архива {sport}/{day}: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                        # A late uncorrelated response must never be assigned to another sport.
                        break
                    snapshot(force=False)
                    if i % 50 == 0 or i == len(pairs):
                        print(f"Архив: {i}/{len(pairs)} запросов", flush=True)
                    page.wait_for_timeout(args.request_delay * 1000)
            if args.expand_markets:
                events = [
                    e
                    for e in last_data["events"]
                    if not e.get("isLive") and e.get("count_add_line") != 0
                ]
                if args.event_limit:
                    events = events[: args.event_limit]
                db.execute(
                    "UPDATE collection_runs SET planned_market_requests=? WHERE capture_id=?",
                    (len(events), capture),
                )
                db.commit()
                for offset in range(0, len(events), args.market_batch_size):
                    batch = events[offset : offset + args.market_batch_size]
                    replies = page.evaluate(
                        PLUS_BATCH,
                        {
                            "events": [e["id"] for e in batch],
                            "timeout": 12000,
                            "delay": args.request_delay * 1000,
                        },
                    )
                    for reply in replies:
                        error = reply.get("error")
                        failed = failed or bool(error)
                        db.execute(
                            "INSERT OR REPLACE INTO market_requests VALUES (?,?,?,?,?,?)",
                            (
                                capture,
                                reply["event"],
                                "ordinary",
                                now(),
                                reply.get("count"),
                                error,
                            ),
                        )
                        if error:
                            print(
                                f"Рынки {reply['event']}: {error}",
                                file=sys.stderr,
                                flush=True,
                            )
                    db.commit()
                    page.wait_for_timeout(args.request_delay * 1000)
                    done = min(offset + len(batch), len(events))
                    snapshot(force=False, event_ids=[e["id"] for e in batch])
                    if done % 100 == 0 or done == len(events):
                        print(f"Дополнительные рынки: {done}/{len(events)}", flush=True)
                snapshot()
            end = time.monotonic() + args.seconds
            while time.monotonic() < end:
                page.wait_for_timeout(min(args.interval, end - time.monotonic()) * 1000)
                snapshot()
            snapshot()
            db.execute(
                "UPDATE captures SET finished_at=?,title=?,page_text=?,error=? WHERE id=?",
                (
                    now(),
                    page.title(),
                    page.locator("body").inner_text(),
                    "Некоторые запросы не завершены; см. market_requests/result_requests"
                    if failed
                    else None,
                    capture,
                ),
            )
            db.execute(
                "UPDATE collection_runs SET status=? WHERE capture_id=?",
                ("partial" if failed else "complete", capture),
            )
            db.commit()
            browser.close()
        print(
            f"Сбор {capture} завершён; база {args.database}; "
            + (
                f"исходный снимок {save_path}"
                if args.snapshot_files
                else "сырые поля сохранены в SQLite"
            ),
            flush=True,
        )
        return 1 if failed else 0
    except (
        Error,
        OSError,
        ValueError,
        KeyError,
        sqlite3.Error,
        subprocess.CalledProcessError,
        KeyboardInterrupt,
    ) as exc:
        db.execute(
            "UPDATE captures SET finished_at=?,error=? WHERE id=?",
            (now(), str(exc) or type(exc).__name__, capture),
        )
        db.execute(
            "UPDATE collection_runs SET status=? WHERE capture_id=?",
            ("failed", capture),
        )
        db.commit()
        print(f"Ошибка сбора {capture}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
