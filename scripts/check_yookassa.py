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
    parser=argparse.ArgumentParser();parser.add_argument('--payment-id');parser.add_argument('--site',action='store_true');args=parser.parse_args()
    settings=Settings.from_env();provider=YooKassa(settings)
    try:
        if args.payment_id:
            from matchlab.billing import apply_payment
            value=provider.payment(args.payment_id)
            repo=Repository(settings.database);repo.initialize();result=apply_payment(repo,settings,value)
            print('Платёж проверен. Статус:',result['state'])
        else:
            result=provider.request('GET','/me')
            if result.get('account_id')!=settings.yookassa_shop_id or result.get('test') is not (settings.billing_mode=='test'):raise ProviderMismatch('store_mode')
            if settings.billing_mode=='live' and (result.get('fiscalization') or {}).get('enabled') is not True:raise ProviderMismatch('cash_register')
            print('API ЮKassa: доступен. Проверка выполнена без создания платежей.')
            print('Режим: '+('рабочий' if settings.billing_mode=='live' else 'тестовый')+'. Пакеты: 1 / 490 ₽, 5 / 2190 ₽, 10 / 4090 ₽.')
            if settings.billing_mode=='live':print('Передача чеков в подключённую кассу: включена.')
        if args.site:
            import httpx
            try:
                response=httpx.get(settings.public_origin+'/api/billing/config',timeout=15,follow_redirects=False)
                response.raise_for_status();config=response.json()
                if config.get('test_mode') is not (settings.billing_mode=='test') or settings.billing_mode=='live' and not (config.get('checkout_enabled') and config.get('receipt_email_required')):
                    raise ValueError
            except (httpx.HTTPError,ValueError):raise ProviderMismatch('site_configuration') from None
            print('Работающий сайт: режим магазина совпадает с настройками VPS.')
    except (ProviderUnavailable,ProviderMismatch):
        raise SystemExit('Проверка не пройдена: проверьте Shop ID, режим магазина, ключ и кассу на VPS. Ключ не выводится.') from None


if __name__=='__main__':main()
