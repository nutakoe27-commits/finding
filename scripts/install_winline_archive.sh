#!/usr/bin/env bash
# Install only the standalone Winline history service, leaving model/app services alone.
set -euo pipefail
cd "$(dirname "$0")/.."
dry_run=0
install_only=0
archive_python=python3
while (($#)); do
  case "$1" in
    --python) archive_python="${2:?Missing Python executable}"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    --install-only) install_only=1; shift ;;
    *) echo "Usage: $0 [--python /path/to/python3.11+] [--dry-run|--install-only]" >&2; exit 1 ;;
  esac
done
"$archive_python" - <<'PY'
import shutil
import sys
if sys.version_info < (3, 11):
    sys.exit('Нужен Python 3.11+ с модулем venv.')
if not sys.platform.startswith('linux') or not shutil.which('systemctl'):
    sys.exit('Нужен Linux с systemd.')
from pathlib import Path
mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line)
if int(mem.get('MemAvailable', '0 kB').split()[0]) < 3 * 1024**2:
    sys.exit('Нужно не менее 3 ГиБ свободной RAM для отдельного браузера.')
if shutil.disk_usage('/var/lib').free < 5 * 1024**3:
    sys.exit('Для базы, браузера и резервных копий нужно минимум 5 ГиБ свободного места.')
print('План: /opt/winline-archive; база и backup в /var/lib/winline-archive; отдельная служба winline-archive.')
print('Существующие службы модели, сайта, сборщиков и туннелей не меняются.')
PY
target=/opt/winline-archive
state=/var/lib/winline-archive
unit=/etc/systemd/system/winline-archive.service
marker='# Managed by standalone Winline archive'
for path in "$target" "$state" "$unit"; do
  if [[ -L "$path" ]]; then
    echo "Путь занят ссылкой: $path" >&2; exit 1
  fi
done
if [[ -e "$target" && ! -f "$target/.winline-archive-managed" ]]; then
  echo "$target уже занят; установка остановлена." >&2; exit 1
fi
if [[ -e "$state" && ! -f "$state/.winline-archive-managed" ]]; then
  echo "$state уже занят; установка остановлена." >&2; exit 1
fi
if [[ -e "$unit" ]] && [[ $(head -n 1 "$unit") != "$marker" ]]; then
  echo "$unit принадлежит другой установке." >&2; exit 1
fi
if [[ "$dry_run" == 1 ]]; then
  echo 'Предварительная проверка завершена; изменений нет.'
  exit 0
fi
if [[ "$EUID" != 0 ]]; then
  echo 'Запустите установщик через sudo на сервере нейросети.' >&2; exit 1
fi
if ! systemctl show --property=Version --value >/dev/null; then
  echo 'Нет доступного системного systemd; установка остановлена до изменений.' >&2; exit 1
fi
if id winline-archive >/dev/null 2>&1; then
  if [[ ! -f "$target/.winline-archive-managed" || ! -f "$state/.winline-archive-managed" ]]; then
    echo 'Пользователь winline-archive уже занят другой установкой.' >&2; exit 1
  fi
else
  useradd --system --user-group --home-dir "$state" --shell /usr/sbin/nologin winline-archive
fi
# Stop only our managed service before replacing its own code.
if [[ -f "$unit" ]]; then
  systemctl stop winline-archive.service
fi
install -d -m 755 "$target" "$target/winline"
install -d -m 700 -o winline-archive -g winline-archive "$state"
touch "$target/.winline-archive-managed" "$state/.winline-archive-managed"
chown winline-archive:winline-archive "$state/.winline-archive-managed"
for name in __init__ browser collect supplemental storage query audit backup worker team_context session; do
  install -m 644 "winline/$name.py" "$target/winline/"
done
install -m 644 winline/requirements.txt "$target/requirements.txt"
echo '[1/4] Отдельное Python-окружение сборщика...'
"$archive_python" -m venv "$target/.venv"
"$target/.venv/bin/python" -m pip install --disable-pip-version-check --progress-bar off -r "$target/requirements.txt"
echo '[2/4] Отдельный браузер Chromium...'
runuser -u winline-archive -- env PLAYWRIGHT_BROWSERS_PATH="$state/browsers" \
  "$target/.venv/bin/python" -m playwright install chromium
install -m 644 deploy/winline-archive.service "$unit"
systemctl daemon-reload
if [[ "$install_only" == 1 ]]; then
  echo 'Файлы установлены. Проверка источника и автозапуск не включены.'
  exit 0
fi
echo '[3/4] Реальная проверка Winline от имени пользователя службы...'
# Run the check with the same resource limits and isolation as the persistent service.
# It uses probe.sqlite3, preserving any existing working database.
probe_unit="winline-archive-preflight-$$"
if ! systemd-run --unit="$probe_unit" --wait --pipe --collect \
  -p User=winline-archive -p Group=winline-archive -p WorkingDirectory="$target" \
  -p MemoryHigh=1536M -p MemoryMax=2048M -p CPUQuota=50% -p Nice=15 \
  -p PrivateDevices=true -p OOMScoreAdjust=500 -p TasksMax=512 -p IOSchedulingClass=idle \
  -p NoNewPrivileges=true -p RuntimeMaxSec=600 -p KillMode=control-group \
  -p PrivateTmp=true -p ProtectHome=true -p ProtectSystem=strict -p "ReadWritePaths=$state" \
  -p "Environment=PLAYWRIGHT_BROWSERS_PATH=$state/browsers" \
  "$target/.venv/bin/python" -m winline.worker --state-dir "$state" --check; then
  echo 'Проверка не пройдена; новая служба не запущена. Причина — выше, статус — в status.json.' >&2
  echo "Если Chromium сообщает об отсутствующих системных библиотеках, выполните: sudo $target/.venv/bin/python -m playwright install-deps chromium" >&2
  exit 2
fi
echo '[4/4] Включение независимой службы...'
systemctl enable --now winline-archive.service
systemctl is-active --quiet winline-archive.service
echo "Служба запущена. Первый полный сбор может занять продолжительное время. Статус: sudo cat $state/status.json"
echo 'Журнал: sudo journalctl -u winline-archive -n 40 --no-pager'
