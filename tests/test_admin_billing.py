"""Payment report arithmetic, privacy and read-only operator drilldowns."""
from datetime import datetime,timedelta,timezone
import unittest
from uuid import uuid4

import test_billing as fixture
from matchlab import billing


class AdminBillingTests(unittest.TestCase):
    setUp=fixture.BillingTests.setUp
    create_payment=fixture.BillingTests.create_payment
    order=fixture.BillingTests.order
    succeed=fixture.BillingTests.succeed

    def admin(self):
        with self.repo.connection() as db:db.execute("UPDATE users SET role='admin' WHERE id=?",(self.user,))

    def seed(self):
        one,_=self.order();self.succeed(one)
        five,_=self.order(package_id='five');self.succeed(five)
        pending,_=self.order(package_id='ten')
        canceled,_=self.order();self.payments['payment-'+canceled['id']].update(status='canceled',paid=False)
        self.client.get('/api/billing/orders/'+canceled['id'])
        billing.apply_refund(self.repo,self.settings,{'id':'refund-'+uuid4().hex,'payment_id':'payment-'+five['id'],'status':'succeeded','amount':{'currency':'RUB','value':'438.00'}})
        return one,five,pending,canceled

    def test_only_admin_can_read_reports_or_order_details(self):
        one,*_=self.seed()
        for path in ('/api/admin/billing','/api/admin/billing/orders/'+one['id']):
            response=self.client.get(path);self.assertEqual(response.status_code,403)
            self.assertNotIn(one['id'],response.text)
        self.admin();response=self.client.get('/api/admin/billing')
        self.assertEqual(response.status_code,200);self.assertEqual(response.headers['cache-control'],'no-store')
        for private in ('test_fixture','confirmation_url','password_hash','session_hash','SECRET_PRIVATE_NOTE'):self.assertNotIn(private,response.text)

    def test_amounts_refunds_packages_and_modes_are_not_double_counted(self):
        self.seed();self.admin()
        data=self.client.get('/api/admin/billing').json();m=data['metrics']
        self.assertEqual((m['orders'],m['paid'],m['buyers'],m['pending'],m['canceled']),(4,2,1,1,1))
        self.assertEqual((m['gross_kopecks'],m['refunded_kopecks'],m['net_kopecks'],m['average_check_kopecks']),(268000,43800,224200,134000))
        self.assertEqual(m['credited_units'],6);self.assertEqual(m['payment_rate'],.5)
        self.assertEqual(sum(d['gross_kopecks'] for d in data['daily']),268000)
        self.assertEqual(sum(d['refunded_kopecks'] for d in data['packages']),43800)
        live=self.client.get('/api/admin/billing?mode=live').json()
        self.assertEqual(live['metrics']['gross_kopecks'],0);self.assertEqual(live['total'],0)
        self.assertIn('Тестовые',data['notice'])

    def test_filters_pagination_and_old_orders(self):
        one,*_=self.seed();self.admin()
        self.assertEqual(self.client.get('/api/admin/billing?state=succeeded&limit=1').json()['total'],2)
        self.assertEqual(len(self.client.get('/api/admin/billing?limit=1&offset=1').json()['orders']),1)
        self.assertEqual(self.client.get('/api/admin/billing?query='+one['id']).json()['total'],1)
        self.assertEqual(self.client.get('/api/admin/billing?query=%27%20OR%201%3D1--').json()['total'],0)
        with self.repo.connection() as db:db.execute('UPDATE payment_orders SET created_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(days=400)).isoformat(),one['id']))
        self.assertEqual(self.client.get('/api/admin/billing').json()['total'],3)
        self.assertEqual(self.client.get('/api/admin/billing?days=0').json()['total'],4)
        for query in ('mode=all','days=-1','offset=-1','limit=101','state=unknown'):self.assertEqual(self.client.get('/api/admin/billing?'+query).status_code,422)

    def test_detail_shows_refunds_and_account_ledger_without_provider_calls(self):
        _,five,*_=self.seed();self.admin();before=len(self.creates)
        response=self.client.get('/api/admin/billing/orders/'+five['id']+'?limit=1')
        self.assertEqual(response.status_code,200,response.text);data=response.json()
        self.assertEqual(data['order']['refunded_kopecks'],43800);self.assertEqual(len(data['refunds']),1)
        self.assertEqual(data['wallet']['balance'],5);self.assertEqual(data['ledger_total'],3);self.assertEqual(len(data['ledger']),1)
        self.assertEqual(len(self.creates),before)
        self.assertEqual(self.client.get('/api/admin/billing/orders/'+'f'*32).status_code,404)


if __name__=='__main__':unittest.main()
