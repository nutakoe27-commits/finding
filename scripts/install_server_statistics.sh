#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$EUID" != 0 ]]; then
  echo 'Запустите через sudo на сервере браузерного сборщика.' >&2
  exit 1
fi
# Leave the small VPS and its other applications alone, before any mutation.
python3 - <<'PY'
from pathlib import Path
mem=dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line)
available=int(mem.get('MemAvailable','0 kB').split()[0])//1024
if available<2048:
    raise SystemExit(f'Доступно {available} МиБ RAM. Нужен сервер сборщика с запасом не менее 2048 МиБ; установка остановлена.')
print(f'Доступно {available} МиБ RAM; дополнительный браузер ограничен 1024 МиБ и двумя логическими ядрами CPU.')
PY
target=/opt/matchlab-statistics
unit=/etc/systemd/system/matchlab-statistics.service
token=/etc/matchlab-scraper-token.env
if [[ ! -f /opt/matchlab-crawler/.matchlab-managed || ! -x /opt/matchlab-crawler/.venv/bin/python ]]; then
  echo 'Сначала установите рабочий серверный сборщик scripts/install_market_crawler.sh.' >&2
  exit 1
fi
if [[ -L "$target" ]] || { [[ -e "$target" ]] && [[ ! -f "$target/.matchlab-managed" ]]; }; then
  echo 'Каталог статистики уже занят другим приложением; изменений нет.' >&2
  exit 1
fi
if [[ -L "$unit" ]] || { [[ -f "$unit" ]] && [[ $(head -n 1 "$unit") != '# Managed by Matchlab server statistics' ]]; }; then
  echo 'Unit статистики уже занят другой конфигурацией; изменений нет.' >&2
  exit 1
fi
if [[ ! -f "$token" || -L "$token" || $(stat -c %u "$token") != 0 || $(stat -c %a "$token") != 600 ]]; then
  echo 'Нужен существующий root-owned ключ сборщика с правами 600.' >&2
  exit 1
fi
id matchlab-scraper >/dev/null
/opt/matchlab-crawler/.venv/bin/python -c 'import playwright.async_api, httpx'
if [[ ! -x /usr/bin/xvfb-run ]] || ! command -v Xvfb >/dev/null || ! command -v xauth >/dev/null; then
  echo 'Нужны Xvfb и xauth. Установите системные пакеты xvfb и xauth, затем повторите установку. Работающая служба не изменена.' >&2
  exit 1
fi
# Only our separately named unit is restarted. No model or other crawler is stopped.
if [[ -f "$unit" ]]; then systemctl stop matchlab-statistics.service; fi
install -d -m 755 "$target" "$target/matchlab"
touch "$target/.matchlab-managed"
install -m 644 matchlab/{__init__,sports,capture,bookmakers,winline_browser,winline_markets,popular_markets,markets,worker_protocol,statistics,statistics_feed,statistics_worker}.py "$target/matchlab/"
install -m 644 deploy/matchlab-statistics.service "$unit"
systemctl daemon-reload
systemctl enable --now matchlab-statistics.service
echo 'Серверная статистика включена без Mac. Это запуск службы, а не подтверждение доступа к источнику.'
echo 'Подробная статистика запрашивается для выбранного матча по действию пользователя; массовый сбор отключён.'
echo 'Проверка: sudo journalctl -u matchlab-statistics -n 20 --no-pager'
echo 'Состояние: sudo cat /var/lib/matchlab-statistics/status.json'
echo 'При Access Denied сохраняется статус blocked и пауза перед повтором; рынки продолжают обновляться.'
