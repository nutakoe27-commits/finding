#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
api_url=http://127.0.0.1:18081
if [[ ${1:-} == --api-url ]]; then api_url="${2:?Missing local tunnel URL}"; shift 2; fi
if (($#)); then echo "Usage: $0 [--api-url http://127.0.0.1:18081]" >&2; exit 1; fi
if [[ $EUID != 0 ]]; then echo 'Запустите через sudo на сервере нейросети.' >&2; exit 1; fi
target=/opt/winline-archive
unit=/etc/systemd/system/winline-archive-sync.service
conf=/etc/winline-archive-sync.conf
token=/etc/matchlab-scraper-token.env
if [[ ! -f "$target/.winline-archive-managed" || ! -x "$target/.venv/bin/python" ]]; then
  echo 'Сначала обновите scripts/install_winline_archive.sh.' >&2; exit 1
fi
for path in "$unit" "$conf"; do
  if [[ -L "$path" ]] || { [[ -e "$path" ]] && [[ $(head -n 1 "$path") != '# Managed by Winline archive sync' ]]; }; then
    echo "Путь занят другой конфигурацией: $path" >&2; exit 1
  fi
done
if [[ ! -f "$token" || -L "$token" || $(stat -c %u "$token") != 0 || $(stat -c %a "$token") != 600 ]]; then
  echo 'Нужен существующий ключ сборщика /etc/matchlab-scraper-token.env (root,600). Смотрите docs/server-scraper.md.' >&2; exit 1
fi
# Validate the endpoint before replacing any running publisher. Never print the key.
"$target/.venv/bin/python" - "$api_url" "$token" <<'PY'
from pathlib import Path
from urllib.parse import urlsplit
import sys,re,httpx
url=urlsplit(sys.argv[1])
if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost','::1') or url.username or url.password or url.query or url.fragment or url.path not in ('','/'):
    sys.exit('Нужен локальный URL существующего туннеля.')
text=Path(sys.argv[2]).read_text().strip()
if not re.fullmatch(r'BOOKMAKER_INGEST_TOKEN=[A-Za-z0-9_-]{32,128}',text):sys.exit('Неожиданный формат ключа, значение не выводится.')
with httpx.Client(base_url=sys.argv[1],trust_env=False,timeout=15) as client:
    response=client.get('/api/bookmakers/archive/watch',headers={'X-Matchlab-Collector-Key':text.split('=',1)[1]})
    if response.status_code!=200:sys.exit(f'VPS/туннель ещё не готовы (HTTP {response.status_code}). Сначала обновите VPS.')
    if response.json().get('format')!='matchlab.archive.v1':sys.exit('Версия API архива не совпадает.')
PY
if [[ -f "$unit" ]]; then systemctl stop winline-archive-sync.service; fi
for name in sync projection; do install -m 644 "winline/$name.py" "$target/winline/"; done
printf '# Managed by Winline archive sync\nWINLINE_SYNC_API_URL=%s\n' "$api_url" > "$conf"
chmod 600 "$conf"
install -m 644 deploy/winline-archive-sync.service "$unit"
systemctl daemon-reload
systemctl enable --now winline-archive-sync.service
systemctl is-active --quiet winline-archive-sync.service
echo 'Передача включена. Проверка доставки: sudo cat /var/lib/winline-archive/sync-status.json'
echo 'На сайте: раздел «Матчи» для будущих и Live, «Архив» — для результатов.'
