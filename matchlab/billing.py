"""Test-store orders, an auditable credit ledger and atomic one-credit unlocks."""
from datetime import datetime,timedelta,timezone
import json
import re
from uuid import uuid4

from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse

from . import accounts
from .yookassa import YooKassa,ProviderUnavailable,ProviderMismatch,kopecks,confirmation_url

MODE='test'
OFFER_VERSION='2026-11-11.test.v1'
PUBLIC_OFFER_VERSION='2026-11-11.v1'
PACKAGES=[{'id':'one','credits':1,'amount_kopecks':49000},
          {'id':'five','credits':5,'amount_kopecks':219000},
          {'id':'ten','credits':10,'amount_kopecks':409000}]
SCHEMA='''
CREATE TABLE IF NOT EXISTS payment_orders (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,request_key TEXT NOT NULL,mode TEXT NOT NULL,
 package_id TEXT NOT NULL,credits INTEGER NOT NULL,amount_kopecks INTEGER NOT NULL,
 state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 payment_id TEXT UNIQUE,confirmation_url TEXT,analysis_id TEXT,offer_version TEXT NOT NULL,
 refunded_kopecks INTEGER NOT NULL DEFAULT 0,error_code TEXT,UNIQUE(user_id,request_key));
CREATE INDEX IF NOT EXISTS payment_orders_owner ON payment_orders(user_id,created_at);
CREATE TABLE IF NOT EXISTS analysis_credit_ledger (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,mode TEXT NOT NULL,delta INTEGER NOT NULL,
 kind TEXT NOT NULL,reference TEXT NOT NULL,at TEXT NOT NULL,UNIQUE(mode,kind,reference));
CREATE INDEX IF NOT EXISTS credit_ledger_owner ON analysis_credit_ledger(user_id,mode,at);
CREATE TABLE IF NOT EXISTS analysis_unlocks (
 user_id TEXT NOT NULL,analysis_id TEXT NOT NULL,mode TEXT NOT NULL,at TEXT NOT NULL,
 source TEXT NOT NULL,PRIMARY KEY(user_id,analysis_id,mode));
CREATE TABLE IF NOT EXISTS payment_refunds (
 id TEXT PRIMARY KEY,order_id TEXT NOT NULL,amount_kopecks INTEGER NOT NULL,at TEXT NOT NULL);
'''


def initialize(repo,settings):
    if not settings.billing_enabled:return
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM site_settings WHERE key='billing_test_started'").fetchone():return
        stamp=accounts.now()
        # Answers already delivered free remain open to their original owners.
        db.execute("""INSERT OR IGNORE INTO analysis_unlocks
            SELECT a.user_id,a.id,?,?, 'legacy' FROM event_analyses a JOIN users u ON u.id=a.user_id""",(MODE,stamp))
        db.execute('INSERT INTO site_settings VALUES (?,?,?)',('billing_test_started',stamp,stamp))


def active(repo,settings):
    if settings.billing_enabled:return True
    with repo.connection() as db:return bool(db.execute("SELECT 1 FROM site_settings WHERE key='billing_test_started'").fetchone())


def balance(db,user_id):
    paid=db.execute('SELECT coalesce(sum(delta),0) FROM analysis_credit_ledger WHERE user_id=? AND mode=?',(user_id,MODE)).fetchone()[0]
    row=db.execute('SELECT bonus_analyses FROM users WHERE id=?',(user_id,)).fetchone()
    return int(paid+(row['bonus_analyses'] if row else 0))


def wallet(repo,user_id):
    with repo.connection() as db:
        amount=balance(db,user_id)
        bonus=db.execute('SELECT bonus_analyses FROM users WHERE id=?',(user_id,)).fetchone()
        orders=db.execute('SELECT * FROM payment_orders WHERE user_id=? AND mode=? ORDER BY created_at DESC LIMIT 20',(user_id,MODE)).fetchall()
    return {'balance':max(0,amount),'bonus_balance':bonus['bonus_analyses'] if bonus else 0,'adjustment_due':max(0,-amount),'test_mode':True,
        'orders':[public_order(row) for row in orders]}


def opened(repo,user_id,offset=0,limit=20):
    if type(offset) is not int or not 0<=offset<=100000 or type(limit) is not int or not 1<=limit<=100:
        raise HTTPException(422,'Некорректная страница разборов.')
    with repo.connection() as db:
        total=db.execute('SELECT count(*) FROM analysis_unlocks WHERE user_id=? AND mode=?',(user_id,MODE)).fetchone()[0]
        rows=db.execute('''SELECT a.id,a.user_id,a.payload,u.at FROM analysis_unlocks u JOIN event_analyses a ON a.id=u.analysis_id
            WHERE u.user_id=? AND u.mode=? ORDER BY u.at DESC,u.analysis_id DESC LIMIT ? OFFSET ?''',(user_id,MODE,limit,offset)).fetchall()
    items=[]
    for row in rows:
        payload=json.loads(row['payload']);event=payload.get('event') or {}
        items.append({'id':row['id'],'title':str(event.get('home') or 'Участник')+' — '+str(event.get('away') or 'Участник'),
            'opened_at':row['at'],'route':('#result/' if row['user_id']=='__system_featured__' else '#analysis/')+row['id']})
    return {'items':items,'total':total,'offset':offset,'limit':limit}


def ready(payload):
    context=payload.get('context') or {};v=context.get('neural_verdict') or {};g=context.get('ai_generation') or {}
    return g.get('state')=='ready' and v.get('engine')=='qwen' and v.get('decision') in ('bet','skip') and not v.get('data_forced_skip') and not (context.get('neural_input') or {}).get('policy',{}).get('force_skip')


def entitled(repo,user,identifier):
    if not user:return False
    if user.get('role')=='admin':return True
    with repo.connection() as db:return bool(db.execute('SELECT 1 FROM analysis_unlocks WHERE user_id=? AND analysis_id=? AND mode=?',(user['id'],identifier,MODE)).fetchone())


def locked(repo,settings,payload,user=None):
    if not active(repo,settings) or entitled(repo,user,payload['id']):return None
    context=payload.get('context') or {};v=context.get('neural_verdict') or {}
    skip=v.get('decision')=='skip'
    return {'id':payload['id'],'generated_at':payload.get('generated_at'),'analysis_type':'locked',
        'locked':True,'decision':'skip' if skip else None,'text':'Рекомендуем пропустить матч' if skip else '',
        'unlock':{'cost':1,'available':ready(payload),'authenticated':bool(user),
            'balance':wallet(repo,user['id'])['balance'] if user else 0,'test_mode':True}}


def protect(repo,settings,payload,user):
    return locked(repo,settings,payload,user) or payload


def unlock(repo,settings,user,identifier):
    if not active(repo,settings):raise HTTPException(409,'Платный доступ пока не включён.')
    if not re.fullmatch(r'[a-f0-9]{32}',identifier):raise HTTPException(404,'Разбор не найден.')
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT payload,user_id FROM event_analyses WHERE id=?',(identifier,)).fetchone()
        if not row or row['user_id'] not in (user['id'],'__system_featured__'):
            raise HTTPException(404,'Разбор не найден.')
        payload=json.loads(row['payload'])
        if not ready(payload):raise HTTPException(409,'Этот ответ не содержит подтверждённого разбора ИИ. Списание недоступно.')
        old=db.execute('SELECT 1 FROM analysis_unlocks WHERE user_id=? AND analysis_id=? AND mode=?',(user['id'],identifier,MODE)).fetchone()
        route=('#analysis/' if row['user_id']==user['id'] else '#result/')+identifier
        if old or user.get('role')=='admin':return {'unlocked':True,'charged':0,'balance':max(0,balance(db,user['id'])),'route':route}
        from .bookmakers import instant
        published=[]
        if instant(payload['event']['kickoff_at'])<=datetime.now(timezone.utc):
            from .public_results import report
            published=report(repo,analysis_id=identifier,limit=1)['matches']
        if published and published[0]['state']=='settled':
            db.execute('INSERT INTO analysis_unlocks VALUES (?,?,?,?,?)',(user['id'],identifier,MODE,accounts.now(),'public'))
            return {'unlocked':True,'charged':0,'balance':max(0,balance(db,user['id'])),'route':'#result/'+identifier}
        if balance(db,user['id'])<1:raise HTTPException(402,'Недостаточно разборов. Пополните баланс.' if test_checkout_allowed(settings,user) else 'Нет доступных разборов. Приём новых оплат ещё не открыт.')
        bonus=db.execute('SELECT bonus_analyses FROM users WHERE id=?',(user['id'],)).fetchone()[0]>0
        if bonus:db.execute('UPDATE users SET bonus_analyses=bonus_analyses-1 WHERE id=?',(user['id'],))
        db.execute('INSERT INTO analysis_credit_ledger VALUES (?,?,?,?,?,?,?)',
            (uuid4().hex,user['id'],MODE,0 if bonus else -1,'bonus_unlock' if bonus else 'unlock',user['id']+':'+identifier,accounts.now()))
        db.execute('INSERT INTO analysis_unlocks VALUES (?,?,?,?,?)',(user['id'],identifier,MODE,accounts.now(),'bonus' if bonus else 'credit'))
        accounts.audit(db,user['id'],'analysis.unlocked',identifier,{'cost':1,'mode':MODE})
        return {'unlocked':True,'charged':1,'balance':max(0,balance(db,user['id'])),'route':route}


def public_order(row):
    return {key:row[key] for key in ('id','package_id','credits','amount_kopecks','state','created_at','confirmation_url','analysis_id','refunded_kopecks')}|{'test_mode':row['mode']=='test'}


def test_checkout_allowed(settings,user):
    return bool(settings.billing_enabled and (settings.billing_public_testing or user and user.get('role')=='admin'))


def apply_payment(repo,settings,value):
    if not isinstance(value,dict) or not all(isinstance(value.get(key),dict) for key in ('metadata','recipient','amount')):
        raise ProviderMismatch('payment_shape')
    metadata=value.get('metadata') or {};identifier=metadata.get('order_id')
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        order=db.execute('SELECT * FROM payment_orders WHERE id=?',(identifier,)).fetchone()
        if not order:raise ProviderMismatch('order')
        if (value.get('test') is not True or value.get('recipient',{}).get('account_id')!=settings.yookassa_shop_id or
            metadata.get('user_id')!=order['user_id'] or (value.get('amount') or {}).get('currency')!='RUB' or
            kopecks(value.get('amount',{}).get('value'))!=order['amount_kopecks'] or
            not isinstance(value.get('id'),str) or not re.fullmatch(r'[a-zA-Z0-9-]{10,80}',value['id']) or
            order['payment_id'] not in (None,value['id'])):raise ProviderMismatch('payment_mismatch')
        state=value.get('status')
        if state not in ('pending','waiting_for_capture','succeeded','canceled'):raise ProviderMismatch('status')
        if state=='succeeded' and value.get('paid') is not True:raise ProviderMismatch('not_paid')
        if state=='waiting_for_capture':state='pending'
        if order['state'] in ('succeeded','refunded') and state!='succeeded':return public_order(order)
        url=confirmation_url(value)
        if state=='succeeded':
            db.execute('INSERT OR IGNORE INTO analysis_credit_ledger VALUES (?,?,?,?,?,?,?)',
                (uuid4().hex,order['user_id'],MODE,order['credits'],'payment',order['id'],accounts.now()))
        db.execute('UPDATE payment_orders SET payment_id=?,state=?,confirmation_url=coalesce(?,confirmation_url),updated_at=?,error_code=NULL WHERE id=?',
            (value['id'],'refunded' if order['state']=='refunded' else state,url,accounts.now(),order['id']))
        return public_order(db.execute('SELECT * FROM payment_orders WHERE id=?',(order['id'],)).fetchone())


def reconcile(repo,settings,order):
    provider=YooKassa(settings)
    if order['payment_id']:value=provider.payment(order['payment_id'])
    else:
        if datetime.now(timezone.utc)-datetime.fromisoformat(order['created_at'])>timedelta(hours=23):
            raise HTTPException(409,'Платёж требует проверки поддержки. Не создавайте повторную оплату.')
        value=provider.create(order,settings.public_origin)
    return apply_payment(repo,settings,value)


def create_order(repo,settings,user,body):
    if not test_checkout_allowed(settings,user):raise HTTPException(503,'Приём оплаты ещё не открыт. Тарифы и условия опубликованы для ознакомления.')
    if not settings.billing_enabled:raise HTTPException(503,'Тестовая оплата пока не настроена.')
    package=next((p for p in PACKAGES if p['id']==body.get('package_id')),None)
    key=body.get('request_key');analysis_id=body.get('analysis_id')
    if not package or not isinstance(key,str) or not re.fullmatch(r'[a-f0-9]{32}',key):raise HTTPException(422,'Выберите пакет и повторите оплату.')
    if body.get('offer_accepted') is not True or body.get('offer_version')!=OFFER_VERSION:raise HTTPException(422,'Примите условия приобретения разборов.')
    if analysis_id is not None:
        if not isinstance(analysis_id,str) or not re.fullmatch(r'[a-f0-9]{32}',analysis_id):raise HTTPException(422,'Некорректный разбор.')
        with repo.connection() as db:row=db.execute('SELECT user_id FROM event_analyses WHERE id=?',(analysis_id,)).fetchone()
        if not row or row['user_id'] not in (user['id'],'__system_featured__'):raise HTTPException(404,'Разбор не найден.')
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT * FROM payment_orders WHERE user_id=? AND request_key=?',(user['id'],key)).fetchone()
        if old:
            if old['package_id']!=package['id'] or old['analysis_id']!=analysis_id:raise HTTPException(409,'Ключ уже использован для другой покупки.')
            order=dict(old)
        else:
            identifier=uuid4().hex;stamp=accounts.now()
            db.execute('''INSERT INTO payment_orders(id,user_id,request_key,mode,package_id,credits,amount_kopecks,state,
                created_at,updated_at,analysis_id,offer_version) VALUES (?,?,?,?,?,?,?,'creating',?,?,?,?)''',
                (identifier,user['id'],key,MODE,package['id'],package['credits'],package['amount_kopecks'],stamp,stamp,analysis_id,OFFER_VERSION))
            order=dict(db.execute('SELECT * FROM payment_orders WHERE id=?',(identifier,)).fetchone())
    if order['state'] in ('succeeded','canceled','refunded','failed'):return public_order(order)
    try:return reconcile(repo,settings,order)
    except ProviderMismatch:raise HTTPException(502,'Не удалось подтвердить параметры тестового платежа. Баланс не изменён.') from None
    except ProviderUnavailable as error:
        if error.definitive:
            with repo.connection() as db:
                db.execute("UPDATE payment_orders SET state='failed',error_code='provider_rejected',updated_at=? WHERE id=? AND payment_id IS NULL AND state='creating'",(accounts.now(),order['id']))
                current=db.execute('SELECT * FROM payment_orders WHERE id=?',(order['id'],)).fetchone()
            return {**public_order(current),'message':'Тестовый платёж не создан. Проверьте настройки магазина; баланс не изменён.'}
        return {**public_order(order),'state':'checking','retry_after':error.retry_after,'message':'Ответ платёжного сервиса задерживается. Проверьте статус этого заказа.'}


def order_status(repo,settings,user,identifier):
    with repo.connection() as db:row=db.execute('SELECT * FROM payment_orders WHERE id=? AND user_id=?',(identifier,user['id'])).fetchone()
    if not row:raise HTTPException(404,'Заказ не найден.')
    if row['state'] in ('succeeded','canceled','refunded','failed'):return public_order(row)
    try:return reconcile(repo,settings,dict(row))
    except ProviderMismatch:raise HTTPException(502,'Платёж требует проверки. Баланс не изменён.') from None
    except ProviderUnavailable as error:return {**public_order(row),'state':'checking','retry_after':error.retry_after}


def apply_refund(repo,settings,value):
    if value.get('status')!='succeeded' or (value.get('amount') or {}).get('currency')!='RUB':raise ProviderMismatch('refund')
    amount=kopecks(value['amount']['value'])
    if amount<=0:raise ProviderMismatch('refund_amount')
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        order=db.execute('SELECT * FROM payment_orders WHERE payment_id=?',(value.get('payment_id'),)).fetchone()
        if not order or order['state'] not in ('succeeded','refunded'):raise ProviderMismatch('refund_order')
        if db.execute('SELECT 1 FROM payment_refunds WHERE id=?',(value['id'],)).fetchone():return
        total=order['refunded_kopecks']+amount
        if total>order['amount_kopecks']:raise ProviderMismatch('refund_total')
        old_units=(order['refunded_kopecks']*order['credits']+order['amount_kopecks']-1)//order['amount_kopecks']
        units=(total*order['credits']+order['amount_kopecks']-1)//order['amount_kopecks']-old_units
        db.execute('INSERT INTO payment_refunds VALUES (?,?,?,?)',(value['id'],order['id'],amount,accounts.now()))
        db.execute('INSERT INTO analysis_credit_ledger VALUES (?,?,?,?,?,?,?)',(uuid4().hex,order['user_id'],MODE,-units,'refund',value['id'],accounts.now()))
        db.execute('UPDATE payment_orders SET refunded_kopecks=?,state=?,updated_at=? WHERE id=?',
            (total,'refunded' if total==order['amount_kopecks'] else 'succeeded',accounts.now(),order['id']))


def install(app,repo,settings):
    from . import billing_analytics
    billing_analytics.install(app,repo)
    @app.get('/api/billing/config')
    def config(request:Request):
        allowed=test_checkout_allowed(settings,accounts.current_user(request,False))
        return JSONResponse({'enabled':active(repo,settings),'checkout_enabled':allowed,'test_checkout_allowed':allowed,
            'sales_state':'prelaunch','test_mode':True,'packages':PACKAGES,'offer_version':OFFER_VERSION,'unlock_cost':1,
            'offer_url':'/static/legal/test-purchase.html' if allowed else '/static/legal/offer.html',
            'public_offer_version':PUBLIC_OFFER_VERSION,'public_offer_url':'/static/legal/offer.html'},
            headers={'Cache-Control':'private, no-store'})

    @app.get('/api/billing/wallet')
    def get_wallet(request:Request):return wallet(repo,accounts.current_user(request)['id'])

    @app.get('/api/billing/unlocks')
    def get_opened(request:Request,offset:int=0,limit:int=20):return opened(repo,accounts.current_user(request)['id'],offset,limit)

    @app.post('/api/billing/analyses/{identifier}/unlock')
    def open_analysis(identifier:str,request:Request):
        user=accounts.current_user(request);accounts.mutation(request)
        accounts.limited(repo,'unlock:'+user['id'],30,60)
        return unlock(repo,settings,user,identifier)

    @app.post('/api/billing/orders')
    async def purchase(request:Request):
        from starlette.concurrency import run_in_threadpool
        user=accounts.current_user(request);accounts.mutation(request)
        if not accounts.agreements_current(repo,user['id']):raise HTTPException(403,'Подтвердите действующие согласия.')
        accounts.limited(repo,'purchase:'+user['id'],12,60)
        body=await accounts.body(request)
        return await run_in_threadpool(create_order,repo,settings,user,body)

    @app.get('/api/billing/orders/{identifier}')
    def status(identifier:str,request:Request):
        user=accounts.current_user(request);accounts.limited(repo,'payment-poll:'+user['id'],40,60)
        return order_status(repo,settings,user,identifier)

    @app.post('/api/billing/yookassa/webhook')
    async def webhook(request:Request):
        from starlette.concurrency import run_in_threadpool
        if not active(repo,settings):return {'ok':True}
        raw=await request.body()
        if len(raw)>32768:raise HTTPException(413,'Слишком большое уведомление.')
        try:
            body=json.loads(raw);event=body.get('event');obj=body.get('object') or {};identifier=obj.get('id')
            metadata=obj.get('metadata') or {}
            if not isinstance(metadata,dict):raise ValueError
        except (ValueError,AttributeError):raise HTTPException(400,'Некорректное уведомление.') from None
        if event not in ('payment.succeeded','payment.canceled','refund.succeeded') or not isinstance(identifier,str) or not re.fullmatch(r'[a-zA-Z0-9-]{10,80}',identifier):return {'ok':True}
        with repo.connection() as db:
            if event=='refund.succeeded':
                payment_id=obj.get('payment_id')
                if not isinstance(payment_id,str):return {'ok':True}
                known=db.execute('SELECT 1 FROM payment_orders WHERE payment_id=?',(payment_id,)).fetchone()
            else:
                order_id=metadata.get('order_id')
                if not isinstance(order_id,str):order_id=None
                known=db.execute('SELECT 1 FROM payment_orders WHERE payment_id=? OR id=?',(identifier,order_id)).fetchone()
        if not known:return {'ok':True}
        try:
            provider=YooKassa(settings)
            if event=='refund.succeeded':
                value=await run_in_threadpool(provider.refund,identifier)
                await run_in_threadpool(apply_refund,repo,settings,value)
            else:
                value=await run_in_threadpool(provider.payment,identifier)
                await run_in_threadpool(apply_payment,repo,settings,value)
        except (ProviderUnavailable,ProviderMismatch):raise HTTPException(503,'Уведомление пока не подтверждено.') from None
        return {'ok':True}
