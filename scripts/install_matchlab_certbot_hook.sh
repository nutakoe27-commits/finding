#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$EUID" != 0 ]]; then
  echo 'Запустите через sudo на VPS.' >&2
  exit 1
fi
hook_dir=/etc/letsencrypt/renewal-hooks/deploy
hook_path="$hook_dir/matchlab-prognozich-online"
if [[ -L "$hook_dir" || ! -d /etc/letsencrypt ]]; then
  echo 'Не найден безопасный каталог Certbot; чужие файлы не менялись.' >&2
  exit 1
fi
if [[ -L "$hook_path" ]]; then
  echo 'Путь hook занят ссылкой; чужие файлы не менялись.' >&2
  exit 1
fi
if [[ -e "$hook_path" ]]; then
  if [[ -f "$hook_path" ]] && cmp -s deploy/matchlab-certbot-renewal.sh "$hook_path"; then
    chmod 755 "$hook_path"
    echo 'Hook уже установлен.'
    exit 0
  fi
  echo 'Путь hook занят другим файлом; он не изменялся.' >&2
  exit 1
fi
install -d -m 755 "$hook_dir"
install -m 755 deploy/matchlab-certbot-renewal.sh "$hook_path"
echo 'Hook для продления prognozich.online установлен.'
