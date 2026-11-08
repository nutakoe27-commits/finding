#!/usr/bin/env python3
"""Configure only the scoped collector key, preserving existing app settings."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import re
import secrets
import tempfile

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
NAME = "BOOKMAKER_INGEST_TOKEN"
BINDING = re.compile(r"^\s*(?:export\s+)?BOOKMAKER_INGEST_TOKEN\s*=", re.M)


def atomic_private(path: Path, text: str):
    previous = path.stat() if path.exists() else None
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as output:
        temporary = Path(output.name)
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    try:
        if previous and os.geteuid() == 0:
            os.chown(temporary, previous.st_uid, previous.st_gid)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def configure(env_file: Path, token_file: Path):
    if env_file.is_symlink() or token_file.is_symlink() or env_file == token_file:
        raise ValueError("Пути настроек должны быть разными обычными файлами.")
    original = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    if len(BINDING.findall(original)) > 1:
        raise ValueError("В .env несколько определений ключа сборщика. Уберите дубликаты.")
    key = dotenv_values(stream=io.StringIO(original), interpolate=False).get(NAME)
    if key and not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key):
        raise ValueError("Существующий ключ сборщика имеет неожиданный формат; файл не изменён.")
    key = key or secrets.token_urlsafe(32)
    if token_file.exists():
        previous = dotenv_values(token_file, interpolate=False)
        if set(previous) != {NAME} or previous[NAME] != key:
            raise ValueError("Файл ключа уже занят другой конфигурацией; файлы не изменены.")
    lines = original.splitlines(keepends=True)
    binding = NAME + "=" + key + "\n"
    updated = "".join(binding if BINDING.match(line) else line for line in lines)
    if not BINDING.search(original):
        updated += ("\n" if updated and not updated.endswith("\n") else "") + binding
    if original != updated and env_file.exists():
        backup = env_file.parent / ".local" / "receiver-backups"
        backup.mkdir(parents=True, mode=0o700, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        atomic_private(backup / ("env-" + stamp), original)
    atomic_private(env_file, updated)
    atomic_private(token_file, binding)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--token-file", type=Path, default=Path("/etc/matchlab-scraper-token.env"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, "Запустите от root: файл ключа должен принадлежать root.\n")
    try:
        configure(args.env_file, args.token_file)
    except (OSError, ValueError):
        parser.exit(1, "Не удалось настроить приёмник. Проверьте права, дубликаты BOOKMAKER_INGEST_TOKEN и назначение файла ключа. Значения ключей не выводятся.\n")
    print("Ключ сборщика настроен. Файлы закрыты правами 600; остальные настройки сохранены.")
    print("Перезапустите только matchlab.service, чтобы приложение прочитало ключ.")


if __name__ == "__main__":
    main()
