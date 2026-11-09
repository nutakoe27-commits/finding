#!/usr/bin/env python3
"""Install only the Matchlab push queue worker on the existing application VPS."""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from matchlab.config import Settings
from matchlab.push import configured

MARKER = "# Managed by Matchlab push queue"


def unit(root, state, user, group):
    for path in (root, state):
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)):
            raise ValueError("Нужны абсолютные пути без пробелов.")
    for value in (user, group):
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", value):
            raise ValueError("Некорректный пользователь службы.")
    return f"""{MARKER}
[Unit]
Description=Matchlab opt-in Web Push delivery queue
After=network-online.target matchlab.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={root}
EnvironmentFile=-{root}/.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={root}/.venv/bin/python -m matchlab.push
Restart=on-failure
RestartSec=15
TimeoutStopSec=30
MemoryMax=256M
CPUQuota=20%
Nice=10
TasksMax=32
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
ReadWritePaths={state}
UMask=0077

[Install]
WantedBy=multi-user.target
"""


def command(*args):
    return subprocess.run(
        args, check=True, capture_output=True, text=True
    ).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    if not configured(settings):
        parser.error("Сначала выполните .venv/bin/python -m matchlab.manage init-push.")
    if not (ROOT / ".venv/bin/python").is_file():
        parser.error("Не найдено окружение приложения.")
    user = (
        command("systemctl", "show", "matchlab.service", "-p", "User", "--value")
        or "root"
    )
    group = command(
        "systemctl", "show", "matchlab.service", "-p", "Group", "--value"
    ) or command("id", "-gn", user)
    text = unit(ROOT, settings.data_dir.resolve(), user, group)
    target = Path("/etc/systemd/system/matchlab-push.service")
    if target.is_symlink() or (
        target.exists() and target.read_text().splitlines()[:1] != [MARKER]
    ):
        parser.error("Путь службы занят другой конфигурацией.")
    if args.check:
        print(
            f"План: отдельная matchlab-push.service, пользователь {user}, база {settings.database}."
        )
        return
    if os.geteuid() != 0:
        parser.error("Запустите через sudo на VPS.")
    target.write_text(text)
    target.chmod(0o644)
    command("systemctl", "daemon-reload")
    command("systemctl", "enable", "--now", "matchlab-push.service")
    command("systemctl", "restart", "matchlab-push.service")
    command("systemctl", "is-active", "--quiet", "matchlab-push.service")
    print(
        "Очередь push запущена. Проверка: sudo journalctl -u matchlab-push -n 20 --no-pager"
    )


if __name__ == "__main__":
    main()
