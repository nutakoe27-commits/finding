#!/usr/bin/env python3
"""Configure Matchlab's YooKassa store with hidden input and private backups."""
import getpass
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))


def main(*,live=False,tax_system_code=0):
    os.umask(0o077)
    path=ROOT/'.env'
    mode='live' if live else 'test'
    shop=input('Shop ID '+('рабочего' if live else 'тестового')+' магазина ЮKassa: ').strip()
    secret=getpass.getpass('Секретный ключ (ввод скрыт): ').strip()
    if not shop.isdigit() or not secret.startswith(mode+'_') or any(ord(c)<32 for c in secret):
        raise SystemExit('Нужны числовой Shop ID и ключ выбранного режима. Файл не изменён.')
    if tax_system_code not in (0,2,3):raise SystemExit('Код УСН: 2 или 3; 0 — взять настройку кассы.')
    if live:
        from matchlab.config import Settings
        from matchlab.yookassa import YooKassa,ProviderUnavailable
        settings=Settings(ROOT/'.local',billing_mode='live',yookassa_shop_id=shop,yookassa_secret_key=secret)
        try:account=YooKassa(settings).request('GET','/me')
        except ProviderUnavailable:raise SystemExit('Рабочий магазин не подтвердил доступ к API. Настройки не изменены; ключ не выводится.') from None
        if account.get('account_id')!=shop or account.get('test') is not False:
            raise SystemExit('API не подтвердил указанный рабочий магазин. Настройки не изменены.')
        fiscal=account.get('fiscalization') or {}
        if fiscal.get('enabled') is not True:
            raise SystemExit('ЮKassa не подтвердила подключённую кассу (fiscalization.enabled). Проверьте настройку кассы в кабинете. Файл не изменён.')
    original=path.read_text() if path.exists() else ''
    from dotenv import dotenv_values
    values=dotenv_values(path) if path.exists() else {}
    data_dir=Path(os.getenv('MATCHLAB_DATA_DIR') or values.get('MATCHLAB_DATA_DIR') or ROOT/'.local')
    if not data_dir.is_absolute():data_dir=ROOT/data_dir
    database=data_dir/'matchlab.sqlite3'
    database_url=os.getenv('MATCHLAB_DATABASE_URL') or values.get('MATCHLAB_DATABASE_URL')
    if database_url:
        from matchlab.mysql_backend import MySQLTarget
        from matchlab.database_migration import export_sqlite
        snapshot=data_dir/('matchlab-before-billing-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3')
        export_sqlite(MySQLTarget(database_url),snapshot)
    elif database.exists():
        snapshot=data_dir/('matchlab-before-billing-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3')
        with sqlite3.connect(database) as source,sqlite3.connect(snapshot) as destination:source.backup(destination)
        snapshot.chmod(0o600)
    if path.exists():
        backup=path.with_name('.env.before-yookassa-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        shutil.copyfile(path,backup);backup.chmod(0o600)
    names={'BILLING_ENABLED':'true','BILLING_MODE':mode,'BILLING_RECEIPTS_ENABLED':'true' if live else 'false',
        'BILLING_PUBLIC_TESTING':'false','YOOKASSA_TAX_SYSTEM_CODE':str(tax_system_code),
        'YOOKASSA_SHOP_ID':shop,'YOOKASSA_SECRET_KEY':secret}
    lines=[line for line in original.splitlines() if not any(re.match(r'\s*(?:export\s+)?'+name+r'\s*=',line) for name in names)]
    for name,value in names.items():lines.append(name+"='"+value.replace("'","\\'")+"'")
    temporary=path.with_name('.env.yookassa-new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as f:f.write('\n'.join(lines)+'\n');f.flush();os.fsync(f.fileno())
        temporary.replace(path);path.chmod(0o600)
    finally:
        if temporary.exists():temporary.unlink()
    print(('Рабочая' if live else 'Тестовая')+' ЮKassa настроена. Секретный ключ сохранён приватно.')
    print('Следующие команды: sudo systemctl restart matchlab.service')
    print('Затем: .venv/bin/python scripts/check_yookassa.py')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true')
    parser.add_argument('--tax-system-code',type=int,choices=(0,2,3),default=0)
    args=parser.parse_args();main(live=args.live,tax_system_code=args.tax_system_code)
