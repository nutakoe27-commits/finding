#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
install_only=0
dry_run=0
api_url=http://127.0.0.1:18081
concurrency=4
sports=all
while (($#)); do
  case "$1" in
    --install-only) install_only=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    --api-url) api_url="${2:?Missing URL}"; shift 2 ;;
    --concurrency) concurrency="${2:?Missing concurrency}"; shift 2 ;;
    --sports) sports="${2:?Missing sports}"; shift 2 ;;
    *) echo "Использование: $0 [--api-url http://127.0.0.1:PORT] [--sports all|football,hockey,tennis] [--concurrency 1..8] [--install-only|--dry-run]" >&2; exit 1 ;;
  esac
done
python3 - "$api_url" "$concurrency" "$sports" <<'PY'
from pathlib import Path
import sys
from urllib.parse import urlsplit
from matchlab.sports import ordinary_sport
if sys.version_info < (3, 11):
    sys.exit('Для Scrapy нужен Python 3.11+.')
mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line)
available = int(mem.get('MemAvailable', '0 kB').split()[0]) // 1024
if available < 6144:
    sys.exit(f'Остановка до установки: доступно {available} МиБ RAM; для полного браузерного обхода нужно не менее 6144 МиБ. Устанавливайте на сервер Qwen.')
url = urlsplit(sys.argv[1])
if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
    sys.exit('Нужен локальный HTTP API; для VPS используйте существующий SSH-туннель.')
if sys.argv[2] not in tuple(str(value) for value in range(1, 9)):
    sys.exit('Допустима параллельность от 1 до 8 страниц.')
if sys.argv[3] != 'all' and any(not ordinary_sport(key) for key in sys.argv[3].split(',')):
    sys.exit('Укажите all или обычные виды спорта через запятую: football,hockey,tennis,basketball.')
print(f'Память: доступно {available} МиБ; проверка пройдена.')
PY
if [[ "$dry_run" == 1 ]]; then
  echo 'Проверка завершена без изменений.'
  exit 0
fi
if [[ "$EUID" != 0 ]]; then
  echo 'Запустите установщик через sudo на сервере Qwen.' >&2
  exit 1
fi
target=/opt/matchlab-crawler
token=/etc/matchlab-scraper-token.env
if [[ ! -f "$token" || -L "$token" || $(stat -c %u "$token") != 0 || $(stat -c %a "$token") != 600 ]]; then
  echo 'Сначала настройте приёмник и отдельный SSH-туннель; нужен root-owned файл ключа с правами 600.' >&2
  exit 1
fi
if [[ -L "$target" ]] || { [[ -e "$target" ]] && [[ ! -f "$target/.matchlab-managed" ]]; }; then
  echo 'Каталог /opt/matchlab-crawler уже занят.' >&2
  exit 1
fi
for path in /etc/systemd/system/matchlab-crawler.service /etc/systemd/system/matchlab-crawler-check.service /etc/matchlab-crawler.conf; do
  if [[ -L "$path" ]]; then
    echo "Путь $path занят ссылкой; установка остановлена." >&2
    exit 1
  fi
  if [[ -e "$path" ]]; then
    IFS= read -r first < "$path" || true
    if [[ "$first" != '# Managed by Matchlab market crawler' ]]; then
      echo "Путь $path занят другой конфигурацией." >&2
      exit 1
    fi
  fi
done
if systemctl is-active --quiet matchlab-crawler.service; then
  echo 'Перед обновлением остановите только matchlab-crawler.service.' >&2
  exit 1
fi
if ! id matchlab-scraper >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/matchlab-scraper --shell /usr/sbin/nologin matchlab-scraper
fi
echo '[1/6] Подготовка кода и отдельного Python-окружения...'
install -d -m 755 "$target" "$target/matchlab"
install -d -m 700 -o matchlab-scraper -g matchlab-scraper /var/lib/matchlab-crawler /var/lib/matchlab-scraper
touch "$target/.matchlab-managed"
install -m 644 matchlab/{__init__,sports,bookmakers,capture,worker_protocol,server_scraper,winline_browser,winline_markets,markets,scrapy_worker}.py "$target/matchlab/"
install -m 644 requirements-capture.txt requirements-server-scraper.txt requirements-crawler.txt "$target/"
install -m 644 scripts/diagnose_winline_stats.py "$target/diagnose_winline_stats.py"
install -m 644 scripts/diagnose_winline_header.py "$target/diagnose_winline_header.py"
python3 -m venv "$target/.venv"
echo '[2/6] Установка Python-пакетов. Первая загрузка может занять несколько минут; прогресс pip показан ниже.'
"$target/.venv/bin/python" -m pip install --disable-pip-version-check --progress-bar off -r "$target/requirements-crawler.txt"
echo '[3/6] Проверка Chromium; при отсутствии браузера начнётся загрузка...'
runuser -u matchlab-scraper -- env PLAYWRIGHT_BROWSERS_PATH=/var/lib/matchlab-scraper/browsers "$target/.venv/bin/python" -m playwright install chromium
echo '[4/6] Установка конфигурации служб Matchlab...'
printf '# Managed by Matchlab market crawler\nBOOKMAKER_API_URL=%s\nBOOKMAKER_CRAWL_CONCURRENCY=%s\nBOOKMAKER_SPORTS=%s\n' "$api_url" "$concurrency" "$sports" > /etc/matchlab-crawler.conf
chmod 600 /etc/matchlab-crawler.conf
install -m 644 deploy/matchlab-crawler{,-check}.service /etc/systemd/system/
systemctl daemon-reload
if [[ "$install_only" == 1 ]]; then
  echo 'Scrapy и код установлены. Проверка и сбор не запускались.'
  exit 0
fi
check_since="$(date --iso-8601=seconds)"
echo '[5/6] Проверка страницы Winline и доставки данных. Ожидание — до четырёх минут.'
echo 'Журнал проверки во втором SSH-сеансе: sudo journalctl -fu matchlab-crawler-check.service'
if ! systemctl start matchlab-crawler-check.service; then
  journalctl -u matchlab-crawler-check.service --since "$check_since" -n 40 --no-pager || true
  echo 'Не подтверждены полный снимок матча и его доставка. Автосбор не включён.' >&2
  exit 2
fi
old=/etc/systemd/system/matchlab-scraper.service
if [[ -f "$old" ]]; then
  IFS= read -r first < "$old" || true
  if [[ "$first" != '# Managed by Matchlab server scraper' ]]; then
    echo 'Прежняя служба сборщика имеет другую конфигурацию; она не изменена.' >&2
    exit 1
  fi
  systemctl disable --now matchlab-scraper.service
fi
echo '[6/6] Включение автосбора...'
systemctl enable --now matchlab-crawler.service
echo "Проверена доставка одного полного снимка. Включён обход видов спорта: $sports. Интервал циклов — 60 секунд без наложения; полный обход может занимать дольше. Смотрите фактические события и свежесть в интерфейсе."
