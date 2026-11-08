#!/usr/bin/env bash
set -euo pipefail
if [[ "$EUID" != 0 ]]; then
  echo 'Запустите через sudo на VPS.' >&2
  exit 1
fi
acme_dir=/var/lib/matchlab-acme
if [[ -L "$acme_dir" ]] || { [[ -e "$acme_dir" ]] && [[ ! -f "$acme_dir/.matchlab-managed" ]]; }; then
  echo 'Каталог ACME занят; существующие файлы не менялись.' >&2
  exit 1
fi
if [[ ! -e "$acme_dir" ]]; then
  install -d -m 755 "$acme_dir"
  touch "$acme_dir/.matchlab-managed"
fi
if [[ -L "$acme_dir/.well-known" || -L "$acme_dir/.well-known/acme-challenge" ]]; then
  echo 'Каталог ACME содержит ссылку; существующие файлы не менялись.' >&2
  exit 1
fi
install -d -m 755 "$acme_dir/.well-known" "$acme_dir/.well-known/acme-challenge"
echo 'Отдельный каталог ACME для Matchlab подготовлен.'
