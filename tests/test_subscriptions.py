"""Recurring billing and bet-only spending through a wholly fake HTTP provider."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from account_helpers import signed_in
from outcome_helpers import forecast
from matchlab import accounts,billing,subscriptions,billing_fiscal
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.settings=Settings(Path(folder.name),public_origin='https://prognozich.online',billing_enabled=True,
            billing_mode='live',billing_receipts_enabled=True,billing_recurring_enabled=True,
            yookassa_shop_id='100500',yookassa_secret_key='live_fixture',ai_daily_limit=1)
        self.repo=Repository(self.settings.database);self.repo.initialize()
        if os.getenv('SUBSCRIPTIONS_USE_MYSQL')=='1':
            import pymysql
            from matchlab.mysql_backend import MySQLTarget,quote
            from matchlab.database_migration import migrate
            target=MySQLTarget(os.environ['MYSQL_TEST_ADMIN_URL']);options=target.options()
            if options['host'] not in ('127.0.0.1','localhost') or options['database']!='matchlab_test':raise RuntimeError('Disposable local MySQL required')
            admin=pymysql.connect(**options,autocommit=True);self.addCleanup(admin.close)
            name='subscription_test_'+uuid4().hex[:12];admin.cursor().execute('CREATE DATABASE '+quote(name)+' CHARACTER SET utf8mb4')
            self.addCleanup(lambda:admin.cursor().execute('DROP DATABASE '+quote(name)))
            url=target.url.rsplit('/',1)[0]+'/'+name
            migrate(self.settings.database,MySQLTarget(url),Path(folder.name)/'before.sqlite3',apply=True)
            self.settings=replace(self.settings,database_url=url);self.repo=Repository(self.settings.database);self.repo.initialize()
        self.requests=[];self.payments={};self.fail=False;self.saved=True;self.next_paid=False
        self.addCleanup(patch.stopall)
        patch('matchlab.billing_fiscal.Worker.start').start();patch('matchlab.subscriptions.Worker.start').start()
        patch('matchlab.yookassa.httpx.request',side_effect=self.provider).start()
        self.client=TestClient(create_app(self.settings),base_url=self.settings.public_origin)
        self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.user=signed_in(self.client,self.repo)
        self.client.cookies.set('__Host-prognozich',self.client.cookies.get('prognozich_session'))

    def provider(self,method,url,**kw):
        data=kw.get('json');key=(kw.get('headers') or {}).get('Idempotence-Key')
        self.requests.append((method,url,deepcopy(data),key))
        if method=='POST' and url.endswith('/payments'):
            if self.fail:raise httpx.ReadTimeout('fixture timeout')
            value=self.payments.setdefault('payment-'+key,{'id':'payment-'+key,'status':'succeeded' if self.next_paid else 'pending',
                'paid':self.next_paid,'test':False,'recipient':{'account_id':'100500'},'amount':data['amount'],
                'metadata':data['metadata'],'payment_method':{'id':data.get('payment_method_id','saved-card-fixture-001'),
                'type':'bank_card','saved':self.saved},'receipt_registration':'succeeded',
                'confirmation':{} if data.get('payment_method_id') else {'confirmation_url':'https://yoomoney.ru/checkout?orderId='+key}})
        elif method=='GET' and '/payments/' in url:value=self.payments[url.rsplit('/',1)[-1]]
        else:raise AssertionError('Unexpected external call: '+method+' '+url)
        return httpx.Response(200,json=deepcopy(value),request=httpx.Request(method,url))

    def body(self,plan='daily_one',**kw):
        return dict(plan_id=plan,request_key=uuid4().hex,offer_accepted=True,offer_version=billing.PUBLIC_OFFER_VERSION,
            recurring_accepted=True,recurring_version=subscriptions.VERSION,receipt_email='buyer@example.ru',**kw)

    def start(self,plan='daily_one'):
        body=self.body(plan);r=self.client.post('/api/billing/subscriptions',json=body)
        self.assertEqual(r.status_code,200,r.text);return r.json(),body

    def succeed(self,order):
        self.payments['payment-'+order['id']].update(status='succeeded',paid=True)
        r=self.client.get('/api/billing/orders/'+order['id']);self.assertEqual(r.status_code,200,r.text);return r.json()

    def wallet(self):return self.client.get('/api/billing/wallet').json()

    def save(self,decision='bet'):
        payload=forecast(self.repo,uuid4().hex,kickoff=datetime.now(timezone.utc)+timedelta(hours=2),
            at=datetime.now(timezone.utc)-timedelta(seconds=20),score=None,decision=decision)
        with self.repo.connection() as db:db.execute('UPDATE event_analyses SET user_id=? WHERE id=?',(self.user,payload['id']))
        return payload

    def due(self,order):
        past=(datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat()
        with self.repo.connection() as db:
            db.execute('UPDATE billing_subscriptions SET period_end_at=? WHERE id=?',(past,order['subscription_id']))
            db.execute('UPDATE billing_subscription_periods SET ends_at=? WHERE order_id=?',(past,order['id']))

    def test_flag_defaults_off_and_cannot_charge_even_with_existing_shop_keys(self):
        paused=replace(self.settings,billing_recurring_enabled=False)
        with self.assertRaises(Exception) as exc:subscriptions.create(self.repo,paused,{'id':self.user},self.body())
        self.assertEqual(exc.exception.status_code,503);self.assertEqual(self.requests,[])
        self.assertFalse(Settings(Path('/tmp/fixture')).billing_recurring_enabled)

    def test_requires_separate_current_consent_and_valid_email_before_any_provider_call(self):
        for field,value in [('recurring_accepted',False),('offer_accepted',False),('recurring_version','old'),('receipt_email','bad')]:
            body=self.body();body[field]=value
            self.assertEqual(self.client.post('/api/billing/subscriptions',json=body).status_code,422)
        self.assertEqual(self.requests,[])

    def test_initial_charge_is_card_save_with_server_price_receipt_and_immutable_consent(self):
        body=self.body('daily_five');body.update(credits=999,amount_kopecks=1)
        r=self.client.post('/api/billing/subscriptions',json=body);self.assertEqual(r.status_code,200,r.text)
        request=self.requests[0][2]
        self.assertEqual(request['amount']['value'],'2190.00');self.assertTrue(request['save_payment_method'])
        self.assertEqual(request['payment_method_data'],{'type':'bank_card'});self.assertNotIn('payment_method_id',request)
        item=request['receipt']['items'][0];self.assertEqual((item['quantity'],item['amount']['value'],item['payment_mode']),('5.00','438.00','full_prepayment'))
        with self.repo.connection() as db:
            sub=db.execute('SELECT * FROM billing_subscriptions').fetchone()
            self.assertIn('2190 ₽',sub['consent_text']);self.assertIn('24 часа',sub['consent_text']);self.assertEqual(sub['recurring_version'],subscriptions.VERSION)

    def test_pending_return_and_forged_webhook_do_not_grant_or_publish_saved_payment_method(self):
        order,_=self.start()
        self.client.post('/api/billing/yookassa/webhook',json={'event':'payment.succeeded','object':{
            'id':'payment-'+order['id'],'metadata':{'order_id':order['id']},'status':'succeeded'}})
        self.assertEqual(self.wallet()['balance'],0)
        text=json.dumps(self.wallet())+self.client.get('/api/billing/subscriptions').text
        self.assertNotIn('saved-card',text);self.assertNotIn('payment_method_id',text);self.assertNotIn('live_fixture',text)

    def test_first_payment_idempotently_grants_permanent_credits_once(self):
        order,body=self.start('daily_five');self.succeed(order)
        for _ in range(3):
            self.client.post('/api/billing/subscriptions',json=body);self.succeed(order)
        self.assertEqual(self.wallet()['balance'],5)
        with self.repo.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM billing_subscription_periods').fetchone()[0],1)
        self.assertEqual(sum(r[0]=='POST' for r in self.requests),1)

    def test_only_one_subscription_can_be_created_even_with_different_request_keys(self):
        self.start()
        response=self.client.post('/api/billing/subscriptions',json=self.body('daily_five'))
        self.assertEqual(response.status_code,409);self.assertEqual(sum(r[0]=='POST' for r in self.requests),1)

    def test_next_day_charge_uses_saved_method_without_confirmation_and_accumulates(self):
        order,_=self.start();self.succeed(order);self.due(order);self.next_paid=True
        self.assertTrue(subscriptions.process_one(self.repo,self.settings));self.assertFalse(subscriptions.process_one(self.repo,self.settings))
        self.assertEqual(self.wallet()['balance'],2)
        request=[r for r in self.requests if r[0]=='POST'][-1][2]
        self.assertEqual(request['payment_method_id'],'saved-card-fixture-001')
        self.assertNotIn('confirmation',request);self.assertNotIn('save_payment_method',request)

    def test_cancel_preserves_accumulated_credits_and_prevents_all_new_charges(self):
        order,_=self.start('daily_five');self.succeed(order)
        self.assertEqual(self.client.post('/api/billing/subscriptions/'+order['subscription_id']+'/cancel',json={}).status_code,200)
        self.due(order);self.assertFalse(subscriptions.process_one(self.repo,self.settings));self.assertEqual(self.wallet()['balance'],5)
        with self.repo.connection() as db:self.assertIsNone(db.execute('SELECT payment_method_id FROM billing_subscriptions').fetchone()[0])

    def test_cancel_during_pending_payment_never_reactivates_future_charges(self):
        order,_=self.start();self.client.post('/api/billing/subscriptions/'+order['subscription_id']+'/cancel',json={});self.succeed(order)
        s=self.wallet()['subscription']['current'];self.assertFalse(s['auto_renew']);self.assertEqual(self.wallet()['balance'],1)
        self.due(order);self.assertFalse(subscriptions.process_one(self.repo,self.settings))

    def test_timeout_retry_reuses_same_order_key_and_unknown_aged_payment_requires_review(self):
        self.fail=True;order,body=self.start();self.assertEqual(order['state'],'checking')
        self.fail=False
        with self.repo.connection() as db:db.execute('UPDATE billing_subscription_charges SET next_attempt_at=?',(accounts.now(),))
        self.assertTrue(subscriptions.process_one(self.repo,self.settings))
        posts=[r for r in self.requests if r[0]=='POST'];self.assertEqual(posts[0][3],posts[1][3])
        other_key=self.body();self.assertEqual(self.client.post('/api/billing/subscriptions',json=other_key).status_code,409)
        with self.repo.connection() as db:
            db.execute('UPDATE payment_orders SET payment_id=NULL,created_at=?',((datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(),))
        count=len(self.requests);r=self.client.get('/api/billing/orders/'+order['id']);self.assertEqual(r.json()['state'],'review');self.assertEqual(len(self.requests),count)

    def test_missing_saved_permission_grants_paid_service_but_disables_renewal(self):
        self.saved=False;order,_=self.start();self.succeed(order)
        s=self.wallet()['subscription']['current'];self.assertFalse(s['auto_renew']);self.assertEqual(s['error_code'],'payment_method_not_saved')
        self.assertEqual(self.wallet()['balance'],1)

    def test_unlimited_receipt_period_and_access_do_not_spend_accumulated_balance(self):
        with self.repo.connection() as db:db.execute('UPDATE users SET bonus_analyses=7 WHERE id=?',(self.user,))
        order,_=self.start('daily_unlimited');self.succeed(order)
        item=self.requests[0][2]['receipt']['items'][0]
        self.assertEqual((item['quantity'],item['amount']['value'],item['payment_mode']),('1.00','6090.00','full_payment'))
        self.assertTrue(self.wallet()['subscription']['unlimited_active'])
        payload=self.save();response=self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={})
        self.assertEqual(response.json()['charged'],0);self.assertEqual(self.wallet()['balance'],7)
        self.due(order);self.assertFalse(self.wallet()['subscription']['unlimited_active'])
        again=self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={});self.assertEqual(again.json()['charged'],0)
        other=self.save();self.assertEqual(self.client.post('/api/billing/analyses/'+other['id']+'/unlock',json={}).json()['charged'],1)
        self.assertEqual(self.wallet()['balance'],6)

    def test_skip_is_fully_readable_and_unlockable_without_balance_or_subscription(self):
        payload=self.save('skip');protected=billing.protect(self.repo,self.settings,payload,{'id':self.user})
        self.assertNotIn('locked',protected)
        values=list(ThreadPoolExecutor(max_workers=5).map(lambda _:billing.unlock(self.repo,self.settings,{'id':self.user},payload['id']),range(8)))
        self.assertTrue(all(v['charged']==0 for v in values));self.assertEqual(self.wallet()['balance'],0)
        with self.repo.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM analysis_credit_ledger').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM billing_receipt_jobs').fetchone()[0],0)
        bet=self.save();self.assertEqual(self.client.post('/api/billing/analyses/'+bet['id']+'/unlock',json={}).status_code,402)

    def test_owner_origin_and_forged_provider_amount_are_enforced(self):
        order,_=self.start()
        self.assertEqual(self.client.post('/api/billing/subscriptions/'+order['subscription_id']+'/cancel',json={},headers={'Origin':'https://attacker.example'}).status_code,403)
        bad=deepcopy(self.payments['payment-'+order['id']]);bad.update(status='succeeded',paid=True);bad['amount']['value']='1.00'
        from matchlab.yookassa import ProviderMismatch
        with self.assertRaises(ProviderMismatch):billing.apply_payment(self.repo,self.settings,bad)
        self.assertEqual(self.wallet()['balance'],0)
        other=TestClient(self.client.app,base_url=self.settings.public_origin);signed_in(other,self.repo,username='second-user')
        other.cookies.set('__Host-prognozich',other.cookies.get('prognozich_session'))
        self.assertEqual(other.post('/api/billing/subscriptions/'+order['subscription_id']+'/cancel',json={}).status_code,404)

    def test_finite_and_unlimited_refunds_stop_renewal_without_future_credit_debt(self):
        order,_=self.start('daily_five');self.succeed(order)
        refund={'id':'refund-'+uuid4().hex,'status':'succeeded','payment_id':'payment-'+order['id'],
            'amount':{'currency':'RUB','value':'2190.00'}}
        for _ in range(2):billing.apply_refund(self.repo,self.settings,refund)
        self.assertEqual(self.wallet()['balance'],0);self.assertFalse(self.wallet()['subscription']['current']['auto_renew'])
        unlimited,_=self.start('daily_unlimited');self.succeed(unlimited)
        refund.update(id='refund-'+uuid4().hex,payment_id='payment-'+unlimited['id'],amount={'currency':'RUB','value':'6090.00'})
        billing.apply_refund(self.repo,self.settings,refund)
        self.assertFalse(self.wallet()['subscription']['unlimited_active'])

    def test_pausing_sales_reconciles_sent_payment_but_cannot_issue_new_renewal(self):
        order,_=self.start();self.payments['payment-'+order['id']].update(status='succeeded',paid=True)
        paused=replace(self.settings,billing_recurring_enabled=False)
        subscriptions.reconcile(self.repo,paused,dict(self.order_row(order)))
        self.assertEqual(self.wallet()['balance'],1);self.due(order)
        self.assertFalse(subscriptions.process_one(self.repo,paused))

    def test_concurrent_renewals_cannot_charge_the_same_period_twice(self):
        order,_=self.start();self.succeed(order);self.due(order);self.next_paid=True
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _:subscriptions.process_one(self.repo,self.settings),range(8)))
        self.assertEqual(self.wallet()['balance'],2)
        self.assertEqual(sum(r[0]=='POST' for r in self.requests),2)
        with self.repo.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM billing_subscription_periods').fetchone()[0],2)

    def test_declined_renewal_stops_without_debt_or_new_payment_retries(self):
        order,_=self.start();self.succeed(order);self.due(order)
        self.assertTrue(subscriptions.process_one(self.repo,self.settings))
        latest=[r for r in self.requests if r[0]=='POST'][-1][3]
        self.payments['payment-'+latest].update(status='canceled',paid=False)
        self.client.get('/api/billing/orders/'+latest)
        self.assertEqual(self.wallet()['balance'],1)
        self.assertFalse(self.wallet()['subscription']['current']['auto_renew'])
        self.assertFalse(subscriptions.process_one(self.repo,self.settings))

    def test_unlimited_access_receipt_job_is_checked_without_dividing_by_zero(self):
        order,_=self.start('daily_unlimited');self.succeed(order)
        self.assertTrue(billing_fiscal.process_one(self.repo,self.settings))
        with self.repo.connection() as db:
            job=db.execute('SELECT * FROM billing_receipt_jobs WHERE order_id=?',(order['id'],)).fetchone()
            self.assertEqual((job['kind'],job['state']),('subscription','succeeded'))
        report=self.client.get('/api/admin/billing').status_code
        self.assertEqual(report,403)
        with self.repo.connection() as db:db.execute("UPDATE users SET role='admin' WHERE id=?",(self.user,))
        detail=self.client.get('/api/admin/billing/orders/'+order['id']).json()
        self.assertEqual(detail['refundable_units'],0);self.assertIn('6090 ₽',detail['subscription']['consent_text'])
        self.assertNotIn('saved-card',json.dumps(detail))

    def test_unlimited_bypasses_daily_request_cap_and_expiration_restores_it(self):
        order,_=self.start('daily_unlimited');self.succeed(order)
        self.assertEqual(self.client.get('/api/account/usage').json()['daily_limit'],0)
        with self.repo.connection() as db:
            db.execute("INSERT INTO ai_usage(id,user_id,at,kind,target,status) VALUES (?,?,?,'event','fixture','success')",
                       (uuid4().hex,self.user,accounts.now()))
        usage=accounts.start_usage(self.repo,{'id':self.user},'event','fixture',self.settings)
        self.assertTrue(usage);self.due(order)
        self.assertEqual(self.client.get('/api/account/usage').json()['daily_limit'],1)
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as raised:accounts.start_usage(self.repo,{'id':self.user},'event','fixture',self.settings)
        self.assertEqual(raised.exception.status_code,429)

    def test_cancel_or_operator_pause_never_replays_an_unknown_post(self):
        for canceled in (True,False):
            self.fail=True;order,_=self.start();self.fail=False
            if canceled:self.client.post('/api/billing/subscriptions/'+order['subscription_id']+'/cancel',json={})
            paused=self.settings if canceled else replace(self.settings,billing_recurring_enabled=False)
            with self.repo.connection() as db:db.execute('UPDATE billing_subscription_charges SET next_attempt_at=?',(accounts.now(),))
            before=len(self.requests)
            result=subscriptions.reconcile(self.repo,paused,self.order_row(order))
            self.assertEqual(result['state'],'review');self.assertEqual(len(self.requests),before)
            # Isolate the next independent fixture without making any payment.
            with self.repo.connection() as db:
                db.execute("UPDATE payment_orders SET state='canceled' WHERE id=?",(order['id'],))
                db.execute("UPDATE billing_subscriptions SET state='canceled' WHERE id=?",(order['subscription_id'],))

    def test_disabled_account_cannot_replay_an_unknown_charge(self):
        self.fail=True;order,_=self.start();self.fail=False
        with self.repo.connection() as db:
            db.execute('UPDATE users SET enabled=0 WHERE id=?',(self.user,))
            db.execute('UPDATE billing_subscription_charges SET next_attempt_at=?',(accounts.now(),))
        before=len(self.requests);result=subscriptions.reconcile(self.repo,self.settings,self.order_row(order))
        self.assertEqual(result['state'],'review');self.assertEqual(len(self.requests),before)

    def order_row(self,order):
        with self.repo.connection() as db:return dict(db.execute('SELECT * FROM payment_orders WHERE id=?',(order['id'],)).fetchone())

if __name__=='__main__':unittest.main()
