#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

dry_run=0
install_only=0
meminfo=/proc/meminfo
api_url=http://127.0.0.1:8080
sources=winline
while (($#)); do
  case "$1" in
    --dry-run) dry_run=1; shift ;;
    --install-only) install_only=1; shift ;;
    --meminfo) meminfo="${2:?Missing file}"; shift 2 ;;
    --api-url) api_url="${2:?Missing URL}"; shift 2 ;;
    --sources) sources="${2:?Missing sources}"; shift 2 ;;
    *) echo "Использование: $0 [--api-url http://127.0.0.1:PORT] [--sources winline|betboom,winline] [--dry-run|--install-only]" >&2; exit 1 ;;
  esac
done
if [[ "$meminfo" != /proc/meminfo && "$dry_run" != 1 ]]; then
  echo '--meminfo разрешён только с --dry-run.' >&2
  exit 1
fi

# Check before creating users, installing anything or touching services.
python3 - "$meminfo" "$api_url" "$sources" <<'PY'
from pathlib import Path
import sys
from urllib.parse import urlsplit
mem = dict(line.split(':', 1) for line in Path(sys.argv[1]).read_text().splitlines() if ':' in line)
available = int(mem.get('MemAvailable', '0 kB').split()[0]) // 1024
if available < 1536:
    sys.exit(f'Остановка до установки: доступно {available} МиБ RAM, нужно не менее 1536 МиБ. Swap не заменяет RAM. Используйте другой сервер или увеличьте память VPS.')
url = urlsplit(sys.argv[2])
if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
    sys.exit('API должен быть локальным HTTP-адресом. Для другого сервера используйте SSH-туннель.')
if sys.version_info < (3, 10):
    sys.exit('Нужен Python 3.10+.')
from matchlab.worker_protocol import parse_sources
parse_sources(sys.argv[3])
print(f'Проверка памяти: доступно {available} МиБ; требование выполнено.')
PY
if [[ "$dry_run" == 1 ]]; then
  echo 'Проверка завершена. Ничего не установлено и не запущено.'
  exit 0
fi
if [[ "$EUID" != 0 ]]; then
  echo 'Установка требует root для отдельного пользователя и systemd.' >&2
  exit 1
fi

target=/opt/matchlab-scraper
token_file=/etc/matchlab-scraper-token.env
if [[ ! -f "$token_file" || -L "$token_file" || $(stat -c %u "$token_file") != 0 || $(stat -c %a "$token_file") != 600 ]]; then
  echo 'Сначала подготовьте root-owned /etc/matchlab-scraper-token.env с правами 600 (инструкция в docs/server-scraper.md).' >&2
  exit 1
fi
if [[ -L "$target" ]] || { [[ -e "$target" ]] && [[ ! -f "$target/.matchlab-managed" ]]; }; then
  echo '/opt/matchlab-scraper уже занят. Установка остановлена.' >&2
  exit 1
fi
for unit in matchlab-scraper.service matchlab-scraper-check.service; do
  path="/etc/systemd/system/$unit"
  if [[ -e "$path" ]]; then
    IFS= read -r first_line < "$path" || true
    if [[ -L "$path" || "$first_line" != '# Managed by Matchlab server scraper' ]]; then
      echo "Unit $unit уже занят другой конфигурацией." >&2
      exit 1
    fi
  fi
done
if [[ -e /etc/matchlab-scraper.conf ]]; then
  IFS= read -r first_line < /etc/matchlab-scraper.conf || true
  if [[ -L /etc/matchlab-scraper.conf || "$first_line" != '# Managed by Matchlab server scraper' ]]; then
    echo '/etc/matchlab-scraper.conf уже занят другой конфигурацией.' >&2
    exit 1
  fi
fi
if systemctl is-active --quiet matchlab-scraper.service; then
  echo 'Сначала остановите только matchlab-scraper.service перед обновлением его файлов.' >&2
  exit 1
fi
if ! id matchlab-scraper >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/matchlab-scraper --shell /usr/sbin/nologin matchlab-scraper
fi
install -d -m 755 "$target" "$target/matchlab"
install -d -m 700 -o matchlab-scraper -g matchlab-scraper /var/lib/matchlab-scraper
touch "$target/.matchlab-managed"
install -m 644 matchlab/{__init__,sports,bookmakers,capture,worker_protocol,server_scraper,winline_browser}.py "$target/matchlab/"
install -m 644 scripts/diagnose_winline.py "$target/diagnose_winline.py"
install -m 644 requirements-capture.txt requirements-server-scraper.txt "$target/"
python3 -m venv "$target/.venv"
"$target/.venv/bin/python" -m pip install --quiet -r "$target/requirements-server-scraper.txt"
runuser -u matchlab-scraper -- env PLAYWRIGHT_BROWSERS_PATH=/var/lib/matchlab-scraper/browsers "$target/.venv/bin/python" -m playwright install chromium

printf '# Managed by Matchlab server scraper\nBOOKMAKER_API_URL=%s\nBOOKMAKER_SOURCES=%s\n' "$api_url" "$sources" > /etc/matchlab-scraper.conf
chmod 600 /etc/matchlab-scraper.conf
install -m 644 deploy/matchlab-scraper{,-check}.service /etc/systemd/system/
systemctl daemon-reload
if [[ "$install_only" == 1 ]]; then
  echo 'Код и диагностика установлены. Проверка и автосбор не запускались.'
  exit 0
fi
check_since="$(date --iso-8601=seconds)"
if ! systemctl start matchlab-scraper-check.service; then
  journalctl -u matchlab-scraper-check.service --since "$check_since" -n 25 --no-pager || true
  echo 'Проверка не пройдена: автосбор не включён. Журнал проверки выведен выше. Для снимка предматчевой страницы используйте /opt/matchlab-scraper/diagnose_winline.py --prematch (docs/server-scraper.md).' >&2
  exit 2
fi
systemctl enable --now matchlab-scraper.service
echo "Выбранные источники ($sources) прошли проверку; автосбор включён, интервал 60 секунд. Проверяйте свежесть и состояние в интерфейсе."
