"""Loopback-only browser fixture. No real YooKassa or model calls are made."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi import Request
from matchlab import accounts,billing,subscriptions
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from outcome_helpers import forecast

LIVE=os.getenv('LIVE_BILLING_FIXTURE')=='true'
settings=Settings(Path(os.environ['MATCHLAB_DATA_DIR']),'','',billing_enabled=True,billing_public_testing=not LIVE,
    billing_mode='live' if LIVE else 'test',billing_receipts_enabled=LIVE,
    yookassa_shop_id='100500',yookassa_secret_key='live_browser' if LIVE else 'test_browser',public_origin=os.environ['PUBLIC_ORIGIN'],
    billing_recurring_enabled=os.getenv('SUBSCRIPTION_BROWSER_FIXTURE')=='true')
subscriptions.Worker.start=lambda self:None
if LIVE:
    from matchlab.billing_fiscal import Worker
    Worker.start=lambda self:None
repo=Repository(settings.database);repo.initialize()


class FakeProvider:
    def __init__(self,settings):pass
    def create(self,order,origin):
        pid='payment-'+order['id']
        with repo.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS browser_payments (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            old=db.execute('SELECT payload FROM browser_payments WHERE id=?',(pid,)).fetchone()
            if old:return json.loads(old[0])
            value={'id':pid,'status':'pending','paid':False,'test':not LIVE,'recipient':{'account_id':'100500'},
                'metadata':{'order_id':order['id'],'user_id':order['user_id']},
                'amount':{'currency':'RUB','value':f"{order['amount_kopecks']/100:.2f}"},
                'confirmation':{'confirmation_url':'https://yoomoney.ru/checkout/payments/v2/contract?orderId='+order['id']}}
            db.execute('INSERT INTO browser_payments VALUES (?,?)',(pid,json.dumps(value)))
            return value
    def payment(self,identifier):
        with repo.connection() as db:row=db.execute('SELECT payload FROM browser_payments WHERE id=?',(identifier,)).fetchone()
        if not row:raise RuntimeError('unknown fake payment')
        return json.loads(row[0])
    def create_subscription(self,order,origin,payment_method_id=None):
        value=self.create(order,origin)
        value['metadata']['subscription_id']=order['subscription_id']
        value['payment_method']={'id':payment_method_id or 'saved-browser-card-001','type':'bank_card','saved':True}
        with repo.connection() as db:db.execute('UPDATE browser_payments SET payload=? WHERE id=?',(json.dumps(value),value['id']))
        return value


billing.YooKassa=FakeProvider
subscriptions.YooKassa=FakeProvider
app=create_app(settings)


@app.post('/fixture/subscription-admin')
def subscription_admin(request:Request):
    if not settings.billing_recurring_enabled:raise RuntimeError('Subscription fixture only')
    user=accounts.current_user(request)
    with repo.connection() as db:db.execute("UPDATE users SET role='admin' WHERE id=?",(user['id'],))
    return {'ok':True}


@app.post('/fixture/seed')
def seed(request:Request):
    user=accounts.current_user(request);result={};now=datetime.now(timezone.utc)
    for key,decision,owner in [('system','bet','__system_featured__'),('private','bet',user['id']),('skip','skip',user['id'])]:
        event={'source_event_id':str(int(uuid4().hex[:7],16)),'sport':'football','home':'Скрытая команда А','away':'Скрытая команда Б',
            'competition':'Закрытый тестовый турнир','phase':'prematch','kickoff_at':(now+timedelta(minutes=30)).isoformat()}
        item=forecast(repo,uuid4().hex,event=event,at=now-timedelta(seconds=20),score=None,decision=decision)
        c=item['context'];c.pop('notes',None);c['neural_input']['unverified_user_notes']='';c['analysis_origin']='system' if owner=='__system_featured__' else 'human'
        v=c['neural_verdict'];v['preference_label']=event['home'];v['selection_label']=event['home'];v['market_label']='Победа в основное время'
        v['comparison']={'home':{'text':'СЕКРЕТНОЕ ОБОСНОВАНИЕ: результаты первого участника.'},'away':{'text':'Сравнение результатов соперника.'}}
        v['reason']['text']='СЕКРЕТНОЕ ОБОСНОВАНИЕ выбора до начала матча.'
        with repo.connection() as db:db.execute('UPDATE event_analyses SET payload=?,user_id=? WHERE id=?',(json.dumps(item),owner,item['id']))
        result[key]=item['id']
    return result


@app.post('/fixture/revoke-agreements')
def revoke_agreements(request:Request):
    user=accounts.current_user(request)
    with repo.connection() as db:db.execute('DELETE FROM registration_agreements WHERE user_id=?',(user['id'],))
    return {'ok':True}


@app.post('/fixture/succeed/{order_id}')
def succeed(order_id:str,request:Request):
    user=accounts.current_user(request)
    with repo.connection() as db:
        row=db.execute('SELECT * FROM payment_orders WHERE id=? AND user_id=?',(order_id,user['id'])).fetchone()
        if not row:raise RuntimeError('unknown order')
        value=json.loads(db.execute('SELECT payload FROM browser_payments WHERE id=?',('payment-'+order_id,)).fetchone()[0])
        value.update(status='succeeded',paid=True)
        db.execute('UPDATE browser_payments SET payload=? WHERE id=?',(json.dumps(value),value['id']))
    return {'ok':True}
