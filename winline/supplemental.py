#!/usr/bin/env python3
"""Fetch explicitly advertised player and outright markets separately."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import Error, sync_playwright

try:
    from .browser import proxy_ca_args
    from .session import SourceSession
    from .storage import DEFAULT_DB, connect, digest, encode, now
except ImportError:
    from browser import proxy_ca_args
    from session import SourceSession
    from storage import DEFAULT_DB, connect, digest, encode, now

INSTALL = """() => {
 const a=window.apiWlb,s=window.__supplemental={pending:null};
 const reply=(kind,data)=>{
   if(s.pending?.kind===kind){s.pending.resolve(JSON.stringify(data));s.pending=null}
 };
 a.client.addEventListener(d=>reply('players',d),a.API_PARAMETS.CLIENT_EVENTS.GET_PLAYERS_LINES);
 a.client.addEventListener(d=>reply('outrights',d),
   a.API_PARAMETS.CLIENT_EVENTS.GET_SPECIAL_EVENTS_PLUS_ARRAY);
}"""
REQUEST = """({kind,event})=>new Promise((resolve,reject)=>{
 const s=window.__supplemental;
 const timer=setTimeout(()=>{s.pending=null;reject(Error('Supplemental request timed out'))},15000);
 s.pending={kind,resolve:data=>{clearTimeout(timer);resolve(data)}};
 const service=window.apiWlb.getService('EventService');
 if(kind==='players') service.getPlayersLines(event); else service.getOutrights(event);
})"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database", type=Path, default=DEFAULT_DB)
    p.add_argument("--chromium")
    p.add_argument("--proxy-ca", help="PEM сертификат доверенного прокси")
    p.add_argument("--limit", type=int, default=0, help="0 = все объявленные запросы")
    p.add_argument("--delay", type=float, default=0.35)
    args = p.parse_args()
    if args.delay < 0.1 or args.limit < 0:
        p.error("Неверный предел или интервал")
    db = connect(args.database)
    db.execute("PRAGMA busy_timeout=30000")
    capture = db.execute(
        "INSERT INTO captures(started_at,url) VALUES (?,?)",
        (now(), "https://winline.ru/"),
    ).lastrowid
    db.execute(
        "INSERT INTO collection_runs(capture_id,settings_json,status) VALUES (?,?,?)",
        (
            capture,
            encode({"mode": "supplemental", "limit": args.limit, "delay": args.delay}),
            "running",
        ),
    )
    db.commit()
    current_job = None
    try:
        ca = "/usr/local/share/ca-certificates/environment-proxy-ca.crt"
        ca = (
            ca
            if os.environ.get("NODE_EXTRA_CA_CERTS") == ca and Path(ca).is_file()
            else None
        )
        opts = {"headless": True, "args": proxy_ca_args(args.proxy_ca or ca)}
        if args.chromium:
            opts["executable_path"] = args.chromium
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:
            opts["proxy"] = {"server": proxy}
        with sync_playwright() as p:
            b = p.chromium.launch(**opts)
            page = b.new_page()
            page.goto(
                "https://winline.ru/", wait_until="domcontentloaded", timeout=45000
            )
            page.wait_for_function(
                "window.apiWlb?.getEvents().toArray().length > 0", timeout=45000
            )
            page.wait_for_timeout(3000)
            page.evaluate(INSTALL)
            session = SourceSession(page, db, capture, args.database)
            session.tick(force=True)
            jobs = page.evaluate("""() => window.apiWlb.getEvents().toArray().flatMap(e=>{
              if(e.isLive) return [];
              const out=[];
              if(e.hasPlayers?.())out.push({event:e.id,kind:'players'});
              if(e.hasOutrights?.())out.push({event:e.id,kind:'outrights'});
              return out;
            })""")
            if args.limit:
                jobs = jobs[: args.limit]
            version = page.evaluate("window.apiWlb.version")
            db.execute(
                "UPDATE collection_runs SET planned_market_requests=?,sdk_version=? "
                "WHERE capture_id=?",
                (len(jobs), str(version), capture),
            )
            db.commit()
            print(f"Дополнительные режимы: {len(jobs)} запросов", flush=True)
            for i, job in enumerate(jobs, 1):
                current_job = job
                raw = encode(json.loads(page.evaluate(REQUEST, job)))
                at = now()
                with db:
                    old = db.execute(
                        "SELECT content_hash FROM supplemental WHERE event_id=? AND kind=?",
                        (job["event"], job["kind"]),
                    ).fetchone()
                    h = digest(raw)
                    if old is None or old["content_hash"] != h:
                        db.execute(
                            "INSERT INTO supplemental_history(event_id,kind,observed_at,raw_json) "
                            "VALUES (?,?,?,?)",
                            (job["event"], job["kind"], at, raw),
                        )
                    db.execute(
                        """INSERT INTO supplemental VALUES (?,?,?,?,?,?)
                      ON CONFLICT(event_id,kind) DO UPDATE SET last_seen=excluded.last_seen,
                      capture_id=excluded.capture_id,raw_json=excluded.raw_json,
                      content_hash=excluded.content_hash""",
                        (job["event"], job["kind"], at, capture, raw, h),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO market_requests VALUES (?,?,?,?,?,?)",
                        (
                            capture,
                            job["event"],
                            job["kind"],
                            at,
                            len(json.loads(raw)),
                            None,
                        ),
                    )
                session.tick()
                current_job = None
                if i % 50 == 0 or i == len(jobs):
                    print(f"Дополнительные режимы: {i}/{len(jobs)}", flush=True)
                page.wait_for_timeout(args.delay * 1000)
            session.tick(force=True)
            b.close()
        with db:
            db.execute("UPDATE captures SET finished_at=? WHERE id=?", (now(), capture))
            db.execute(
                "UPDATE collection_runs SET status='complete' WHERE capture_id=?",
                (capture,),
            )
        return 0
    except (
        Error,
        OSError,
        ValueError,
        sqlite3.Error,
        subprocess.CalledProcessError,
        KeyboardInterrupt,
    ) as exc:
        with db:
            if current_job:
                db.execute(
                    "INSERT OR REPLACE INTO market_requests VALUES (?,?,?,?,?,?)",
                    (
                        capture,
                        current_job["event"],
                        current_job["kind"],
                        now(),
                        None,
                        str(exc) or type(exc).__name__,
                    ),
                )
            db.execute(
                "UPDATE captures SET finished_at=?,error=? WHERE id=?",
                (now(), str(exc) or type(exc).__name__, capture),
            )
            db.execute(
                "UPDATE collection_runs SET status='failed' WHERE capture_id=?",
                (capture,),
            )
        print(f"Ошибка дополнительных режимов: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
