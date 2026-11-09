"""Consistent, compressed SQLite backups, including committed WAL data."""

import gzip
import hashlib
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote


def backup_database(database, directory, keep=7):
    if keep < 1:
        raise ValueError("keep must be positive")
    database, directory = Path(database), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    uri = "file:" + quote(str(database.resolve())) + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source:
        size = (
            source.execute("PRAGMA page_count").fetchone()[0]
            * source.execute("PRAGMA page_size").fetchone()[0]
        )
    if shutil.disk_usage(directory).free < size * 2 + 2 * 1024**3:
        raise OSError(
            "Not enough free space for a consistent backup plus 2 GiB reserve"
        )
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=directory) as tmp:
        snapshot = Path(tmp) / "snapshot.sqlite3"
        with (
            closing(sqlite3.connect(uri, uri=True)) as src,
            closing(sqlite3.connect(snapshot)) as dst,
        ):
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite backup failed integrity check")
        compressed = Path(tmp) / "snapshot.gz"
        checksum = hashlib.sha256()
        with (
            snapshot.open("rb") as src,
            gzip.open(compressed, "wb", compresslevel=3) as dst,
        ):
            while block := src.read(1024 * 1024):
                checksum.update(block)
                dst.write(block)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"winline-{stamp}.sqlite3.gz"
        path = directory / name
        os.link(compressed, path)
        try:
            (directory / (name + ".sha256")).write_text(checksum.hexdigest() + "\n")
        except OSError:
            path.unlink()
            raise
    # Rotate only complete backups owned by this helper; never touch the working DB.
    files = sorted(
        (
            p
            for p in directory.glob("winline-*.sqlite3.gz")
            if p.is_file()
            and not p.is_symlink()
            and p.with_name(p.name + ".sha256").is_file()
        ),
        reverse=True,
    )
    for old in files[keep:]:
        old.unlink()
        old.with_name(old.name + ".sha256").unlink()
    return path


def restore_backup(archive, target):
    """Restore into a new path only. Never overwrite an existing database."""
    archive, target = Path(archive), Path(target)
    if target.exists() or target.is_symlink():
        raise FileExistsError("Target database already exists")
    expected = archive.with_name(archive.name + ".sha256").read_text().strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=target.parent) as tmp:
        database = Path(tmp) / "database.sqlite3"
        checksum = hashlib.sha256()
        with gzip.open(archive, "rb") as src, database.open("wb") as dst:
            while block := src.read(1024 * 1024):
                checksum.update(block)
                dst.write(block)
        if checksum.hexdigest() != expected:
            raise ValueError("Backup checksum mismatch")
        with closing(sqlite3.connect(database)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Restored database failed integrity check")
        os.link(database, target)


def main():
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("create")
    s.add_argument("--database", type=Path, required=True)
    s.add_argument("--directory", type=Path, required=True)
    s.add_argument("--keep", type=int, default=7)
    s = sub.add_parser("restore")
    s.add_argument("--archive", type=Path, required=True)
    s.add_argument("--database", type=Path, required=True)
    args = p.parse_args()
    if args.command == "create":
        print(backup_database(args.database, args.directory, args.keep))
    else:
        restore_backup(args.archive, args.database)
        print(args.database)


if __name__ == "__main__":
    main()
