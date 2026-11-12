"""Production-mode contracts with a wholly fake YooKassa HTTP boundary."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from account_helpers import signed_in
from outcome_helpers import forecast
from matchlab import accounts,billing,billing_fiscal
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from matchlab.yookassa import ProviderMismatch


class LiveBillingTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.settings=Settings(Path(folder.name),public_origin='https://prognozich.online',billing_enabled=True,
            billing_mode='live',billing_receipts_enabled=True,yookassa_shop_id='100500',yookassa_secret_key='live_fixture')
        self.repo=Repository(self.settings.database);self.repo.initialize()
        self.payments={};self.receipts={};self.requests=[];self.fail_receipt=False
        self.addCleanup(patch.stopall)
        patch('matchlab.billing_fiscal.Worker.start').start()
        patch('matchlab.yookassa.httpx.request',side_effect=self.provider).start()
        self.client=TestClient(create_app(self.settings),base_url='https://prognozich.online');self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.user=signed_in(self.client,self.repo)
        self.client.cookies.set('__Host-prognozich',self.client.cookies.get('prognozich_session'))

    def provider(self,method,url,**kw):
        data=kw.get('json');key=(kw.get('headers') or {}).get('Idempotence-Key');self.requests.append((method,url,deepcopy(data),key))
        if method=='POST' and url.endswith('/payments'):
            identifier='payment-'+data['metadata']['order_id']
            value=self.payments.setdefault(identifier,{'id':identifier,'status':'pending','paid':False,'test':False,
                'recipient':{'account_id':'100500'},'amount':data['amount'],'metadata':data['metadata'],
                'confirmation':{'confirmation_url':'https://yoomoney.ru/checkout?orderId='+identifier}})
        elif method=='GET' and '/payments/' in url:value=self.payments[url.rsplit('/',1)[-1]]
        elif method=='POST' and url.endswith('/receipts'):
            if self.fail_receipt:raise httpx.ReadTimeout('fake timeout')
            value=self.receipts.setdefault('receipt-'+key,{**data,'id':'receipt-'+key,'status':'succeeded'})
        elif method=='GET' and '/receipts/' in url:value=self.receipts[url.rsplit('/',1)[-1]]
        elif method=='GET' and url.endswith('/me'):value={'account_id':'100500','test':False,'fiscalization':{'enabled':True}}
        else:raise AssertionError('Unexpected fake-provider request')
        return httpx.Response(200,json=deepcopy(value),request=httpx.Request(method,url))

    def order(self,**extra):
        body={'package_id':'one','request_key':uuid4().hex,'offer_accepted':True,'offer_version':billing.PUBLIC_OFFER_VERSION,
            'receipt_email':'buyer@example.ru',**extra}
        response=self.client.post('/api/billing/orders',json=body)
        self.assertEqual(response.status_code,200,response.text);return response.json(),body

    def succeed(self,order):
        self.payments['payment-'+order['id']].update(status='succeeded',paid=True,receipt_registration='succeeded')
        result=self.client.get('/api/billing/orders/'+order['id']);self.assertEqual(result.status_code,200,result.text)
        return result.json()

    def wallet(self):return self.client.get('/api/billing/wallet').json()

    def save(self):
        payload=forecast(self.repo,uuid4().hex,kickoff=datetime.now(timezone.utc)+timedelta(hours=2),
            at=datetime.now(timezone.utc)-timedelta(seconds=20),score=None)
        with self.repo.connection() as db:db.execute('UPDATE event_analyses SET user_id=? WHERE id=?',(self.user,payload['id']))
        return payload

    def unlock(self,payload):return self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={})

    def test_real_checkout_has_public_offer_and_email_without_test_labels_or_secrets(self):
        config=self.client.get('/api/billing/config').json()
        self.assertTrue(config['checkout_enabled']);self.assertFalse(config['test_mode']);self.assertFalse(config['test_checkout_allowed'])
        self.assertEqual(config['offer_url'],'/static/legal/offer.html');self.assertTrue(config['receipt_email_required'])
        self.assertNotIn('live_fixture',json.dumps(config));self.assertNotIn('100500',json.dumps(config))
        for name in ('pricing','offer','payment-refund','terms','privacy'):
            text=self.client.get('/static/legal/'+name+'.html').text
            self.assertNotIn('Приём оплаты ещё не открыт',text);self.assertNotIn('планируется использование ЮKassa',text)

    def test_receipt_contact_is_validated_before_creating_order_or_provider_call(self):
        for email in (None,'','bad','a@b.ru\nbcc:attacker@example.ru','a..b@example.ru'):
            body={'package_id':'one','request_key':uuid4().hex,'offer_accepted':True,'offer_version':billing.PUBLIC_OFFER_VERSION,'receipt_email':email}
            self.assertEqual(self.client.post('/api/billing/orders',json=body).status_code,422)
        self.assertEqual(self.requests,[])
        with self.repo.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM payment_orders').fetchone()[0],0)

    def test_existing_account_can_renew_documents_then_retry_same_purchase_without_a_loop(self):
        with self.repo.connection() as db:db.execute('DELETE FROM registration_agreements WHERE user_id=?',(self.user,))
        body={'package_id':'one','request_key':uuid4().hex,'offer_accepted':True,'offer_version':billing.PUBLIC_OFFER_VERSION,'receipt_email':'buyer@example.ru'}
        response=self.client.post('/api/billing/orders',json=body)
        self.assertEqual(response.status_code,403);self.assertEqual(response.headers['X-Legal-Required'],'1');self.assertEqual(self.requests,[])
        self.client.post('/api/auth/confirm-documents',json={'terms_accepted':True,'personal_data_consent':True,'age_confirmed':True,'legal_version':accounts.LEGAL_VERSION})
        self.assertEqual(self.client.post('/api/billing/orders',json=body).status_code,200)

    def test_server_price_and_prepayment_receipt_are_exact_and_retries_are_idempotent(self):
        order,body=self.order(package_id='five',credits=1000,amount_kopecks=1,test_mode=True)
        self.assertEqual(self.wallet()['balance'],0)
        request=self.requests[0];receipt=request[2]['receipt'];item=receipt['items'][0]
        self.assertEqual((item['quantity'],item['amount']['value'],item['vat_code'],item['payment_mode']),('5.00','438.00',1,'full_prepayment'))
        self.assertEqual(receipt['customer'],{'email':'buyer@example.ru'})
        self.assertNotIn('(тест)',request[2]['description']);self.assertEqual(request[2]['amount']['value'],'2190.00')
        self.succeed(order)
        for _ in range(3):self.client.post('/api/billing/orders',json=body)
        self.assertEqual(self.wallet()['balance'],5);self.assertEqual(sum(r[0]=='POST' and r[1].endswith('/payments') for r in self.requests),1)

    def test_test_payment_or_wrong_shop_currency_amount_and_identity_never_credit_live_wallet(self):
        for change in ({'test':True},{'test':None},{'recipient':{'account_id':'other'}},{'amount':{'currency':'USD','value':'490.00'}},
                {'amount':{'currency':'RUB','value':'4.90'}},{'metadata':{'order_id':'other','user_id':self.user}},{'paid':False}):
            order,_=self.order();self.payments['payment-'+order['id']].update(status='succeeded',paid=True,**{k:v for k,v in change.items() if k!='paid'})
            if 'paid' in change:self.payments['payment-'+order['id']]['paid']=change['paid']
            self.assertEqual(self.client.get('/api/billing/orders/'+order['id']).status_code,502)
            self.assertEqual(self.wallet()['balance'],0)

    def test_false_notification_does_not_credit_and_verified_duplicate_notification_credits_once(self):
        order,_=self.order();event={'event':'payment.succeeded','object':{'id':'payment-'+order['id'],'metadata':{'order_id':order['id']}}}
        self.client.post('/api/billing/yookassa/webhook',json=event);self.assertEqual(self.wallet()['balance'],0)
        self.succeed(order)
        for _ in range(3):self.assertEqual(self.client.post('/api/billing/yookassa/webhook',json=event).status_code,200)
        self.assertEqual(self.wallet()['balance'],1)

    def test_atomic_spend_allocates_one_package_unit_and_enqueues_one_settlement_receipt(self):
        order,_=self.order(package_id='five');self.succeed(order);payload=self.save()
        with ThreadPoolExecutor(max_workers=6) as pool:
            values=list(pool.map(lambda _:billing.unlock(self.repo,self.settings,{'id':self.user},payload['id']),range(10)))
        self.assertEqual(sum(v['charged'] for v in values),1);self.assertEqual(self.wallet()['balance'],4)
        with self.repo.connection() as db:
            allocation=db.execute('SELECT * FROM analysis_credit_allocations').fetchone()
            self.assertEqual((allocation['order_id'],allocation['unit_kopecks']),(order['id'],43800))
            self.assertEqual(db.execute("SELECT count(*) FROM billing_receipt_jobs WHERE kind='settlement'").fetchone()[0],1)

    def test_receipt_retry_keeps_same_key_and_settles_by_prepayment_without_new_charge(self):
        order,_=self.order();self.succeed(order);self.assertTrue(billing_fiscal.process_one(self.repo,self.settings))
        payload=self.save();self.assertEqual(self.unlock(payload).status_code,200)
        self.fail_receipt=True;self.assertTrue(billing_fiscal.process_one(self.repo,self.settings))
        self.fail_receipt=False
        with self.repo.connection() as db:db.execute("UPDATE billing_receipt_jobs SET next_attempt_at=? WHERE kind='settlement'",((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
        self.assertTrue(billing_fiscal.process_one(self.repo,self.settings));self.assertFalse(billing_fiscal.process_one(self.repo,self.settings))
        requests=[r for r in self.requests if r[0]=='POST' and r[1].endswith('/receipts')]
        self.assertEqual(len(requests),2);self.assertEqual(requests[0][3],requests[1][3])
        self.assertEqual(requests[1][2]['settlements'],[{'type':'prepayment','amount':{'value':'490.00','currency':'RUB'}}])
        self.assertEqual(requests[1][2]['items'][0]['payment_mode'],'full_payment')
        self.assertEqual(sum(r[0]=='POST' and r[1].endswith('/payments') for r in self.requests),1)

    def test_pausing_new_sales_does_not_stop_receipts_for_existing_paid_access(self):
        order,_=self.order();self.succeed(order)
        paused=replace(self.settings,billing_enabled=False)
        self.assertTrue(billing_fiscal.process_one(self.repo,paused))
        billing.unlock(self.repo,paused,{'id':self.user},self.save()['id'])
        self.assertTrue(billing_fiscal.process_one(self.repo,paused))
        self.assertEqual(self.wallet()['balance'],0)

    def test_fifo_refund_uses_package_price_and_never_debits_next_purchase_for_used_service(self):
        order,_=self.order(package_id='five');self.succeed(order)
        for _ in range(2):self.assertEqual(self.unlock(self.save()).status_code,200)
        refund={'id':'refund-'+uuid4().hex,'status':'succeeded','payment_id':'payment-'+order['id'],'amount':{'currency':'RUB','value':'1314.00'}}
        for _ in range(2):billing.apply_refund(self.repo,self.settings,refund)
        self.assertEqual(self.wallet()['balance'],0)
        compensation={**refund,'id':'refund-'+uuid4().hex,'amount':{'currency':'RUB','value':'876.00'}}
        billing.apply_refund(self.repo,self.settings,compensation)
        self.assertEqual(self.wallet()['adjustment_due'],0)
        other,_=self.order();self.succeed(other);self.assertEqual(self.wallet()['balance'],1)

    def test_transition_keeps_already_opened_answers_without_converting_test_balance(self):
        opened=self.save();closed=self.save()
        with self.repo.connection() as db:
            db.execute("DELETE FROM site_settings WHERE key='billing_live_started'")
            db.execute('INSERT INTO site_settings VALUES (?,?,?)',('billing_test_started','yes',accounts.now()))
            db.execute('INSERT INTO analysis_unlocks VALUES (?,?,?,?,?)',(self.user,opened['id'],'test',accounts.now(),'credit'))
            db.execute('INSERT INTO analysis_credit_ledger VALUES (?,?,?,?,?,?,?)',(uuid4().hex,self.user,'test',10,'payment','old-test',accounts.now()))
        billing.initialize(self.repo,self.settings);billing.initialize(self.repo,self.settings)
        self.assertTrue(billing.entitled(self.repo,{'id':self.user},opened['id'],self.settings))
        self.assertFalse(billing.entitled(self.repo,{'id':self.user},closed['id'],self.settings))
        self.assertEqual(self.wallet()['balance'],0)
        self.assertEqual(billing.wallet(self.repo,self.user,replace(self.settings,billing_mode='test'))['balance'],10)


if __name__=='__main__':unittest.main()
