#!/usr/bin/env python3
"""Set login app credentials privately, preserving payment/model/database settings."""
import argparse
from datetime import datetime,timezone
import getpass
import os
from pathlib import Path
import re
import shutil

ROOT=Path(__file__).resolve().parent.parent


def configure(provider):
    os.umask(0o077)
    if provider=='yandex':
        client=input('Client ID приложения Яндекс ID: ').strip()
        secret=getpass.getpass('Client secret (ввод скрыт): ').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',client) or not secret or any(ord(c)<32 for c in secret):
            raise SystemExit('Проверьте Client ID и Client secret. Файл не изменён.')
        values={'YANDEX_CLIENT_ID':client,'YANDEX_CLIENT_SECRET':secret}
    else:
        client=input('ID веб-приложения VK ID (ВК, ОК и Mail.ru): ').strip()
        if not re.fullmatch(r'[0-9]{1,20}',client):raise SystemExit('Нужен числовой ID приложения VK ID. Файл не изменён.')
        values={'VKID_CLIENT_ID':client}
    path=ROOT/'.env';original=path.read_text() if path.exists() else ''
    if path.exists():
        backup=ROOT/('.env.before-social-login-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        shutil.copyfile(path,backup);backup.chmod(0o600)
    lines=[line for line in original.splitlines() if not any(re.match(r'\s*(?:export\s+)?'+key+r'\s*=',line) for key in values)]
    lines += [key+"='"+value.replace("'","\\'")+"'" for key,value in values.items()]
    temporary=ROOT/'.env.social-new';fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as out:out.write('\n'.join(lines)+'\n');out.flush();os.fsync(out.fileno())
        temporary.replace(path);path.chmod(0o600)
    finally:
        if temporary.exists():temporary.unlink()
    print('Настройки входа сохранены приватно. Перезапустите matchlab.service.')
    print('Callback: https://prognozich.online/api/auth/social/'+('yandex' if provider=='yandex' else 'vkid')+'/callback')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('provider',choices=('yandex','vkid'));args=parser.parse_args();configure(args.provider)
