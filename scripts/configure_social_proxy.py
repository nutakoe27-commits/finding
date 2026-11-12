#!/usr/bin/env python3
"""Disable logging OAuth callback queries in this site's managed Nginx host only."""
import argparse
from datetime import datetime,timezone
import os
from pathlib import Path
import re
import shutil
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from configure_domain import MARKER,apply_site,domain_name

SOCIAL_MARKER='# Managed by Matchlab social callback privacy'


def updated(text):
    if text.splitlines()[:1]!=[MARKER]:raise ValueError('Ожидалась управляемая конфигурация Matchlab. Файл не изменён.')
    if SOCIAL_MARKER in text:return text
    matches=list(re.finditer(r'(?m)^(\s*)location / \{\s*\n([^{}]+?)\n\1\}',text))
    if len(matches)!=1:raise ValueError('Не удалось однозначно найти прокси приложения. Файл не изменён.')
    match=matches[0];indent=match[1];body=match[2]
    if not re.search(r'proxy_pass\s+http://127\.0\.0\.1:[0-9]+;',body):raise ValueError('Ожидался локальный прокси Matchlab. Файл не изменён.')
    block=(indent+SOCIAL_MARKER+'\n'+indent+'location ~ ^/api/auth/social/(yandex|vkid)/callback$ {\n'+
        indent+'    access_log off;\n'+body+'\n'+indent+'}\n')
    return text[:match.start()]+block+text[match.start():]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--domain',default='prognozich.online');parser.add_argument('--check',action='store_true');args=parser.parse_args()
    domain=domain_name(args.domain);path=Path('/etc/nginx/sites-available')/('matchlab-'+domain+'.conf')
    try:
        text=path.read_text();value=updated(text)
        if value==text:print('Защита callback уже настроена.');return
        if args.check:print('Можно добавить отдельный callback-прокси без записи кодов входа в журнал.');return
        if os.geteuid()!=0:raise ValueError('Запустите эту команду через sudo.')
        backup=Path('/var/backups/matchlab/nginx');backup.mkdir(parents=True,exist_ok=True)
        saved=backup/('social-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.conf')
        shutil.copyfile(path,saved);saved.chmod(0o600)
        apply_site(path,Path('/etc/nginx/sites-enabled')/path.name,value)
        print('Nginx проверен и перечитан; query-параметры OAuth callback не записываются в журнал.')
    except (OSError,ValueError,RuntimeError) as error:raise SystemExit(str(error)) from None


if __name__=='__main__':main()
