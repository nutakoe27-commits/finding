"""Safe recurring switch and current public-document contracts."""
from contextlib import redirect_stdout
from dataclasses import replace
import importlib.util
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from matchlab.config import Settings
from matchlab import billing,accounts,subscriptions
ROOT=Path(__file__).resolve().parent.parent


class SubscriptionSetupTests(unittest.TestCase):
    def module(self):
        spec=importlib.util.spec_from_file_location('configure_subscriptions',ROOT/'scripts/configure_subscriptions.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
    def test_enable_requires_explicit_approval_before_any_network_or_write(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as folder,patch.object(module,'ROOT',Path(folder)),patch('matchlab.yookassa.YooKassa.request') as api:
            with self.assertRaises(SystemExit):module.configure(True)
            self.assertFalse((Path(folder)/'.env').exists());api.assert_not_called()
    def test_only_feature_flag_changes_and_backup_is_private_and_secrets_not_printed(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as folder,patch.object(module,'ROOT',Path(folder)):
            path=Path(folder)/'.env';original='MATCHLAB_DATABASE_URL=mysql://fixture\nQWEN_API_KEY=local-secret\nYOOKASSA_SECRET_KEY=live_fixture\nBILLING_RECURRING_ENABLED=true\n'
            path.write_text(original);out=StringIO()
            with redirect_stdout(out):module.configure(False)
            self.assertIn('QWEN_API_KEY=local-secret',path.read_text());self.assertIn('BILLING_RECURRING_ENABLED=false',path.read_text())
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            backup=next(Path(folder).glob('.env.before-subscriptions-*'));self.assertEqual(backup.read_text(),original)
            self.assertNotIn('local-secret',out.getvalue());self.assertNotIn('live_fixture',out.getvalue())
    def test_enable_checks_live_shop_and_cash_register_but_never_creates_payment(self):
        module=self.module();settings=Settings(Path('/tmp/fixture'),billing_enabled=True,billing_mode='live',billing_receipts_enabled=True,
            yookassa_shop_id='100500',yookassa_secret_key='live_fixture')
        with tempfile.TemporaryDirectory() as folder,patch.object(module,'ROOT',Path(folder)),patch('matchlab.config.Settings.from_env',return_value=settings),patch('matchlab.yookassa.YooKassa.request',return_value={'account_id':'100500','test':False,'fiscalization':{'enabled':True}}) as request,redirect_stdout(StringIO()):
            module.configure(True,True);self.assertIn('BILLING_RECURRING_ENABLED=true',(Path(folder)/'.env').read_text())
            request.assert_called_once_with('GET','/me')
    def test_current_documents_describe_daily_charges_accumulation_cancellation_and_free_skips(self):
        for name in ('offer','pricing','recurring','payment-refund'):
            text=(ROOT/'matchlab/static/legal'/f'{name}.html').read_text()
            self.assertIn('ТОЛЬКО',text);self.assertIn('Пропустить',text);self.assertIn('24 час',text);self.assertIn('отмен',text.lower())
            self.assertNotIn('Подписок и автоматических списаний нет',text)
        self.assertEqual(accounts.LEGAL_VERSION,billing.PUBLIC_OFFER_VERSION)
        self.assertEqual([p['amount_kopecks'] for p in subscriptions.plans()],[49000,219000,609000])
if __name__=='__main__':unittest.main()
