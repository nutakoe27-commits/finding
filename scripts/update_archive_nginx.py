#!/usr/bin/env python3
"""Update the managed site's archive routes while preserving existing TLS settings."""

import argparse
import os
import re
from datetime import UTC, datetime
from pathlib import Path

try:
    from .configure_domain import MARKER, apply_site, check_paths, domain_name
except ImportError:
    from configure_domain import MARKER, apply_site, check_paths, domain_name


def updated(text, domain):
    if text.splitlines()[:1] != [MARKER]:
        raise ValueError("Это не конфигурация сайта, созданная Matchlab.")
    if re.findall(r"\bserver_name\s+([^;]+);", text) != [domain]:
        raise ValueError("Неожиданная конфигурация виртуального хоста.")
    for old, new in (
        (
            "refresh/(claim|finish))/?$",
            "refresh/(claim|finish)|archive/(ingest|watch))/?$",
        ),
        (
            "bookmakers/events/[0-9]+/explain)/?$",
            "bookmakers/events/[0-9]+/explain|archive/events/[0-9]+/explain)/?$",
        ),
    ):
        if text.count(new) == 1:
            continue
        if text.count(old) != 1:
            raise ValueError("Не удалось однозначно найти правила API. Изменений нет.")
        text = text.replace(old, new, 1)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="prognozich.online")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    domain = domain_name(args.domain)
    available = Path(f"/etc/nginx/sites-available/matchlab-{domain}.conf")
    enabled = Path(f"/etc/nginx/sites-enabled/{available.name}")
    check_paths(available, enabled)
    if not available.is_file() or not enabled.is_symlink():
        parser.error("Сначала нужна существующая включённая конфигурация сайта.")
    before = available.read_text()
    after = updated(before, domain)
    if args.check or before == after:
        print(
            "Правила проверены. "
            + ("Изменения готовы." if before != after else "Уже обновлено.")
        )
        return
    if os.geteuid() != 0:
        parser.error("Запустите через sudo на VPS.")
    backup = available.with_name(
        available.name
        + ".archive-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        + ".bak"
    )
    backup.write_text(before)
    backup.chmod(0o600)
    apply_site(available, enabled, after)
    print(f"Правила архива обновлены; Nginx проверен и перечитан. Копия: {backup}")


if __name__ == "__main__":
    main()
