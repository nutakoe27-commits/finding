"""Single-writer server worker for persistent public Winline history."""

import argparse
import fcntl
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from .backup import backup_database
from .storage import now

STOP = False


def write_status(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def last_run(database):
    if not database.exists():
        return None
    uri = "file:" + quote(str(database.resolve())) + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("""SELECT r.capture_id,r.status,c.error,
          (SELECT count(*) FROM snapshots s WHERE s.capture_id=r.capture_id) snapshots,
          r.planned_result_requests,
          (SELECT count(*) FROM result_requests q WHERE q.capture_id=r.capture_id
           AND q.error IS NULL) successful_result_requests
          FROM collection_runs r JOIN captures c ON c.id=r.capture_id
          ORDER BY r.capture_id DESC LIMIT 1""").fetchone()
        return dict(row) if row else None


def command(database, kind, chromium=None, initial=False, seconds=300):
    args = [
        sys.executable,
        "-m",
        "winline.supplemental" if kind == "supplemental" else "winline.collect",
        "--database",
        str(database),
    ]
    if chromium:
        args += ["--chromium", chromium]
    if kind == "fast":
        args += [
            "--no-expand-markets",
            "--history-days",
            "0",
            "--seconds",
            str(seconds),
        ]
    elif kind == "full":
        args += [
            "--history-days",
            "30" if initial else "2",
            "--seconds",
            "0",
            "--market-batch-size",
            "1",
            "--request-delay",
            "0.5",
        ]
    elif kind == "check":
        args += ["--no-expand-markets", "--history-days", "0", "--seconds", "5"]
    return args


def run_child(args):
    """Forward a graceful stop to the whole isolated browser/process group."""
    child = subprocess.Popen(args, start_new_session=True)
    interrupted_at = None
    while child.poll() is None:
        if STOP and interrupted_at is None:
            try:
                os.killpg(child.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            interrupted_at = time.monotonic()
        if interrupted_at is not None and time.monotonic() - interrupted_at > 60:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.25)
    return child.returncode


def request_stop(_signal, _frame):
    global STOP
    STOP = True


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", type=Path, default=Path("/var/lib/winline-archive"))
    p.add_argument("--chromium")
    p.add_argument("--check", action="store_true", help="One bounded real source probe")
    p.add_argument(
        "--once", action="store_true", help="Run one fast collection and stop"
    )
    p.add_argument("--stream-seconds", type=int, default=300)
    p.add_argument("--full-hours", type=float, default=6)
    p.add_argument("--backup-hours", type=float, default=24)
    p.add_argument("--min-free-mib", type=int, default=2048)
    args = p.parse_args()
    if (
        args.stream_seconds < 5
        or args.full_hours < 1
        or args.backup_hours < 1
        or args.min_free_mib < 1
    ):
        p.error("Invalid intervals or disk threshold")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.state_dir, 0o700)
    database = args.state_dir / "winline.sqlite3"
    status_file = args.state_dir / "status.json"
    schedule_file = args.state_dir / "schedule.json"
    with (args.state_dir / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            p.exit(1, "Another archive worker is already running\n")
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        schedule = (
            json.loads(schedule_file.read_text()) if schedule_file.exists() else {}
        )
        if args.check:
            # Probe in its own DB, preserving the working database and its snapshot coverage.
            probe = args.state_dir / "probe.sqlite3"
            before = last_run(probe)
            code = run_child(command(probe, "check", args.chromium))
            observed = last_run(probe)
            success = (
                code == 0
                and observed
                and observed["status"] == "complete"
                and observed["snapshots"] > 0
                and (before is None or observed["capture_id"] > before["capture_id"])
            )
            write_status(
                status_file,
                {
                    "state": "check_ok" if success else "check_failed",
                    "at": now(),
                    "run": observed,
                },
            )
            return 0 if success else 1
        while not STOP:
            free = shutil.disk_usage(args.state_dir).free
            if free < args.min_free_mib * 1024**2:
                write_status(
                    status_file,
                    {"state": "storage_low", "at": now(), "free_bytes": free},
                )
                if args.once:
                    return 1
                for _ in range(60):
                    if STOP:
                        break
                    time.sleep(1)
                continue
            stamp = time.time()
            if args.once:
                kind = "fast"
            elif stamp - schedule.get("full", 0) >= args.full_hours * 3600:
                kind = "full"
            elif stamp - schedule.get("supplemental", 0) >= args.full_hours * 3600:
                kind = "supplemental"
            else:
                kind = "fast"
            before = last_run(database)
            write_status(status_file, {"state": "collecting", "at": now(), "job": kind})
            code = run_child(
                command(
                    database,
                    kind,
                    args.chromium,
                    initial=not schedule.get("archive_initialized"),
                    seconds=args.stream_seconds,
                )
            )
            observed = last_run(database)
            fresh = observed and (
                before is None or observed["capture_id"] > before["capture_id"]
            )
            accepted = fresh and observed["status"] in ("complete", "partial")
            state = (
                "ok" if code == 0 and accepted else "partial" if accepted else "error"
            )
            value = {
                "state": "stopped" if STOP else state,
                "at": now(),
                "job": kind,
                "exit_code": code,
                "run": observed,
                "database": str(database),
            }
            write_status(status_file, value)
            if STOP:
                break
            if accepted:
                if kind in ("full", "supplemental"):
                    schedule[kind] = time.time()
                if (
                    kind == "full"
                    and observed["planned_result_requests"] > 0
                    and observed["planned_result_requests"]
                    == observed["successful_result_requests"]
                ):
                    schedule["archive_initialized"] = True
                if time.time() - schedule.get("backup", 0) >= args.backup_hours * 3600:
                    try:
                        value["backup"] = str(
                            backup_database(database, args.state_dir / "backups")
                        )
                        schedule["backup"] = time.time()
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        value["state"] = "backup_error"
                        value["backup_error"] = str(exc)
                    write_status(status_file, value)
            schedule["failures"] = 0 if accepted else schedule.get("failures", 0) + 1
            write_status(schedule_file, schedule)
            if args.once:
                return 0 if state == "ok" else 1
            # Errors and partial runs never cause a rapid restart/retry loop.
            for _ in range(
                min(3600, 60 * 2 ** min(schedule["failures"], 6)) if not accepted else 5
            ):
                if STOP:
                    break
                time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
