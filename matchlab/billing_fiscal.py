"""Package allocations and durable receipts for a cash register linked to YooKassa."""
from datetime import datetime,timedelta,timezone
import hashlib
import re
from threading import Event,Thread

from fastapi import HTTPException

from . import accounts
from .yookassa import YooKassa,ProviderMismatch,ProviderUnavailable,kopecks

SCHEMA='''
CREATE TABLE IF NOT EXISTS billing_receipt_jobs (
 id TEXT PRIMARY KEY,order_id TEXT NOT NULL,analysis_id TEXT,kind TEXT NOT NULL,
 amount_kopecks INTEGER NOT NULL,state TEXT NOT NULL,provider_receipt_id TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,next_attempt_at TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,error_code TEXT);
CREATE INDEX IF NOT EXISTS receipt_jobs_due ON billing_receipt_jobs(state,next_attempt_at);
CREATE INDEX IF NOT EXISTS receipt_jobs_order ON billing_receipt_jobs(order_id);
'''


def receipt_email(value):
    if not isinstance(value,str):raise HTTPException(422,'Укажите e-mail для кассового чека.')
    value=value.strip()
    if len(value)>254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}",value) or '..' in value:
        raise HTTPException(422,'Проверьте e-mail для кассового чека.')
    return value


def money(amount):return {'value':f'{amount//100}.{amount%100:02d}','currency':'RUB'}


def receipt(settings,order,*,mode='full_prepayment',quantity=None,description=None):
    units=order['credits'] if quantity is None else quantity
    unit=order['amount_kopecks']//order['credits']
    value={'customer':{'email':receipt_email(order.get('receipt_email'))},'items':[{
        'description':description or 'Доступ к одному ИИ-разбору спортивного матча',
        'quantity':str(units)+'.00','amount':money(unit),'vat_code':1,
        'payment_mode':mode,'payment_subject':'service'}]}
    if settings.yookassa_tax_system_code:value['tax_system_code']=settings.yookassa_tax_system_code
    return value


def enqueue(db,order,kind,amount,analysis_id=None):
    identifier=hashlib.sha256((kind+':'+order['id']+':'+str(analysis_id or '')).encode()).hexdigest()[:32]
    stamp=accounts.now()
    db.execute('''INSERT OR IGNORE INTO billing_receipt_jobs(id,order_id,analysis_id,kind,amount_kopecks,state,
        created_at,updated_at,next_attempt_at) VALUES (?,?,?,?,?,'pending',?,?,?)''',
        (identifier,order['id'],analysis_id,kind,amount,stamp,stamp,stamp))


def allocate(db,settings,user_id,analysis_id):
    # The caller holds the same write transaction as the debit and unlock.
    orders=db.execute('''SELECT o.*,coalesce(a.used,0) used FROM payment_orders o LEFT JOIN
        (SELECT order_id,count(*) used FROM analysis_credit_allocations GROUP BY order_id) a ON a.order_id=o.id
        WHERE o.user_id=? AND o.mode='live' AND o.state='succeeded' ORDER BY o.created_at,o.id''',(user_id,)).fetchall()
    for row in orders:
        order=dict(row);unit=order['amount_kopecks']//order['credits']
        refunded=(order['refunded_kopecks']+unit-1)//unit
        if order['credits']-refunded-order['used']<=0:continue
        db.execute('INSERT INTO analysis_credit_allocations VALUES (?,?,?,?,?,?)',(user_id,analysis_id,'live',order['id'],unit,accounts.now()))
        enqueue(db,order,'settlement',unit,analysis_id)
        return
    raise HTTPException(409,'Не удалось подтвердить оплаченный остаток пакета. Баланс не списан; обратитесь в поддержку.')


def process_one(repo,settings):
    if settings.billing_mode!='live' or not settings.billing_receipts_enabled or not settings.yookassa_secret_key.startswith('live_'):return False
    now=datetime.now(timezone.utc);stamp=now.isoformat()
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        raw=db.execute('''SELECT j.* FROM billing_receipt_jobs j JOIN payment_orders o ON o.id=j.order_id
            WHERE o.mode='live' AND j.state IN ('pending','processing') AND j.next_attempt_at<=?
            ORDER BY j.created_at,j.id LIMIT 1''',(stamp,)).fetchone()
        if not raw:return False
        job=dict(raw);order=dict(db.execute('SELECT * FROM payment_orders WHERE id=?',(job['order_id'],)).fetchone())
        db.execute("UPDATE billing_receipt_jobs SET state='processing',updated_at=?,next_attempt_at=?,attempts=attempts+1 WHERE id=?",
            (stamp,(now+timedelta(seconds=90)).isoformat(),job['id']))
    state='pending';error=None;provider_id=job['provider_receipt_id']
    try:
        provider=YooKassa(settings)
        if job['kind']=='prepayment':
            value=provider.payment(order['payment_id'])
            if (value.get('id')!=order['payment_id'] or value.get('test') is not False or (value.get('recipient') or {}).get('account_id')!=settings.yookassa_shop_id or
                value.get('status')!='succeeded' or value.get('paid') is not True or (value.get('amount') or {}).get('currency')!='RUB' or
                kopecks((value.get('amount') or {}).get('value'))!=order['amount_kopecks'] or (value.get('metadata') or {}).get('order_id')!=order['id']):
                raise ProviderMismatch('receipt_payment')
            status=value.get('receipt_registration')
            if status=='succeeded':state='succeeded'
            elif status=='canceled':state='review';error='prepayment_receipt_canceled'
            else:error='awaiting_prepayment_receipt'
        else:
            with repo.connection() as db:
                parent=db.execute("SELECT state FROM billing_receipt_jobs WHERE order_id=? AND kind='prepayment'",(order['id'],)).fetchone()
            if not parent or parent['state']!='succeeded':raise ProviderUnavailable('prepayment_not_registered')
            body=receipt(settings,order,mode='full_payment',quantity=1,description='ИИ-разбор '+job['analysis_id'])
            body.update(type='payment',payment_id=order['payment_id'],send=True,settlements=[{'type':'prepayment','amount':money(job['amount_kopecks'])}])
            if provider_id:value=provider.request('GET','/receipts/'+provider_id)
            elif now-datetime.fromisoformat(job['created_at'])>=timedelta(hours=23):
                # YooKassa's idempotency retention is finite. Never recreate a
                # receipt of unknown status after that interval automatically.
                raise ProviderUnavailable('receipt_requires_review',definitive=True)
            else:value=provider.request('POST','/receipts',data=body,key=job['id'])
            if value.get('payment_id')!=order['payment_id'] or value.get('type')!='payment' or not isinstance(value.get('id'),str) or not re.fullmatch(r'[a-zA-Z0-9-]{10,80}',value['id']):
                raise ProviderMismatch('receipt_identity')
            items=value.get('items') or [];settlements=value.get('settlements') or []
            if (len(items)!=1 or items[0].get('description')!=body['items'][0]['description'] or
                items[0].get('payment_mode')!='full_payment' or kopecks((items[0].get('amount') or {}).get('value'))!=job['amount_kopecks'] or
                (items[0].get('amount') or {}).get('currency')!='RUB' or kopecks(items[0].get('quantity'))!=100 or
                len(settlements)!=1 or settlements[0].get('type')!='prepayment' or kopecks((settlements[0].get('amount') or {}).get('value'))!=job['amount_kopecks']):
                raise ProviderMismatch('receipt_items')
            provider_id=value['id']
            if value.get('status')=='succeeded':state='succeeded'
            elif value.get('status')=='canceled':state='review';error='receipt_canceled'
            else:error='awaiting_receipt_registration'
    except ProviderMismatch:state='review';error='receipt_mismatch'
    except ProviderUnavailable as exc:
        state='review' if exc.definitive else 'pending';error=str(exc)
    except (ValueError,TypeError,KeyError,HTTPException):state='review';error='receipt_data'
    if state=='pending' and now-datetime.fromisoformat(job['created_at'])>=timedelta(hours=23):state='review';error='receipt_requires_review'
    with repo.connection() as db:
        db.execute('UPDATE billing_receipt_jobs SET state=?,provider_receipt_id=?,updated_at=?,next_attempt_at=?,error_code=? WHERE id=?',
            (state,provider_id,accounts.now(),(datetime.now(timezone.utc)+timedelta(seconds=min(900,30*2**min(job['attempts'],5)))).isoformat(),error,job['id']))
    return True


class Worker:
    def __init__(self,repo,settings):self.repo=repo;self.settings=settings;self.event=Event();self.thread=None
    def start(self):
        if self.settings.billing_mode!='live' or not self.settings.billing_receipts_enabled or not self.settings.yookassa_secret_key.startswith('live_'):return
        self.thread=Thread(target=self.run,name='billing-receipts',daemon=True);self.thread.start()
    def run(self):
        import logging
        while not self.event.wait(5):
            try:
                for _ in range(20):
                    if self.event.is_set() or not process_one(self.repo,self.settings):break
            except Exception:logging.getLogger('uvicorn.error').warning('Receipt worker: retry pending jobs after an internal error')
    def stop(self):
        self.event.set()
        if self.thread:self.thread.join(timeout=1)
