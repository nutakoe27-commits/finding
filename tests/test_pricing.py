"""Published tariffs, first-payment promotions and immutable customer contracts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from account_helpers import signed_in
import test_subscriptions as fixture
from matchlab import accounts,billing,pricing,subscriptions


class PricingTests(unittest.TestCase):
    setUp=fixture.SubscriptionTests.setUp
    provider=fixture.SubscriptionTests.provider
    wallet=fixture.SubscriptionTests.wallet
    succeed=fixture.SubscriptionTests.succeed
    due=fixture.SubscriptionTests.due
    save=fixture.SubscriptionTests.save

    def admin(self,elevate=True):
        with self.repo.connection() as db:db.execute("UPDATE users SET role='admin' WHERE id=?",(self.user,))
        if elevate:
            response=self.client.post('/api/admin/reauth',json={'password':'fixture-password-long-enough'})
            self.assertEqual(response.status_code,200,response.text)

    def edit(self):
        self.admin()
        current=self.client.get('/api/admin/pricing').json()['current']
        return {'base_revision':current['revision'],'draft':pricing.draft_of(current)}

    def publish(self,body):
        preview=self.client.post('/api/admin/pricing/preview',json=body)
        self.assertEqual(preview.status_code,200,preview.text)
        body=deepcopy(body)|{'preview_hash':preview.json()['preview_hash']}
        result=self.client.post('/api/admin/pricing/publish',json=body)
        self.assertEqual(result.status_code,200,result.text)
        return result.json()['current']

    def start(self,plan):
        config=self.client.get('/api/billing/config').json()
        body={'plan_id':plan['id'],'request_key':uuid4().hex,'offer_accepted':True,'offer_version':config['offer_version'],
            'recurring_accepted':True,'recurring_version':config['recurring_version'],'receipt_email':'buyer@example.ru',
            'credits':9999,'amount_kopecks':1}
        response=self.client.post('/api/billing/subscriptions',json=body)
        self.assertEqual(response.status_code,200,response.text)
        return response.json(),body

    def test_roles_csrf_and_recent_password_are_required_to_publish(self):
        self.assertEqual(self.client.get('/api/admin/pricing').status_code,403)
        self.admin(False);body=self.edit();self.client.post('/api/admin/reauth',json={'password':'fixture-password-long-enough'})
        with self.repo.connection() as db:db.execute('UPDATE user_sessions SET elevated_at=NULL')
        token=self.client.post('/api/admin/pricing/preview',json=body).json()['preview_hash']
        self.assertEqual(self.client.post('/api/admin/pricing/publish',json=body|{'preview_hash':token}).status_code,403)
        self.assertEqual(self.client.post('/api/admin/pricing/draft',json=body,headers={'Origin':'https://attacker.example'}).status_code,403)
        self.assertEqual(pricing.load(self.repo)['revision'],'builtin')
        self.assertEqual(self.requests,[])

    def test_draft_and_preview_do_not_change_public_prices_or_invoke_the_provider(self):
        body=self.edit();body['draft']['subscriptions'][0].update(credits=7,amount_kopecks=79900,period_days=7)
        body['draft']['subscriptions'][0]['discount']['percent']=25
        before=self.client.get('/api/billing/config').json()
        self.assertEqual(self.client.post('/api/admin/pricing/draft',json=body).status_code,200)
        saved=self.client.get('/api/admin/pricing').json()['draft'];self.assertEqual(saved['draft'],body['draft'])
        response=self.client.post('/api/admin/pricing/preview',json=body);self.assertEqual(response.status_code,200,response.text)
        preview=response.json();self.assertEqual(preview['subscriptions'][0]['amount_kopecks'],59925)
        self.assertIn('599,25 ₽',preview['documents']['offer']);self.assertIn('Предпросмотр',preview['documents']['pricing'])
        self.assertEqual(self.client.get('/api/billing/config').json(),before)
        self.assertNotIn('599,25 ₽',self.client.get('/static/legal/offer.html').text)
        self.assertEqual(self.requests,[])

    def test_publish_updates_all_documents_checkout_and_archives_an_immutable_revision(self):
        body=self.edit();p=body['draft']['subscriptions'][0];p.update(credits=7,amount_kopecks=79900,period_days=7);p['discount']['percent']=25
        current=self.publish(body);config=self.client.get('/api/billing/config').json()
        plan=config['subscription_plans'][0]
        self.assertEqual((plan['credits'],plan['amount_kopecks'],plan['renewal_amount_kopecks'],plan['period_hours']),(7,59925,79900,168))
        self.assertIn('599,25 ₽ сейчас, затем 799 ₽',plan['consent_text'])
        for doc in ('pricing','offer','recurring','payment-refund'):
            html=self.client.get('/static/legal/'+doc+'.html').text
            self.assertIn('599,25 ₽',html);self.assertIn('799 ₽',html);self.assertIn('7 разборов / 7 дней',html);self.assertIn(current['revision'],html)
        path='/legal/versions/'+current['revision']+'/offer'
        old=self.client.get(path);self.assertEqual(old.status_code,200);self.assertEqual(old.headers['x-robots-tag'],'noindex')
        next_body=self.edit();next_body['draft']['subscriptions'][0]['amount_kopecks']=99900;self.publish(next_body)
        self.assertEqual(self.client.get(path).text,old.text)
        self.assertEqual(self.client.get('/static/legal/offer.html?document=pricing').text,self.client.get('/static/legal/offer.html').text)
        with self.repo.connection() as db:self.assertEqual(db.execute("SELECT count(*) FROM admin_audit WHERE action='pricing.published'").fetchone()[0],2)
        self.assertEqual(self.requests,[])

    def test_only_first_payment_is_discounted_and_old_contract_survives_later_edits(self):
        body=self.edit();p=body['draft']['subscriptions'][0];p.update(credits=7,amount_kopecks=79900,period_days=7);p['discount']['percent']=25
        first=self.publish(body);plan=self.client.get('/api/billing/config').json()['subscription_plans'][0]
        order,_=self.start(plan)
        self.assertEqual(self.requests[-1][2]['amount']['value'],'599.25')
        # Publication while the first payment is pending cannot rewrite it.
        body=self.edit();body['draft']['subscriptions'][0].update(credits=12,amount_kopecks=129900,period_days=30);self.publish(body)
        self.succeed(order);self.assertEqual(self.wallet()['balance'],7)
        current=self.wallet()['subscription']['current']
        self.assertEqual((current['amount_kopecks'],current['period_hours']),(79900,168))
        self.assertIn(first['revision'],current['recurring_url'])
        self.due(order);self.next_paid=True;self.assertTrue(subscriptions.process_one(self.repo,self.settings))
        request=[r for r in self.requests if r[0]=='POST'][-1][2]
        self.assertEqual(request['amount']['value'],'799.00');self.assertEqual(self.wallet()['balance'],14)
        with self.repo.connection() as db:
            row=db.execute('SELECT starts_at,ends_at FROM billing_subscription_periods ORDER BY period_number DESC LIMIT 1').fetchone()
        self.assertEqual(datetime.fromisoformat(row['ends_at'])-datetime.fromisoformat(row['starts_at']),timedelta(days=7))

    def test_promotion_expiry_or_new_publication_rejects_a_stale_checkout_before_gateway_calls(self):
        body=self.edit();end=datetime.now(timezone.utc)+timedelta(hours=1)
        p=body['draft']['subscriptions'][0];p['discount'].update(percent=20,ends_at=end.isoformat())
        self.publish(body);config=self.client.get('/api/billing/config').json();plan=config['subscription_plans'][0]
        request={'plan_id':plan['id'],'request_key':uuid4().hex,'offer_accepted':True,'recurring_accepted':True,
                 'offer_version':config['offer_version'],'recurring_version':config['recurring_version'],'receipt_email':'buyer@example.ru'}
        with patch('matchlab.pricing.datetime',wraps=datetime) as clock:
            clock.now.return_value=end+timedelta(seconds=1)
            after=self.client.get('/api/billing/config').json()['subscription_plans'][0]
            self.assertEqual(after['amount_kopecks'],49000);self.assertNotEqual(after['id'],plan['id'])
            self.assertEqual(self.client.post('/api/billing/subscriptions',json=request).status_code,422)
        self.publish(self.edit())
        self.assertEqual(self.client.post('/api/billing/subscriptions',json=request).status_code,422)
        self.assertEqual(self.requests,[])

    def test_once_packages_discount_hiding_and_server_fixed_price(self):
        body=self.edit();body['draft']['packages'][0].update(credits=4,amount_kopecks=69000);body['draft']['packages'][0]['discount']['percent']=10
        body['draft']['subscriptions'][2]['enabled']=False
        self.publish(body);config=self.client.get('/api/billing/config').json();self.assertEqual(len(config['subscription_plans']),2)
        package=config['packages'][0];self.assertEqual(package['amount_kopecks'],62100)
        response=self.client.post('/api/billing/orders',json={'package_id':package['id'],'request_key':uuid4().hex,'offer_accepted':True,
            'offer_version':config['offer_version'],'receipt_email':'buyer@example.ru','amount_kopecks':1})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.requests[-1][2]['amount']['value'],'621.00')
        self.assertNotIn('Безлимит /',self.client.get('/static/legal/pricing.html').text)

    def test_discounted_unlimited_has_exact_receipt_and_renews_at_regular_price(self):
        body=self.edit();p=body['draft']['subscriptions'][2]
        p.update(amount_kopecks=99999,period_days=2);p['discount']['percent']=33
        self.publish(body);plan=self.client.get('/api/billing/config').json()['subscription_plans'][2]
        self.assertEqual(plan['amount_kopecks'],66999)
        order,_=self.start(plan)
        first=self.requests[-1][2]
        self.assertEqual(first['amount']['value'],'669.99')
        self.assertEqual(first['receipt']['items'][0]['description'],'Безлимитный доступ к ИИ-разборам на 2 дня')
        self.assertEqual(first['receipt']['items'][0]['amount']['value'],'669.99')
        self.succeed(order)
        self.assertTrue(self.wallet()['subscription']['unlimited_active']);self.assertEqual(self.wallet()['balance'],0)
        self.due(order);self.next_paid=True;subscriptions.process_one(self.repo,self.settings)
        renewal=[r for r in self.requests if r[0]=='POST'][-1][2]
        self.assertEqual(renewal['amount']['value'],'999.99')
        self.assertEqual(renewal['receipt']['items'][0]['amount']['value'],'999.99')
        self.assertEqual(self.wallet()['subscription']['current']['period_hours'],48)

    def test_old_recurring_documents_and_demo_use_the_correct_catalogue(self):
        self.assertIn('v4.html',pricing.document_url(subscriptions.VERSION,'recurring'))
        body=self.edit();body['draft']['subscriptions'][1].update(credits=18,amount_kopecks=349900,period_days=10)
        self.publish(body);before=len(self.requests)
        sample=self.client.get('/api/admin/billing/subscription-demo').json()
        self.assertEqual(sample['balance'],18)
        current=sample['subscription']['current']
        self.assertEqual((current['amount_kopecks'],current['period_hours']),(349900,240))
        self.assertEqual(len(self.requests),before)

    def test_invalid_money_period_discount_dates_and_unpreviewed_changes_never_publish(self):
        body=self.edit()
        for key,value in [('amount_kopecks',True),('amount_kopecks',1),('credits',-1),('period_days',0),('period_days',366)]:
            changed=deepcopy(body);changed['draft']['subscriptions'][0][key]=value
            self.assertEqual(self.client.post('/api/admin/pricing/preview',json=changed).status_code,422)
        for discount in ({'percent':100,'starts_at':None,'ends_at':None},{'percent':15,'starts_at':'2026-12-01T00:00:00','ends_at':None},
                         {'percent':15,'starts_at':'2026-12-02T00:00:00Z','ends_at':'2026-12-01T00:00:00Z'}):
            changed=deepcopy(body);changed['draft']['packages'][0]['discount']=discount
            self.assertEqual(self.client.post('/api/admin/pricing/preview',json=changed).status_code,422)
        response=self.client.post('/api/admin/pricing/preview',json=body);token=response.json()['preview_hash']
        changed=deepcopy(body);changed['draft']['subscriptions'][0]['amount_kopecks']=99900
        self.assertEqual(self.client.post('/api/admin/pricing/publish',json=changed|{'preview_hash':token}).status_code,422)
        self.assertEqual(pricing.load(self.repo)['revision'],'builtin');self.assertEqual(self.requests,[])

    def test_two_publishers_cannot_overwrite_each_other_and_versions_keep_their_snapshots(self):
        body=self.edit();preview=self.client.post('/api/admin/pricing/preview',json=body).json();body['preview_hash']=preview['preview_hash']
        with ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(lambda _:self.client.post('/api/admin/pricing/publish',json=body).status_code,range(2)))
        self.assertEqual(sorted(statuses),[200,409])
        history=self.client.get('/api/admin/pricing').json()['history'];self.assertEqual(len(history),1)


if __name__=='__main__':unittest.main()
