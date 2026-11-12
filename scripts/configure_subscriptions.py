#!/usr/bin/env python3
"""Explicitly enable approved recurring payments, or pause them safely."""
import argparse
from datetime import datetime,timezone
import os
from pathlib import Path
import re
import shutil
import sys
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))


def configure(enable,approved=False):
    if enable and not approved:raise SystemExit('Сначала получите подтверждение включения автоплатежей в ЮKassa. Затем используйте --enable --yookassa-approved. Файл не изменён.')
    if enable:
        from matchlab.config import Settings
        from matchlab.yookassa import YooKassa,ProviderUnavailable
        s=Settings.from_env()
        if not s.billing_enabled or s.billing_mode!='live' or not s.billing_receipts_enabled:
            raise SystemExit('Сначала настройте рабочий магазин и кассу. Файл не изменён.')
        try:value=YooKassa(s).request('GET','/me')
        except ProviderUnavailable:raise SystemExit('Нет подтверждённого доступа к рабочему магазину. Файл не изменён.') from None
        if value.get('account_id')!=s.yookassa_shop_id or value.get('test') is not False or (value.get('fiscalization') or {}).get('enabled') is not True:
            raise SystemExit('Рабочий магазин или касса не подтверждены. Файл не изменён.')
        # /me cannot prove that recurring payments were commercially approved;
        # --yookassa-approved records the operator's explicit precondition.
    os.umask(0o077);path=ROOT/'.env';original=path.read_text() if path.exists() else ''
    if path.exists():
        backup=ROOT/('.env.before-subscriptions-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        shutil.copyfile(path,backup);backup.chmod(0o600)
    lines=[s for s in original.splitlines() if not re.match(r'\s*(?:export\s+)?BILLING_RECURRING_ENABLED\s*=',s)]
    lines.append('BILLING_RECURRING_ENABLED='+('true' if enable else 'false'))
    temporary=ROOT/'.env.subscriptions-new';fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as f:f.write('\n'.join(lines)+'\n');f.flush();os.fsync(f.fileno())
        temporary.replace(path);path.chmod(0o600)
    finally:
        if temporary.exists():temporary.unlink()
    print('Подписки '+('включены' if enable else 'приостановлены')+'. Перезапустите matchlab.service. Другие настройки сохранены.')


if __name__=='__main__':
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--enable',action='store_true');g.add_argument('--disable',action='store_true')
    p.add_argument('--yookassa-approved',action='store_true');args=p.parse_args();configure(args.enable,args.yookassa_approved)
