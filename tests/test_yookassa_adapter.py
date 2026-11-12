from dataclasses import replace
import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx
from matchlab.config import Settings
from matchlab.yookassa import YooKassa,ProviderUnavailable,ProviderMismatch,confirmation_url,kopecks


class AdapterTests(unittest.TestCase):
    def setUp(self):self.settings=Settings(Path('/tmp'),'','',billing_enabled=True,yookassa_shop_id='100500',yookassa_secret_key='test_fixture')

    def test_basic_auth_idempotence_header_and_integer_price_are_built_on_server(self):
        order={'id':'a'*32,'amount_kopecks':49000,'credits':1,'user_id':'owner'}
        response=httpx.Response(200,json={'id':'payment-id'},request=httpx.Request('POST','https://api.yookassa.ru/v3/payments'))
        with patch('matchlab.yookassa.httpx.request',return_value=response) as request:YooKassa(self.settings).create(order,'https://prognozich.online')
        args=request.call_args
        self.assertEqual(args.args,('POST','https://api.yookassa.ru/v3/payments'))
        self.assertEqual(args.kwargs['headers'],{'Idempotence-Key':'a'*32})
        self.assertEqual(args.kwargs['auth'],('100500','test_fixture'))
        self.assertEqual(args.kwargs['json']['amount'],{'value':'490.00','currency':'RUB'})
        self.assertEqual(args.kwargs['json']['confirmation']['return_url'],'https://prognozich.online/#account')

    def test_live_key_is_rejected_before_any_provider_request(self):
        self.assertNotIn('test_fixture',repr(self.settings))
        with patch('matchlab.yookassa.httpx.request',side_effect=AssertionError('network')):
            with self.assertRaises(ProviderUnavailable):YooKassa(replace(self.settings,yookassa_secret_key='live_secret')).request('GET','/payments')

    def test_explicit_live_mode_adds_prepayment_receipt_with_no_vat(self):
        settings=replace(self.settings,billing_mode='live',billing_receipts_enabled=True,yookassa_secret_key='live_fixture',yookassa_tax_system_code=2)
        order={'id':'a'*32,'amount_kopecks':409000,'credits':10,'user_id':'owner','receipt_email':'buyer@example.ru'}
        response=httpx.Response(200,json={'id':'payment-id'},request=httpx.Request('POST','https://api.yookassa.ru/v3/payments'))
        with patch('matchlab.yookassa.httpx.request',return_value=response) as request:YooKassa(settings).create(order,'https://prognozich.online')
        body=request.call_args.kwargs['json'];item=body['receipt']['items'][0]
        self.assertEqual(body['receipt']['tax_system_code'],2)
        self.assertEqual((item['quantity'],item['amount']['value'],item['vat_code'],item['payment_mode']),('10.00','409.00',1,'full_prepayment'))
        self.assertNotIn('тест',body['description'])

    def test_live_configuration_checks_api_and_cash_register_before_writing_private_settings(self):
        import scripts.configure_yookassa as configure
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('QWEN_MODEL=original\n')
            output=io.StringIO();previous=os.umask(0o077)
            try:
                with patch.object(configure,'ROOT',root),patch.dict(os.environ,{'MATCHLAB_DATA_DIR':str(root/'.local')}),patch('builtins.input',return_value='100500'),patch('getpass.getpass',return_value='live_hidden_fixture'),patch('matchlab.yookassa.YooKassa.request',return_value={'account_id':'100500','test':False,'fiscalization':{'enabled':True}}),redirect_stdout(output):configure.main(live=True)
            finally:os.umask(previous)
            text=(root/'.env').read_text();self.assertIn("BILLING_MODE='live'",text);self.assertIn("BILLING_RECEIPTS_ENABLED='true'",text)
            self.assertIn('QWEN_MODEL=original',text);self.assertNotIn('live_hidden_fixture',output.getvalue())
            self.assertEqual((root/'.env').stat().st_mode&0o777,0o600)

    def test_live_environment_rejects_mixed_store_key_and_disabled_receipts(self):
        env={'BILLING_ENABLED':'true','BILLING_MODE':'live','BILLING_RECEIPTS_ENABLED':'true',
            'YOOKASSA_SHOP_ID':'100500','YOOKASSA_SECRET_KEY':'live_fixture'}
        with patch('matchlab.config.load_dotenv'),patch.dict(os.environ,env,clear=True):
            self.assertEqual(Settings.from_env().billing_mode,'live')
            for field,value in (('YOOKASSA_SECRET_KEY','test_fixture'),('BILLING_RECEIPTS_ENABLED','false'),('YOOKASSA_TAX_SYSTEM_CODE','1')):
                with patch.dict(os.environ,{field:value}),self.assertRaises(ValueError):Settings.from_env()

    def test_unconfirmed_cash_register_does_not_change_live_configuration(self):
        import scripts.configure_yookassa as configure
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/'.env';p.write_text('QWEN_MODEL=original\n')
            previous=os.umask(0o077)
            try:
                with patch.object(configure,'ROOT',root),patch('builtins.input',return_value='100500'),patch('getpass.getpass',return_value='live_hidden_fixture'),patch('matchlab.yookassa.YooKassa.request',return_value={'account_id':'100500','test':False,'fiscalization':{'enabled':False}}),self.assertRaises(SystemExit):configure.main(live=True)
            finally:os.umask(previous)
            self.assertEqual(p.read_text(),'QWEN_MODEL=original\n')

    def test_provider_errors_log_only_status_and_field_name_and_honor_retry_after(self):
        response=httpx.Response(400,json={'parameter':'receipt','description':'SECRET_PROVIDER_TEXT'},request=httpx.Request('POST','https://api.yookassa.ru/v3/payments'))
        with patch('matchlab.yookassa.httpx.request',return_value=response),self.assertLogs('uvicorn.error',level='WARNING') as logs:
            with self.assertRaises(ProviderUnavailable) as raised:YooKassa(self.settings).request('POST','/payments')
        self.assertTrue(raised.exception.definitive);self.assertIn('parameter=receipt',' '.join(logs.output));self.assertNotIn('SECRET_',' '.join(logs.output))
        response=httpx.Response(429,headers={'retry-after':'20'},request=httpx.Request('GET','https://api.yookassa.ru/v3/payments'))
        with patch('matchlab.yookassa.httpx.request',return_value=response),self.assertLogs('uvicorn.error'):
            with self.assertRaises(ProviderUnavailable) as raised:YooKassa(self.settings).request('GET','/payments')
        self.assertEqual(raised.exception.retry_after,20)

    def test_confirmation_redirects_are_only_https_provider_urls(self):
        for url in ('https://evil.example/pay','javascript:alert(1)','https://yoomoney.ru.evil.example/pay','https://secret@yoomoney.ru/pay','https://yoomoney.ru:wrong/pay'):
            with self.subTest(url=url),self.assertRaises(ProviderMismatch):confirmation_url({'confirmation':{'confirmation_url':url}})
        self.assertEqual(confirmation_url({'confirmation':{'confirmation_url':'https://yoomoney.ru/checkout'}}),'https://yoomoney.ru/checkout')

    def test_decimal_currency_is_exact_and_does_not_allow_nonfinite_or_subkopeck_values(self):
        self.assertEqual(kopecks('2190.00'),219000)
        for value in ('NaN','Infinity','-1','1.001',None):
            with self.subTest(value=value),self.assertRaises(ProviderMismatch):kopecks(value)

    def test_configuration_script_keeps_other_settings_and_never_prints_key(self):
        import scripts.configure_yookassa as configure
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('QWEN_MODEL=original\nPUBLIC_ORIGIN=https://prognozich.online\n')
            output=io.StringIO()
            previous=os.umask(0o077)
            try:
                with patch.object(configure,'ROOT',root),patch.dict(os.environ,{'MATCHLAB_DATA_DIR':str(root/'.local')}),patch('builtins.input',return_value='100500'),patch('getpass.getpass',return_value='test_hidden_fixture'),redirect_stdout(output):configure.main()
            finally:os.umask(previous)
            text=(root/'.env').read_text();self.assertIn('QWEN_MODEL=original',text);self.assertIn("BILLING_ENABLED='true'",text)
            self.assertEqual((root/'.env').stat().st_mode&0o777,0o600);self.assertNotIn('test_hidden_fixture',output.getvalue())

    def test_configure_creates_a_consistent_database_backup_before_enabling(self):
        import scripts.configure_yookassa as configure
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data';data.mkdir()
            with sqlite3.connect(data/'matchlab.sqlite3') as db:db.execute('CREATE TABLE sample(value)');db.execute('INSERT INTO sample VALUES (7)')
            previous=os.umask(0o077)
            try:
                with patch.object(configure,'ROOT',root),patch.dict(os.environ,{'MATCHLAB_DATA_DIR':str(data)}),patch('builtins.input',return_value='100500'),patch('getpass.getpass',return_value='test_fixture'),redirect_stdout(io.StringIO()):configure.main()
            finally:os.umask(previous)
            backups=list(data.glob('matchlab-before-billing-*.sqlite3'));self.assertEqual(len(backups),1)
            with sqlite3.connect(backups[0]) as db:self.assertEqual(db.execute('SELECT value FROM sample').fetchone()[0],7)
            self.assertEqual(backups[0].stat().st_mode&0o777,0o600)
