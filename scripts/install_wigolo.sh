#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
if [[ "$EUID" != 0 || "$(uname -m)" != x86_64 ]]; then
  echo 'Для этого установщика нужны sudo и Linux x86_64. Другие архитектуры устанавливаются отдельно.' >&2
  exit 1
fi
target=/opt/matchlab-wigolo
data=/var/lib/matchlab-wigolo
unit=/etc/systemd/system/matchlab-wigolo.service
if [[ -L "$target" ]] || { [[ -e "$target" ]] && [[ ! -f "$target/.matchlab-managed" ]]; }; then
  echo 'Каталог /opt/matchlab-wigolo занят; установка остановлена.' >&2; exit 1
fi
if [[ -L "$data" ]] || { [[ -e "$data" ]] && [[ ! -f "$data/.matchlab-managed" ]]; }; then
  echo 'Каталог /var/lib/matchlab-wigolo занят; установка остановлена.' >&2; exit 1
fi
if [[ -L "$unit" ]] || { [[ -e "$unit" ]] && [[ "$(head -n 1 "$unit")" != '# Managed by Matchlab Wigolo integration' ]]; }; then
  echo 'Unit уже занят другой службой; установка остановлена.' >&2; exit 1
fi
if [[ $(df -Pk /var/lib | awk 'NR==2 {print $4}') -lt 2097152 ]]; then
  echo 'На разделе /var/lib нужно не менее 2 ГиБ свободного места.' >&2; exit 1
fi
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
curl -fLsS --connect-timeout 20 --max-time 600 \
  'https://github.com/KnockOutEZ/wigolo/releases/download/v0.3.1/wigolo-linux-x64' -o "$tmpdir/wigolo"
echo 'ccefed2b575dc186b2123ce74d8b9550de22f81aead2e1c73007f5c517103af6  '"$tmpdir/wigolo" | sha256sum -c --status
if ! id matchlab-wigolo >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$data" --shell /usr/sbin/nologin matchlab-wigolo
fi
install -d -m 755 "$target"
touch "$target/.matchlab-managed"
install -d -m 700 -o matchlab-wigolo -g matchlab-wigolo "$data"
touch "$data/.matchlab-managed"
if [[ -L "$data/api-token" ]]; then echo 'Файл токена является ссылкой; остановлено.' >&2; exit 1; fi
if [[ ! -f "$data/api-token" ]]; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' > "$data/api-token"
fi
if ! python3 - "$data/api-token" <<'PY'
import re,sys
from pathlib import Path
assert re.fullmatch(r'[0-9a-f]{64}\n?', Path(sys.argv[1]).read_text())
PY
then echo 'Некорректный существующий токен; остановлено.' >&2; exit 1; fi
chown matchlab-wigolo:matchlab-wigolo "$data/api-token"
chmod 600 "$data/api-token"
install -m 755 "$tmpdir/wigolo" "$target/wigolo.new"
mv -f "$target/wigolo.new" "$target/wigolo"
install -m 644 deploy/matchlab-wigolo.service "$unit"
systemctl daemon-reload
systemctl enable matchlab-wigolo.service
systemctl restart matchlab-wigolo.service
systemctl is-active --quiet matchlab-wigolo.service
echo 'Wigolo запущен только на 127.0.0.1:13334. Ключ сохранён локально и не выводится.'
echo 'Проверка: sudo systemctl status matchlab-wigolo --no-pager'
