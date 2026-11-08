#!/usr/bin/env bash
# Forward normalized observations to the VPS without exposing an ingestion port.
set -euo pipefail
umask 077
ssh_target=""
ssh_user="${SUDO_USER:-root}"
ssh_identity=""
while (($#)); do
  case "$1" in
    --ssh-target) ssh_target="${2:?Missing SSH target}"; shift 2 ;;
    --local-user) ssh_user="${2:?Missing local user}"; shift 2 ;;
    --identity-file) ssh_identity="${2:?Missing identity file}"; shift 2 ;;
    *) echo "Использование: sudo bash $0 --ssh-target root@VPS_IP [--local-user user] [--identity-file /home/user/.ssh/matchlab_scraper]" >&2; exit 1 ;;
  esac
done
if [[ "$EUID" != 0 || ! "$ssh_target" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*$ || ! "$ssh_user" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
  echo 'Нужны sudo, корректный SSH alias / user@host и локальный пользователь SSH.' >&2
  exit 1
fi
id "$ssh_user" >/dev/null
identity_options=""
if [[ -n "$ssh_identity" ]]; then
  if [[ ! "$ssh_identity" =~ ^/[A-Za-z0-9_./@+-]+$ ]] || ! runuser -u "$ssh_user" -- test -f "$ssh_identity" || ! runuser -u "$ssh_user" -- test -r "$ssh_identity"; then
    echo 'Укажите абсолютный путь к доступному SSH-ключу без пробелов и специальных символов.' >&2
    exit 1
  fi
  identity_options="-i $ssh_identity -o IdentitiesOnly=yes"
fi
unit=/etc/systemd/system/matchlab-scraper-tunnel.service
if [[ -e "$unit" ]]; then
  IFS= read -r first_line < "$unit" || true
  if [[ -L "$unit" || "$first_line" != '# Managed by Matchlab server scraper' ]]; then
    echo 'Unit туннеля уже занят другой конфигурацией.' >&2
    exit 1
  fi
fi
if systemctl is-active --quiet matchlab-scraper-tunnel.service; then
  echo 'Туннель сборщика уже запущен. Для изменения остановите только matchlab-scraper-tunnel.service.' >&2
  exit 1
fi
python3 - <<'PY'
import socket
with socket.socket() as probe:
    try:
        probe.bind(('127.0.0.1', 18081))
    except OSError:
        raise SystemExit('Порт 127.0.0.1:18081 на сервере сбора занят; существующий процесс не изменён.')
PY
ssh_options=(-o BatchMode=yes -o IdentityAgent=none -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none)
if [[ -n "$ssh_identity" ]]; then
  ssh_options+=(-i "$ssh_identity" -o IdentitiesOnly=yes)
fi
if ! runuser -u "$ssh_user" -- ssh "${ssh_options[@]}" "$ssh_target" true; then
  echo "Нет автономного SSH-доступа от $ssh_user. Настройте SSH-ключ и проверьте host key обычным ssh перед повтором." >&2
  exit 1
fi
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
chown "$ssh_user" "$temporary"
runuser -u "$ssh_user" -- scp -B "${ssh_options[@]}" "$ssh_target:/etc/matchlab-scraper-token.env" "$temporary/token.env"
python3 - "$temporary/token.env" <<'PY'
from pathlib import Path
import re, sys
if not re.fullmatch(r'BOOKMAKER_INGEST_TOKEN=[A-Za-z0-9_-]{32,128}\n', Path(sys.argv[1]).read_text()):
    sys.exit('Неожиданный формат файла ключа; установка остановлена без вывода содержимого.')
PY
token_file=/etc/matchlab-scraper-token.env
if [[ -L "$token_file" ]] || { [[ -e "$token_file" ]] && ! cmp -s "$token_file" "$temporary/token.env"; }; then
  echo 'Локальный файл ключа уже занят другой конфигурацией; он не изменён.' >&2
  exit 1
fi
install -m 600 -o root -g root "$temporary/token.env" "$token_file"
cat > "$unit" <<EOF
# Managed by Matchlab server scraper
[Unit]
Description=Matchlab collector SSH forward to VPS
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$ssh_user
ExecStart=/usr/bin/ssh -NT $identity_options -o BatchMode=yes -o IdentityAgent=none -o StrictHostKeyChecking=yes -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=10 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:18081:127.0.0.1:8080 $ssh_target
Restart=always
RestartSec=15
MemoryMax=64M
CPUQuota=10%
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$unit"
systemctl daemon-reload
systemctl start matchlab-scraper-tunnel.service
if ! python3 - <<'PY'
import json, time, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for _ in range(20):
    try:
        with opener.open('http://127.0.0.1:18081/api/bookmakers', timeout=2) as response:
            if isinstance(json.load(response).get('quotes'), list):
                break
    except (OSError, ValueError):
        pass
    time.sleep(0.5)
else:
    raise SystemExit('Нет ответа Matchlab через туннель; автозапуск туннеля не включён.')
PY
then
  systemctl stop matchlab-scraper-tunnel.service
  exit 2
fi
systemctl enable matchlab-scraper-tunnel.service
echo 'Отдельный туннель готов: 127.0.0.1:18081 → VPS 127.0.0.1:8080. Ключ передан по SSH без вывода значения.'
