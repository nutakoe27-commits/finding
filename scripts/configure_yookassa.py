#!/usr/bin/env python3
"""Configure only Matchlab's test store, without putting secrets in shell history."""
import getpass
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parent.parent


def main():
    os.umask(0o077)
    path=ROOT/'.env'
    shop=input('Shop ID тестового магазина ЮKassa: ').strip()
    secret=getpass.getpass('Тестовый секретный ключ (ввод скрыт): ').strip()
    if not shop.isdigit() or not secret.startswith('test_') or any(ord(c)<32 for c in secret):
        raise SystemExit('Нужны числовой Shop ID и тестовый ключ test_. Файл не изменён.')
    original=path.read_text() if path.exists() else ''
    from dotenv import dotenv_values
    values=dotenv_values(path) if path.exists() else {}
    data_dir=Path(os.getenv('MATCHLAB_DATA_DIR') or values.get('MATCHLAB_DATA_DIR') or ROOT/'.local')
    if not data_dir.is_absolute():data_dir=ROOT/data_dir
    database=data_dir/'matchlab.sqlite3'
    if database.exists():
        snapshot=data_dir/('matchlab-before-billing-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3')
        with sqlite3.connect(database) as source,sqlite3.connect(snapshot) as destination:source.backup(destination)
        snapshot.chmod(0o600)
    if path.exists():
        backup=path.with_name('.env.before-yookassa-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        shutil.copyfile(path,backup);backup.chmod(0o600)
    names={'BILLING_ENABLED':'true','YOOKASSA_SHOP_ID':shop,'YOOKASSA_SECRET_KEY':secret}
    lines=[line for line in original.splitlines() if not any(re.match(r'\s*(?:export\s+)?'+name+r'\s*=',line) for name in names)]
    for name,value in names.items():lines.append(name+"='"+value.replace("'","\\'")+"'")
    temporary=path.with_name('.env.yookassa-new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as f:f.write('\n'.join(lines)+'\n');f.flush();os.fsync(f.fileno())
        temporary.replace(path);path.chmod(0o600)
    finally:
        if temporary.exists():temporary.unlink()
    print('Тестовая ЮKassa настроена. Ключ не выведен. Настройки модели не менялись.')
    print('Следующие команды: sudo systemctl restart matchlab.service')
    print('Затем: .venv/bin/python scripts/check_yookassa.py')


if __name__=='__main__':main()
