#!/usr/bin/env python3
"""Capture validated pre-match 1X2 observations and optionally send over SSH."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matchlab.bookmakers import FORMAT, normalize_capture
from matchlab.capture import collect


def upload(document: dict, target: str, remote_dir: str):
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", target) or target.startswith("-"):
        raise ValueError("Некорректный SSH target. Используйте user@host или имя из ~/.ssh/config.")
    if not remote_dir.startswith("/"):
        raise ValueError("Нужен абсолютный путь к Matchlab на VPS.")
    command = "cd " + shlex.quote(remote_dir) + " && .venv/bin/python -m matchlab import-bookmakers --stdin"
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, command],
                            input=json.dumps(document, ensure_ascii=False), capture_output=True, text=True, timeout=60)
    if result.returncode:
        if result.returncode == 255:
            raise ValueError("Не удалось подключиться по SSH. Проверьте SSH-ключ, host key и адрес VPS.")
        raise ValueError("Команда импорта на VPS завершилась ошибкой. Проверьте версию Matchlab, путь и часы компьютеров. Локальная история сохранена.")
    try:
        reply = json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError("VPS вернул неожиданный ответ команды импорта. Обновите Matchlab на VPS.") from exc
    return {key: reply.get(key) for key in ("inserted", "parsed_snapshots", "events", "already_imported")}


def save_latest(document: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        json.dump(document, temp, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        temporary = Path(temp.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def run(args):
    seen_sources = set()
    successful = 0
    upload_enabled = bool(args.ssh_target)
    upload_failed = False

    async def batch(document):
        nonlocal successful, upload_enabled, upload_failed
        normalized = normalize_capture(document)
        rows = normalized["snapshots"]
        if not rows:
            print("В этом интервале нет подтверждённых предматчевых исходов 1X2. Ожидаем загрузку линии.", flush=True)
            return
        save_latest(normalized, args.output)
        with args.output.with_suffix(".history.jsonl").open("a", encoding="utf-8") as history:
            history.write(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")) + "\n")
        successful += 1
        seen_sources.update(row["source"] for row in rows)
        print(json.dumps(normalized["report"]["sources"], ensure_ascii=False), flush=True)
        if upload_enabled:
            try:
                reply = await asyncio.to_thread(upload, normalized, args.ssh_target, args.remote_dir)
                print("VPS:", json.dumps(reply, ensure_ascii=False), flush=True)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                print(str(exc) if isinstance(exc, ValueError) else "Передача по SSH не завершена; локальный снимок сохранён.", flush=True)
                upload_enabled, upload_failed = False, True
                print("Автоматическая передача отключена до перезапуска сборщика. Локальный сбор продолжается.", flush=True)

    # Both domains are needed: the mixed Winline homepage is corroborated
    # against explicit football fixtures from BetBoom, never guessed by price.
    await collect(["betboom", "winline"], args.seconds, executable=args.executable,
                  headless=args.headless, on_batch=batch, batch_seconds=args.interval,
                  entrypoints={"betboom": "https://betboom.ru/sport/football/128/216", "winline": "https://winline.ru/"})
    if not successful or seen_sources != {"betboom", "winline"} or upload_failed:
        print("Полнота обоих источников не подтверждена. Проверьте открытые вкладки; последний успешный файл сохранён.")
        return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--output", type=Path, default=Path(".local/bookmakers-latest.json"))
    parser.add_argument("--ssh-target", help="Optional user@VPS or SSH alias; requires key-based SSH")
    parser.add_argument("--remote-dir", default="/root/matchlab")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--executable")
    args = parser.parse_args()
    if args.interval < 10 or args.seconds < 1:
        parser.error("Интервал должен быть не меньше 10 секунд, длительность — положительная.")
    history = args.output.with_suffix(".history.jsonl")
    if args.output.exists() or args.output.is_symlink() or history.exists() or history.is_symlink():
        parser.error("Выберите новый путь --output: существующий файл не перезаписывается при старте.")
    if args.ssh_target:
        if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", args.ssh_target) or args.ssh_target.startswith("-"):
            parser.error("Некорректный SSH target.")
    try:
        raise SystemExit(asyncio.run(run(args)))
    except ImportError:
        parser.exit(1, "Установите requirements-capture.txt и Chromium через Playwright.\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Сбор остановлен. Ранее записанные снимки и история сохранены.\n")


if __name__ == "__main__":
    main()
