"""Loopback-only browser fixture. No real YooKassa or model calls are made."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi import Request
from matchlab import accounts,billing
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from outcome_helpers import forecast

settings=Settings(Path(os.environ['MATCHLAB_DATA_DIR']),'','',billing_enabled=True,billing_public_testing=True,
    yookassa_shop_id='100500',yookassa_secret_key='test_browser',public_origin=os.environ['PUBLIC_ORIGIN'])
repo=Repository(settings.database);repo.initialize()


class FakeProvider:
    def __init__(self,settings):pass
    def create(self,order,origin):
        pid='payment-'+order['id']
        with repo.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS browser_payments (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            old=db.execute('SELECT payload FROM browser_payments WHERE id=?',(pid,)).fetchone()
            if old:return json.loads(old[0])
            value={'id':pid,'status':'pending','paid':False,'test':True,'recipient':{'account_id':'100500'},
                'metadata':{'order_id':order['id'],'user_id':order['user_id']},
                'amount':{'currency':'RUB','value':f"{order['amount_kopecks']/100:.2f}"},
                'confirmation':{'confirmation_url':'https://yoomoney.ru/checkout/payments/v2/contract?orderId='+order['id']}}
            db.execute('INSERT INTO browser_payments VALUES (?,?)',(pid,json.dumps(value)))
            return value
    def payment(self,identifier):
        with repo.connection() as db:row=db.execute('SELECT payload FROM browser_payments WHERE id=?',(identifier,)).fetchone()
        if not row:raise RuntimeError('unknown fake payment')
        return json.loads(row[0])


billing.YooKassa=FakeProvider
app=create_app(settings)


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
