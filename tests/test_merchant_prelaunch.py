"""Merchant review documents and closed public checkout; no real payments."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from matchlab import accounts,billing
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from account_helpers import signed_in


class MerchantPrelaunchTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.settings=Settings(Path(folder.name),billing_enabled=True,yookassa_shop_id='100500',yookassa_secret_key='test_merchant_fixture')
        self.repo=Repository(self.settings.database);self.repo.initialize()
        self.client=TestClient(create_app(self.settings));self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)

    def body(self):return {'package_id':'one','request_key':uuid4().hex,'offer_accepted':True,'offer_version':billing.OFFER_VERSION}

    def payment(self,order,origin):
        return {'id':'payment-'+order['id'],'status':'pending','paid':False,'test':True,
            'recipient':{'account_id':'100500'},'amount':{'currency':'RUB','value':'490.00'},
            'metadata':{'order_id':order['id'],'user_id':order['user_id']},
            'confirmation':{'confirmation_url':'https://yoomoney.ru/checkout/payments/v2/contract?orderId='+order['id']}}

    def test_public_prices_offer_and_contacts_are_readable_without_registration(self):
        for name in ('pricing','offer','payment-refund','contacts','terms','privacy','consent'):
            response=self.client.get('/static/legal/'+name+'.html')
            self.assertEqual(response.status_code,200,name)
            self.assertNotIn('REGISTRATION_AUTHORITY',response.text)
            self.assertNotRegex(response.text,r'(?i)(?:улица|квартира|ул\.|кв\.)')
            self.assertNotIn('условия тестового приобретения',response.text.lower())
        offer=self.client.get('/static/legal/offer.html').text
        for value in ('490 ₽','2 190 ₽','4 090 ₽','438 ₽','409 ₽','1 314 ₽','не имеют срока действия','НДС не предъявляется','статьёй 32'):
            self.assertIn(value,offer)
        self.assertIn('№ 46 по г. Москве',self.client.get('/static/legal/contacts.html').text)
        self.assertNotIn('Приём оплаты ещё не открыт',offer)
        self.assertIn('Разовый платёж через ЮKassa',offer)

    def test_guest_and_regular_account_cannot_create_payments_even_with_forged_flags(self):
        config=self.client.get('/api/billing/config')
        self.assertFalse(config.json()['checkout_enabled']);self.assertIn('no-store',config.headers['cache-control'])
        self.assertEqual(config.json()['offer_url'],'/static/legal/offer.html')
        self.assertNotIn('test_merchant_fixture',config.text);self.assertNotIn('100500',config.text)
        signed_in(self.client,self.repo)
        with patch('matchlab.billing.YooKassa.create',side_effect=AssertionError('public payment created')):
            response=self.client.post('/api/billing/orders',json={**self.body(),'role':'admin','test_checkout_allowed':True})
        self.assertEqual(response.status_code,503,response.text)
        self.assertFalse(self.client.get('/api/billing/config').json()['test_checkout_allowed'])
        with self.repo.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM payment_orders').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM analysis_credit_ledger').fetchone()[0],0)

    def test_admin_can_only_create_an_explicit_test_payment_and_demotion_removes_access(self):
        user=signed_in(self.client,self.repo,role='admin')
        config=self.client.get('/api/billing/config').json()
        self.assertTrue(config['test_checkout_allowed']);self.assertTrue(config['test_mode'])
        self.assertEqual(config['offer_url'],'/static/legal/test-purchase.html')
        with patch('matchlab.billing.YooKassa.create',side_effect=self.payment):
            response=self.client.post('/api/billing/orders',json=self.body())
        self.assertEqual(response.status_code,200,response.text);self.assertTrue(response.json()['test_mode'])
        with self.repo.connection() as db:db.execute("UPDATE users SET role='user' WHERE id=?",(user,))
        self.assertFalse(self.client.get('/api/billing/config').json()['checkout_enabled'])
        self.assertEqual(self.client.post('/api/billing/orders',json=self.body()).status_code,503)

    def test_existing_test_order_is_reconciled_without_reopening_public_checkout(self):
        identifier=signed_in(self.client,self.repo)
        user={'id':identifier,'role':'user'}
        with patch('matchlab.billing.YooKassa.create',side_effect=self.payment):
            order=billing.create_order(self.repo,replace(self.settings,billing_public_testing=True),user,self.body())
        with self.repo.connection() as db:raw=dict(db.execute('SELECT * FROM payment_orders WHERE id=?',(order['id'],)).fetchone())
        provider=self.payment(raw,'');provider.update(status='succeeded',paid=True)
        with patch('matchlab.billing.YooKassa.payment',return_value=provider):
            response=self.client.get('/api/billing/orders/'+order['id'])
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'succeeded')
        self.assertTrue(response.json()['test_mode'])
        self.assertEqual(self.client.get('/api/billing/wallet').json()['balance'],1)
        self.assertFalse(self.client.get('/api/billing/config').json()['checkout_enabled'])

    def test_new_registration_version_matches_documents_and_archived_test_terms_remain_truthful(self):
        for name in ('terms','consent','privacy'):
            self.assertIn(accounts.LEGAL_VERSION,self.client.get('/static/legal/'+name+'.html').text)
        legacy=self.client.get('/static/legal/test-purchase.html').text
        self.assertIn('noindex,nofollow',legacy);self.assertIn('Реальные деньги не списываются',legacy)
        home=self.client.get('/').text
        self.assertIn('/static/legal/offer.html',home);self.assertIn('/static/legal/payment-refund.html',home)
        self.assertNotIn('Тестовая покупка разборов',home);self.assertNotIn('Тестовая оплата · ЮKassa',home)


if __name__=='__main__':unittest.main()
