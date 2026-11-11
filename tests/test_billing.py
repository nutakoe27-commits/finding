"""Payments and paid access against a fake authenticated provider; never real charges."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from account_helpers import signed_in
from outcome_helpers import forecast,archive_result
from matchlab import billing,accounts
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from matchlab.yookassa import ProviderUnavailable,ProviderMismatch


class BillingTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=Settings(Path(temp.name),'http://127.0.0.1:9/v1','fixture',billing_enabled=True,billing_public_testing=True,yookassa_shop_id='100500',yookassa_secret_key='test_fixture')
        self.repo=Repository(self.settings.database);self.repo.initialize()
        self.client=TestClient(create_app(self.settings));self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.user=signed_in(self.client,self.repo);self.payments={};self.creates=[]
        self.create_patch=patch('matchlab.billing.YooKassa.create',side_effect=self.create_payment).start()
        self.payment_patch=patch('matchlab.billing.YooKassa.payment',side_effect=lambda pid:deepcopy(self.payments[pid])).start()
        self.addCleanup(patch.stopall)

    def create_payment(self,order,origin):
        self.creates.append(order['id']);pid='payment-'+order['id']
        value={'id':pid,'status':'pending','paid':False,'test':True,'recipient':{'account_id':'100500'},
            'amount':{'currency':'RUB','value':f"{order['amount_kopecks']/100:.2f}"},
            'metadata':{'order_id':order['id'],'user_id':order['user_id']},
            'confirmation':{'confirmation_url':'https://yoomoney.ru/checkout/payments/v2/contract?orderId='+pid}}
        self.payments[pid]=value;return deepcopy(value)

    def order(self,**extra):
        body={'package_id':'one','request_key':uuid4().hex,'offer_accepted':True,'offer_version':billing.OFFER_VERSION,**extra}
        response=self.client.post('/api/billing/orders',json=body)
        self.assertEqual(response.status_code,200,response.text);return response.json(),body

    def succeed(self,order):
        pid='payment-'+order['id'];self.payments[pid].update(status='succeeded',paid=True)
        response=self.client.get('/api/billing/orders/'+order['id'])
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def save(self,*,owner=None,**kw):
        kw.setdefault('kickoff',datetime.now(timezone.utc)+timedelta(minutes=30));kw.setdefault('at',datetime.now(timezone.utc)-timedelta(seconds=20));kw.setdefault('score',None)
        payload=forecast(self.repo,uuid4().hex,**kw)
        payload['context']['notes']='SECRET_PRIVATE_NOTE';payload['context']['neural_verdict']['reason']['text']='SECRET_WINNER_REASON'
        if owner=='__system_featured__':payload['context']['analysis_origin']='system'
        with self.repo.connection() as db:db.execute('UPDATE event_analyses SET payload=?,user_id=? WHERE id=?',(json.dumps(payload),owner or self.user,payload['id']))
        return payload

    def grant(self,amount=1):
        with self.repo.connection() as db:db.execute('INSERT INTO analysis_credit_ledger VALUES (?,?,?,?,?,?,?)',(uuid4().hex,self.user,billing.MODE,amount,'fixture',uuid4().hex,accounts.now()))

    def available(self):return self.client.get('/api/billing/wallet').json()['balance']

    def test_catalogue_is_fixed_and_never_reveals_credentials(self):
        data=self.client.get('/api/billing/config').json()
        self.assertEqual([(p['credits'],p['amount_kopecks']) for p in data['packages']],[(1,49000),(5,219000),(10,409000)])
        self.assertTrue(data['test_mode']);self.assertNotIn('test_fixture',json.dumps(data));self.assertNotIn('100500',json.dumps(data))

    def test_server_price_overrides_any_client_amount_and_pending_does_not_credit(self):
        order,_=self.order(amount_kopecks=1,credits=1000000)
        self.assertEqual((order['credits'],order['amount_kopecks']),(1,49000));self.assertEqual(self.available(),0)

    def test_only_verified_success_credits_and_repeat_polling_never_credits_twice(self):
        order,body=self.order(package_id='five');self.succeed(order)
        for _ in range(3):
            self.client.get('/api/billing/orders/'+order['id']);self.client.post('/api/billing/orders',json=body)
        self.assertEqual(self.available(),5);self.assertEqual(len(self.creates),1)
        with self.repo.connection() as db:self.assertEqual(db.execute("SELECT count(*) FROM analysis_credit_ledger WHERE kind='payment'").fetchone()[0],1)

    def test_same_client_key_returns_same_order_and_cannot_change_package(self):
        first,body=self.order()
        second=self.client.post('/api/billing/orders',json=body).json();self.assertEqual(first['id'],second['id'])
        response=self.client.post('/api/billing/orders',json={**body,'package_id':'ten'})
        self.assertEqual(response.status_code,409);self.assertEqual(len(self.creates),1)

    def test_timeout_keeps_durable_order_and_same_provider_idempotence_key(self):
        with patch('matchlab.billing.YooKassa.create',side_effect=ProviderUnavailable):order,body=self.order()
        self.assertEqual(order['state'],'checking');self.assertEqual(self.available(),0)
        retried=self.client.post('/api/billing/orders',json=body).json()
        self.assertEqual(order['id'],retried['id']);self.assertEqual(self.creates,[order['id']])

    def test_definitive_rejection_is_not_misrepresented_as_a_payment_in_progress(self):
        with patch('matchlab.billing.YooKassa.create',side_effect=ProviderUnavailable(definitive=True)):order,body=self.order()
        self.assertEqual(order['state'],'failed');self.assertEqual(self.available(),0)
        self.assertEqual(self.client.get('/api/billing/orders/'+order['id']).json()['state'],'failed')

    def test_canceled_payment_never_credits(self):
        order,_=self.order();self.payments['payment-'+order['id']]['status']='canceled'
        self.assertEqual(self.client.get('/api/billing/orders/'+order['id']).json()['state'],'canceled');self.assertEqual(self.available(),0)

    def test_false_webhook_status_is_not_trusted_and_genuine_duplicates_are_safe(self):
        order,_=self.order();pid='payment-'+order['id']
        event={'event':'payment.succeeded','object':{'id':pid,'status':'succeeded','metadata':{'order_id':order['id']}}}
        self.client.post('/api/billing/yookassa/webhook',json=event);self.assertEqual(self.available(),0)
        self.payments[pid].update(status='succeeded',paid=True)
        for _ in range(3):self.assertEqual(self.client.post('/api/billing/yookassa/webhook',json=event).status_code,200)
        self.assertEqual(self.available(),1)

    def test_unknown_notifications_do_not_trigger_provider_requests(self):
        with patch('matchlab.billing.YooKassa.payment',side_effect=AssertionError('provider called')):
            response=self.client.post('/api/billing/yookassa/webhook',json={'event':'payment.succeeded','object':{'id':'unknown-payment-1234'}})
        self.assertEqual(response.status_code,200)

    def test_wrong_currency_amount_shop_or_live_payment_never_credits(self):
        for change in ({'test':False},{'recipient':{'account_id':'other'}},{'amount':{'currency':'USD','value':'490.00'}},{'amount':{'currency':'RUB','value':'4.90'}},{'paid':False}):
            with self.subTest(change=change):
                order,_=self.order();pid='payment-'+order['id'];self.payments[pid].update(status='succeeded',paid=True,**change) if 'paid' not in change else self.payments[pid].update(status='succeeded',**change)
                self.assertEqual(self.client.get('/api/billing/orders/'+order['id']).status_code,502)
                self.assertEqual(self.available(),0)

    def test_terms_csrf_and_order_ownership(self):
        self.assertEqual(self.client.post('/api/billing/orders',json={'package_id':'one'}).status_code,422)
        order,_=self.order()
        signed_in(self.client,self.repo,'other_user')
        self.assertEqual(self.client.get('/api/billing/orders/'+order['id']).status_code,404)
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.client.post('/api/billing/orders',json={}).status_code,403)

    def test_private_answer_history_and_job_payload_are_locked_without_raw_context(self):
        payload=self.save()
        for url in ('/api/analyses/'+payload['id'],'/api/analyses'):
            response=self.client.get(url);self.assertEqual(response.status_code,200)
            self.assertNotIn('SECRET_',response.text);self.assertNotIn('comparison',response.text)
        locked=self.client.get('/api/analyses/'+payload['id']).json()
        self.assertTrue(locked['locked']);self.assertNotIn('context',locked);self.assertNotIn('event',locked)
        with self.repo.connection() as db:
            db.execute("INSERT INTO ai_analysis_jobs(id,user_id,request_key,event_id,source,state,created_at,usage_id,analysis_id) VALUES (?,?,?,?,?,'completed',?,?,?)",('job',self.user,'key','123','archive',accounts.now(),'none',payload['id']))
        response=self.client.get('/api/analysis-jobs/job');self.assertTrue(response.json()['result']['locked']);self.assertNotIn('SECRET_',response.text)

    def test_skip_reveals_only_the_free_phrase_and_unlock_is_optional(self):
        payload=self.save(decision='skip')
        response=self.client.get('/api/analyses/'+payload['id'])
        self.assertEqual(response.json()['text'],'Рекомендуем пропустить матч');self.assertNotIn('SECRET_',response.text)
        self.assertNotIn('preference',response.text);self.assertEqual(self.available(),0)

    def test_atomic_unlock_costs_one_and_repeated_open_is_free(self):
        payload=self.save();self.grant(2)
        first=self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={}).json()
        again=self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={}).json()
        self.assertEqual((first['charged'],again['charged'],self.available()),(1,0,1))
        opened=self.client.get('/api/analyses/'+payload['id']);self.assertNotIn('locked',opened.json());self.assertIn('SECRET_',opened.text)
        self.assertEqual(first['route'],'#analysis/'+payload['id'])

    def test_zero_balance_other_owner_and_model_errors_never_spend(self):
        payload=self.save();self.assertEqual(self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={}).status_code,402)
        self.grant(2);failed=self.save(engine='model_error')
        self.assertEqual(self.client.post('/api/billing/analyses/'+failed['id']+'/unlock',json={}).status_code,409)
        other=self.save(owner='other_user')
        self.assertEqual(self.client.post('/api/billing/analyses/'+other['id']+'/unlock',json={}).status_code,404)
        self.assertEqual(self.available(),2)

    def test_concurrent_unlocks_of_same_answer_are_idempotent(self):
        payload=self.save();self.grant()
        user={'id':self.user,'role':'user'}
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _:billing.unlock(self.repo,self.settings,user,payload['id']),range(12)))
        self.assertEqual(sum(r['charged'] for r in results),1);self.assertEqual(self.available(),0)

    def test_concurrent_notifications_and_poll_results_credit_only_once(self):
        order,_=self.order(package_id='five');value=self.payments['payment-'+order['id']];value.update(status='succeeded',paid=True)
        with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(lambda _:billing.apply_payment(self.repo,self.settings,deepcopy(value)),range(12)))
        self.assertEqual(self.available(),5)

    def test_two_different_answers_cannot_overspend_one_credit(self):
        ids=[self.save()['id'],self.save()['id']];self.grant()
        def spend(identifier):
            try:return billing.unlock(self.repo,self.settings,{'id':self.user},identifier)['charged']
            except HTTPException as error:return error.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(spend,ids))
        self.assertEqual(sorted(results),[1,402]);self.assertEqual(self.available(),0)

    def test_system_forecast_is_locked_until_unlocked_by_this_account(self):
        payload=self.save(owner='__system_featured__')
        previews=self.client.get('/api/picks/today').json()['picks']
        self.assertNotIn('event',next(p for p in previews if p['id']==payload['id']))
        response=self.client.get('/api/results/'+payload['id']);self.assertTrue(response.json()['locked']);self.assertNotIn('SECRET_',response.text)
        self.grant();self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={})
        self.assertNotIn('locked',self.client.get('/api/results/'+payload['id']).json())
        signed_in(self.client,self.repo,'second_reader')
        self.assertTrue(self.client.get('/api/results/'+payload['id']).json()['locked'])

    def test_bonus_units_can_unlock_but_generation_does_not_consume_them(self):
        with self.repo.connection() as db:db.execute('UPDATE users SET bonus_analyses=1 WHERE id=?',(self.user,))
        user={'id':self.user,'role':'user'};usage=accounts.start_usage(self.repo,user,'event','target',self.settings)
        self.assertEqual(self.available(),1);accounts.finish_usage(self.repo,usage,'success',10)
        payload=self.save();self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={})
        self.assertEqual(self.available(),0)

    def test_verified_refund_removes_credits_exactly_once(self):
        order,_=self.order(package_id='five');self.succeed(order)
        refund={'id':'refund-'+uuid4().hex,'payment_id':'payment-'+order['id'],'status':'succeeded','amount':{'value':'2190.00','currency':'RUB'}}
        for _ in range(2):billing.apply_refund(self.repo,self.settings,refund)
        self.assertEqual(self.available(),0)
        self.assertEqual(self.client.get('/api/billing/wallet').json()['orders'][0]['state'],'refunded')

    def test_refund_notification_uses_verified_refund_instead_of_its_body(self):
        order,_=self.order();self.succeed(order)
        refund={'id':'refund-'+uuid4().hex,'payment_id':'payment-'+order['id'],'status':'succeeded','amount':{'currency':'RUB','value':'490.00'}}
        body={'event':'refund.succeeded','object':{**refund,'amount':{'currency':'RUB','value':'999999.00'}}}
        with patch('matchlab.billing.YooKassa.refund',return_value=refund):
            for _ in range(2):self.assertEqual(self.client.post('/api/billing/yookassa/webhook',json=body).status_code,200)
        self.assertEqual(self.available(),0)

    def test_old_owner_answers_are_grandfathered_once_without_freeing_new_answers(self):
        payload=self.save()
        with self.repo.connection() as db:db.execute("DELETE FROM site_settings WHERE key='billing_test_started'")
        billing.initialize(self.repo,self.settings)
        self.assertNotIn('locked',self.client.get('/api/analyses/'+payload['id']).json())
        new=self.save();billing.initialize(self.repo,self.settings)
        self.assertTrue(self.client.get('/api/analyses/'+new['id']).json()['locked'])

    def test_paid_system_answer_remains_accessible_outside_the_public_thirty_day_window(self):
        now=datetime.now(timezone.utc)
        payload=self.save(owner='__system_featured__',kickoff=now-timedelta(days=40),at=now-timedelta(days=40,minutes=10))
        self.grant();result=self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={})
        self.assertEqual(result.status_code,200,result.text)
        response=self.client.get('/api/results/'+payload['id'])
        self.assertEqual(response.status_code,200,response.text);self.assertTrue(response.json()['purchased_archive'])
        self.assertEqual(self.client.get('/api/billing/unlocks').json()['items'][0]['id'],payload['id'])

    def test_already_public_finished_answer_is_not_charged_even_from_an_old_paywall(self):
        now=datetime.now(timezone.utc)
        payload=self.save(owner='__system_featured__',kickoff=now-timedelta(days=1),at=now-timedelta(days=1,minutes=10))
        archive_result(self.repo,payload['event'],'2:0','final')
        result=self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={})
        self.assertEqual((result.status_code,result.json()['charged'],self.available()),(200,0,0))
        self.assertEqual(self.client.get('/api/results/'+payload['id']).json()['state'],'settled')

    def test_paused_checkout_never_reopens_paid_text_and_existing_credits_still_work(self):
        from dataclasses import replace
        payload=self.save();self.grant()
        paused=replace(self.settings,billing_enabled=False)
        self.assertTrue(billing.protect(self.repo,paused,payload,{'id':self.user})['locked'])
        self.assertEqual(billing.unlock(self.repo,paused,{'id':self.user},payload['id'])['charged'],1)
        with self.assertRaises(HTTPException) as raised:billing.create_order(self.repo,paused,{'id':self.user},{})
        self.assertEqual(raised.exception.status_code,503)

    def test_direct_explain_and_real_background_job_never_return_closed_text(self):
        from test_product import event
        self.repo.import_event_markets(event('123'))
        jobs=self.client.app.state.analysis_jobs;jobs.stop()
        def generated(settings,context):
            context['ai_generation']={'state':'ready','model_calls':1};context['neural_verdict']={'engine':'qwen','decision':'skip','preference':'home','reason':{'text':'SECRET_REASON'}}
            return 'SECRET_DIRECT_RESPONSE'
        with patch('matchlab.qwen.explain_event',side_effect=generated):
            response=self.client.post('/api/bookmakers/events/123/explain',json={})
            self.assertEqual(response.status_code,200,response.text);self.assertTrue(response.json()['locked']);self.assertNotIn('SECRET_',response.text)
            job=self.client.post('/api/bookmakers/events/123/analysis-jobs',json={})
            self.assertEqual(job.status_code,202,job.text);jobs.run_one()
        response=self.client.get('/api/analysis-jobs/'+job.json()['id'])
        self.assertTrue(response.json()['result']['locked']);self.assertNotIn('SECRET_',response.text)

    def test_refund_after_spending_has_an_auditable_balance_adjustment(self):
        order,_=self.order();self.succeed(order);payload=self.save()
        self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={})
        refund={'id':'refund-'+uuid4().hex,'payment_id':'payment-'+order['id'],'status':'succeeded','amount':{'currency':'RUB','value':'490.00'}}
        billing.apply_refund(self.repo,self.settings,refund)
        wallet=self.client.get('/api/billing/wallet').json();self.assertEqual((wallet['balance'],wallet['adjustment_due']),(0,1))

    def test_unlock_history_is_owner_scoped_and_paginated(self):
        self.grant(2)
        for _ in range(2):
            payload=self.save();self.client.post('/api/billing/analyses/'+payload['id']+'/unlock',json={})
        first=self.client.get('/api/billing/unlocks?limit=1').json();second=self.client.get('/api/billing/unlocks?offset=1&limit=1').json()
        self.assertEqual(first['total'],2);self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        signed_in(self.client,self.repo,'fresh_reader');self.assertEqual(self.client.get('/api/billing/unlocks').json()['total'],0)
        self.assertEqual(self.client.get('/api/billing/unlocks?offset=-1').status_code,422)
