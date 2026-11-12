from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from matchlab.config import Settings
import scripts.configure_social_login as setup
import scripts.configure_social_proxy as proxy
from scripts.configure_domain import configuration


class SocialSetupTests(unittest.TestCase):
    def test_hidden_credentials_preserve_payment_model_and_database_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'.env';path.write_text('QWEN_MODEL=original\nBILLING_MODE=live\nMATCHLAB_DATABASE_URL=private-database\n')
            output=io.StringIO();previous=os.umask(0o077)
            try:
                with patch.object(setup,'ROOT',root),patch('builtins.input',return_value='a'*32),patch('getpass.getpass',return_value='hidden-fixture-secret'),redirect_stdout(output):setup.configure('yandex')
                with patch.object(setup,'ROOT',root),patch('builtins.input',return_value='51400001'),redirect_stdout(output):setup.configure('vkid')
            finally:os.umask(previous)
            text=path.read_text()
            for key in ('QWEN_MODEL=original','BILLING_MODE=live','MATCHLAB_DATABASE_URL=private-database',"VKID_CLIENT_ID='51400001'"):self.assertIn(key,text)
            self.assertNotIn('hidden-fixture-secret',output.getvalue());self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_configuration_requires_yandex_pair_and_only_numeric_vk_id(self):
        with patch('matchlab.config.load_dotenv'),patch.dict(os.environ,{'YANDEX_CLIENT_ID':'a'*32},clear=True):
            with self.assertRaises(ValueError):Settings.from_env()
        with patch('matchlab.config.load_dotenv'),patch.dict(os.environ,{'VKID_CLIENT_ID':'51400001'},clear=True):self.assertEqual(Settings.from_env().vkid_client_id,'51400001')

    def test_proxy_patch_preserves_tls_port_and_existing_locations_and_is_idempotent(self):
        source=configuration('prognozich.online',18081,cert='/private/cert.pem',key='/private/key.pem',acme_root='/acme')
        # Simulate the preceding managed version, without callback privacy.
        marker='    '+proxy.SOCIAL_MARKER
        a=source.index(marker);b=source.index('    location / {',a)
        previous=source[:a]+source[b:];value=proxy.updated(previous)
        self.assertEqual(proxy.updated(value),value)
        for text in ('proxy_pass http://127.0.0.1:18081;','ssl_certificate /private/cert.pem;','ssl_certificate_key /private/key.pem;',
                     'return 404;','limit_req_status 429;','access_log off;','root /acme;'):self.assertIn(text,value)
        self.assertEqual(value.count('access_log off;'),1)
        with self.assertRaises(ValueError):proxy.updated('server { location / { return 200; } }')
