#!/usr/bin/env python3
"""Read-only API access check, or reconcile one known payment after a timeout."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from matchlab.config import Settings
from matchlab.storage import Repository
from matchlab.yookassa import YooKassa,ProviderUnavailable,ProviderMismatch


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--payment-id');args=parser.parse_args()
    settings=Settings.from_env();provider=YooKassa(settings)
    try:
        if args.payment_id:
            from matchlab.billing import apply_payment
            value=provider.payment(args.payment_id)
            repo=Repository(settings.database);repo.initialize();result=apply_payment(repo,settings,value)
            print('Платёж проверен. Статус:',result['state'])
        else:
            result=provider.request('GET','/payments?limit=1')
            items=result.get('items')
            if not isinstance(items,list) or any(item.get('test') is not True for item in items):raise ProviderMismatch('not_test_store')
            print('API ЮKassa: доступен. Проверка выполнена без создания платежей.')
            print('Режим: тестовый. Пакеты: 1 / 490 ₽, 5 / 2190 ₽, 10 / 4090 ₽.')
    except (ProviderUnavailable,ProviderMismatch):
        raise SystemExit('Проверка не пройдена: проверьте тестовый Shop ID и ключ на VPS. Ключ не выводится.') from None


if __name__=='__main__':main()
