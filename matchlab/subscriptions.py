"""Durable subscriptions. Only verified provider payments grant access.

Finite grants accumulate in the existing permanent credit ledger. Unlimited
access expires; it never consumes that ledger. No scheduler runs money charges
until the operator explicitly enables the recurring-payment feature.
"""
from datetime import datetime, timedelta, timezone
import json
import re
from threading import Event, Thread
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import accounts
from .yookassa import YooKassa, ProviderMismatch, ProviderUnavailable

VERSION = '2026-11-12.3days.v1'
PLANS = [dict(id='three_days_three', credits=3, amount_kopecks=49000, unlimited=False, period_hours=72),
         dict(id='three_days_fifteen', credits=15, amount_kopecks=229000, unlimited=False, period_hours=72),
         dict(id='three_days_unlimited', credits=0, amount_kopecks=609000, unlimited=True, period_hours=72)]
# Plan IDs are immutable contracts. Old payments and renewals keep their
# original interval, amount and grants, including callbacks received after deploy.
PLAN_PERIODS = {'daily_one':24, 'daily_five':24, 'daily_unlimited':24,
                **{p['id']:p['period_hours'] for p in PLANS}}


def period_hours(value):
    row=dict(value)
    identifier=row.get('plan_id') or row.get('package_id') or row.get('id')
    dynamic=re.fullmatch(r'sub_([0-9]{2,4})_[a-f0-9]{32}_[a-f0-9]{12}_[dr]',str(identifier))
    if dynamic and 24<=int(dynamic[1])<=8760 and int(dynamic[1])%24==0:return int(dynamic[1])
    try:return PLAN_PERIODS[identifier]
    except KeyError:raise ProviderMismatch('subscription_plan') from None


def period_label(value):
    n=period_hours(value)//24
    return str(n)+' '+('день' if n%10==1 and n%100!=11 else 'дня' if n%10 in (2,3,4) and n%100 not in (12,13,14) else 'дней')


def period_for_id(identifier):
    return period_hours({'id':identifier}) if identifier in PLAN_PERIODS or str(identifier).startswith('sub_') else None


def credit_label(n):
    return str(n)+' '+('разбор' if n%10==1 and n%100!=11 else 'разбора' if n%10 in (2,3,4) and n%100 not in (12,13,14) else 'разборов')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS billing_subscriptions (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,mode TEXT NOT NULL,plan_id TEXT NOT NULL,
 credits INTEGER NOT NULL,amount_kopecks INTEGER NOT NULL,state TEXT NOT NULL,
 auto_renew INTEGER NOT NULL DEFAULT 1,payment_method_id TEXT,receipt_email TEXT,
 period_end_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 canceled_at TEXT,consent_at TEXT NOT NULL,consent_text TEXT NOT NULL,
 offer_version TEXT NOT NULL,recurring_version TEXT NOT NULL,error_code TEXT);
CREATE INDEX IF NOT EXISTS subscriptions_owner ON billing_subscriptions(user_id,mode,created_at);
CREATE INDEX IF NOT EXISTS subscriptions_due ON billing_subscriptions(auto_renew,state,period_end_at);
CREATE TABLE IF NOT EXISTS billing_subscription_charges (
 order_id TEXT PRIMARY KEY,subscription_id TEXT NOT NULL,period_number INTEGER NOT NULL,
 payment_method_id TEXT,started_at TEXT,next_attempt_at TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,UNIQUE(subscription_id,period_number));
CREATE INDEX IF NOT EXISTS subscription_charges_due ON billing_subscription_charges(next_attempt_at);
CREATE TABLE IF NOT EXISTS billing_subscription_periods (
 order_id TEXT PRIMARY KEY,subscription_id TEXT NOT NULL,period_number INTEGER NOT NULL,
 starts_at TEXT NOT NULL,ends_at TEXT NOT NULL,unlimited INTEGER NOT NULL,
 refunded INTEGER NOT NULL DEFAULT 0,UNIQUE(subscription_id,period_number));
CREATE INDEX IF NOT EXISTS subscription_access ON billing_subscription_periods(subscription_id,ends_at);
'''


def enabled(settings):
    return bool(settings.billing_enabled and settings.billing_recurring_enabled and
                (settings.billing_mode == 'test' or settings.billing_receipts_enabled))


def consent_text(plan):
    quantity = 'безлимитный доступ' if plan['credits'] == 0 else credit_label(plan['credits']) + ' на постоянный баланс'
    def rub(n):return str(n//100)+(f',{n%100:02d}' if n%100 else '')+' ₽'
    first=plan['amount_kopecks'];regular=plan.get('renewal_amount_kopecks',first);hours=period_hours(plan)
    unit='часа' if hours%10 in (2,3,4) and hours%100 not in (12,13,14) else 'часов'
    price=(rub(first)+' сейчас, затем '+rub(regular)) if first!=regular else rub(first)+' сейчас и далее'
    return (f"Подключаю {quantity}. Разрешаю ЮKassa сохранить способ оплаты, а сервису списывать "
            f"{price} раз в {period_label(plan)} (каждые {hours} {unit}) до отмены. "
            'Отмена: Личный кабинет → Подписка → Отвязать карту и отменить подписку. '
            'Деньги за подписку списываются независимо от использования. '
            'Разбор расходуется только при открытии решения «Заходим»; «Не заходим» — бесплатно.')


def plans():
    return [p|{'consent_text':consent_text(p)} for p in PLANS]


def public(row):
    from .pricing import document_url
    return {k: row[k] for k in ('id', 'plan_id', 'credits', 'amount_kopecks', 'state', 'auto_renew',
                                'period_end_at', 'created_at', 'canceled_at', 'error_code')} | {'unlimited': row['credits'] == 0,'period_hours':period_hours(row),'recurring_url':document_url(row['offer_version'],'recurring')}


def unlimited(db, user_id, mode, stamp=None):
    return bool(db.execute('''SELECT 1 FROM billing_subscription_periods p JOIN billing_subscriptions s ON s.id=p.subscription_id
        WHERE s.user_id=? AND s.mode=? AND p.unlimited=1 AND p.refunded=0 AND p.starts_at<=? AND p.ends_at>?
        LIMIT 1''', (user_id, mode, stamp or accounts.now(), stamp or accounts.now())).fetchone())


def summary(db, user_id, mode):
    rows = db.execute('SELECT * FROM billing_subscriptions WHERE user_id=? AND mode=? ORDER BY created_at DESC,id DESC LIMIT 20',
                      (user_id, mode)).fetchall()
    return {'current': public(rows[0]) if rows else None, 'history': [public(r) for r in rows],
            'unlimited_active': unlimited(db, user_id, mode)}


def _order(db, settings, sub, period, request_key=None,first_amount=None):
    identifier = uuid4().hex; stamp = accounts.now()
    db.execute('''INSERT INTO payment_orders(id,user_id,request_key,mode,package_id,credits,amount_kopecks,state,
        created_at,updated_at,offer_version,receipt_email,subscription_id)
        VALUES (?,?,?,?,?,?,?,'creating',?,?,?,?,?)''',
        (identifier, sub['user_id'], request_key or uuid4().hex, sub['mode'], sub['plan_id'], sub['credits'],
         first_amount if period==1 and first_amount is not None else sub['amount_kopecks'], stamp, stamp, sub['offer_version'], sub['receipt_email'], sub['id']))
    db.execute('''INSERT INTO billing_subscription_charges(order_id,subscription_id,period_number,payment_method_id,next_attempt_at)
        VALUES (?,?,?,?,?)''', (identifier, sub['id'], period, sub['payment_method_id'] if period > 1 else None, stamp))
    return dict(db.execute('SELECT * FROM payment_orders WHERE id=?', (identifier,)).fetchone())


def create(repo, settings, user, body):
    from . import billing
    from .billing_fiscal import receipt_email
    if not enabled(settings) or not billing.checkout_allowed(settings, user):
        raise HTTPException(503, 'Подписки пока недоступны. Действующие разборы сохраняются.')
    from .pricing import checkout
    catalogue=checkout(repo,settings)
    plan = next((p for p in catalogue['subscription_plans'] if p['id'] == body.get('plan_id')), None)
    key = body.get('request_key')
    if not plan or not isinstance(key, str) or not re.fullmatch('[a-f0-9]{32}', key):
        raise HTTPException(422, 'Тариф недоступен или условия обновились. Закройте и снова откройте тарифы перед оплатой.')
    if body.get('offer_version') != catalogue['offer_version'] or body.get('recurring_version') != catalogue['recurring_version']:
        raise HTTPException(422,'Условия обновились. Закройте и снова откройте тарифы, затем подтвердите новые условия.')
    if body.get('offer_accepted') is not True or body.get('recurring_accepted') is not True:
        raise HTTPException(422, 'Отдельно подтвердите оферту и автоматические списания по периоду выбранного тарифа.')
    email = receipt_email(body.get('receipt_email'))
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT * FROM payment_orders WHERE user_id=? AND request_key=?', (user['id'], key)).fetchone()
        if prior:
            if (not prior['subscription_id'] or prior['mode'] != settings.billing_mode or
                    prior['package_id'] != plan['id'] or prior['receipt_email'] != email):
                raise HTTPException(409, 'Этот ключ уже использован для другого заказа.')
            order = dict(prior)
        else:
            active = db.execute('''SELECT 1 FROM billing_subscriptions s WHERE user_id=? AND mode=? AND
                (state IN ('creating','review') OR auto_renew=1 OR
                 EXISTS(SELECT 1 FROM payment_orders o WHERE o.subscription_id=s.id AND o.state IN ('creating','pending')) OR
                 (credits=0 AND EXISTS(SELECT 1 FROM billing_subscription_periods p WHERE p.subscription_id=s.id
                    AND p.refunded=0 AND p.ends_at>?))) LIMIT 1''',
                (user['id'], settings.billing_mode, accounts.now())).fetchone()
            if active: raise HTTPException(409, 'Подписка уже оформлена. Откройте управление в личном кабинете.')
            identifier = uuid4().hex; stamp = accounts.now()
            db.execute('''INSERT INTO billing_subscriptions(id,user_id,mode,plan_id,credits,amount_kopecks,state,
                receipt_email,created_at,updated_at,consent_at,consent_text,offer_version,recurring_version)
                VALUES (?,?,?,?,?,?,'creating',?,?,?,?,?,?,?)''',
                (identifier, user['id'], settings.billing_mode, plan['id'], plan['credits'], plan['renewal_amount_kopecks'],
                 email, stamp, stamp, stamp, consent_text(plan), catalogue['offer_version'], catalogue['recurring_version']))
            sub = dict(db.execute('SELECT * FROM billing_subscriptions WHERE id=?', (identifier,)).fetchone())
            order = _order(db, settings, sub, 1, key,first_amount=plan['amount_kopecks'])
            accounts.audit(db, user['id'], 'subscription.consent', identifier,
                           {'plan': plan['id'], 'version': catalogue['recurring_version'], 'first_amount_kopecks': plan['amount_kopecks'], 'amount_kopecks': plan['renewal_amount_kopecks'], 'period_hours': period_hours(plan)})
    if order['state'] not in ('succeeded', 'refunded', 'canceled', 'failed'):
        return reconcile(repo, settings, order)
    return billing.public_order(order)


def _stop(db, sub_id, state, error=None):
    db.execute('''UPDATE billing_subscriptions SET auto_renew=0,payment_method_id=NULL,state=?,error_code=?,updated_at=?
        WHERE id=?''', (state, error, accounts.now(), sub_id))


def cancel(repo, settings, user, identifier):
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        sub = db.execute('SELECT * FROM billing_subscriptions WHERE id=? AND user_id=? AND mode=?',
                         (identifier, user['id'], settings.billing_mode)).fetchone()
        if not sub: raise HTTPException(404, 'Подписка не найдена.')
        db.execute('''UPDATE billing_subscriptions SET auto_renew=0,payment_method_id=NULL,canceled_at=coalesce(canceled_at,?),
            updated_at=? WHERE id=?''', (accounts.now(), accounts.now(), identifier))
        # Unsent renewals must never leave the process after cancellation.
        db.execute('''UPDATE payment_orders SET state='canceled',updated_at=? WHERE id IN
            (SELECT order_id FROM billing_subscription_charges WHERE subscription_id=? AND started_at IS NULL)
            AND state='creating' ''', (accounts.now(), identifier))
        if sub['state'] != 'review':
            db.execute("UPDATE billing_subscriptions SET state='canceled' WHERE id=?", (identifier,))
        accounts.audit(db, user['id'], 'subscription.canceled', identifier, {})
        return public(db.execute('SELECT * FROM billing_subscriptions WHERE id=?', (identifier,)).fetchone())


def apply_verified(db, settings, order, value, state):
    """Called only inside billing.apply_payment's exact amount/shop/owner checks."""
    from .billing_fiscal import enqueue
    sub = db.execute('SELECT * FROM billing_subscriptions WHERE id=?', (order['subscription_id'],)).fetchone()
    charge = db.execute('SELECT * FROM billing_subscription_charges WHERE order_id=?', (order['id'],)).fetchone()
    if (not sub or not charge or sub['mode'] != order['mode'] or
            (value.get('metadata') or {}).get('subscription_id') != sub['id']): raise ProviderMismatch('subscription_order')
    if state == 'canceled':
        _stop(db, sub['id'], 'past_due', 'payment_canceled')
        return
    if state != 'succeeded': return
    if db.execute('SELECT 1 FROM billing_subscription_periods WHERE order_id=?', (order['id'],)).fetchone(): return
    now = datetime.now(timezone.utc)
    # Provider capture time is the actual start. Duplicate notifications can
    # never extend an existing period or grant another batch of credits.
    captured = value.get('captured_at')
    try:
        start = datetime.fromisoformat(captured.replace('Z', '+00:00')) if captured else now
        if start.tzinfo is None or start > now + timedelta(minutes=5) or start < datetime.fromisoformat(order['created_at']) - timedelta(minutes=5):
            raise ValueError
    except (ValueError, TypeError, AttributeError): raise ProviderMismatch('subscription_capture_time') from None
    start=start.astimezone(timezone.utc)
    end = start + timedelta(hours=period_hours(sub))
    db.execute('INSERT INTO billing_subscription_periods VALUES (?,?,?,?,?,?,0)',
               (order['id'], sub['id'], charge['period_number'], start.isoformat(), end.isoformat(), int(sub['credits'] == 0)))
    if sub['credits']:
        db.execute('INSERT OR IGNORE INTO analysis_credit_ledger VALUES (?,?,?,?,?,?,?)',
                   (uuid4().hex, order['user_id'], order['mode'], order['credits'], 'payment', order['id'], accounts.now()))
    if order['mode'] == 'live':
        enqueue(db, dict(order), 'prepayment' if sub['credits'] else 'subscription', order['amount_kopecks'])
    method = value.get('payment_method') or {}
    if not isinstance(method,dict):raise ProviderMismatch('subscription_method_shape')
    method_id = method.get('id')
    saved = (method.get('saved') is True and method.get('type') == 'bank_card' and isinstance(method_id, str)
             and re.fullmatch(r'[a-zA-Z0-9-]{10,100}', method_id))
    if charge['period_number'] > 1 and method_id != charge['payment_method_id']: raise ProviderMismatch('subscription_method')
    renew = bool(sub['auto_renew'] and not sub['canceled_at'] and saved)
    db.execute('''UPDATE billing_subscriptions SET state=?,auto_renew=?,payment_method_id=?,period_end_at=?,updated_at=?,error_code=?
        WHERE id=?''', ('active' if renew else 'canceled', int(renew), method_id if renew else None, end.isoformat(),
                       accounts.now(), None if saved else 'payment_method_not_saved', sub['id']))
    db.execute('UPDATE billing_subscription_charges SET payment_method_id=NULL WHERE order_id=?', (order['id'],))
    accounts.audit(db, order['user_id'], 'subscription.paid', sub['id'], {'order_id': order['id'], 'period': charge['period_number']})


def reconcile(repo, settings, order):
    """Retry only the original charge/key; never invent a replacement payment."""
    from . import billing
    provider = YooKassa(settings)
    if order['payment_id']:
        try:return billing.apply_payment(repo, settings, provider.payment(order['payment_id']))
        except ProviderMismatch:
            with repo.connection() as db:
                db.execute('BEGIN IMMEDIATE'); _stop(db, order['subscription_id'], 'review', 'payment_mismatch')
            raise
    now = datetime.now(timezone.utc)
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current = dict(db.execute('SELECT * FROM payment_orders WHERE id=?', (order['id'],)).fetchone())
        if current['state'] in ('succeeded', 'canceled', 'failed', 'refunded'): return billing.public_order(current)
        if current['payment_id']:return billing.public_order(current)
        sub = db.execute('SELECT * FROM billing_subscriptions WHERE id=?', (current['subscription_id'],)).fetchone()
        charge = dict(db.execute('SELECT * FROM billing_subscription_charges WHERE order_id=?', (current['id'],)).fetchone())
        account=db.execute('SELECT enabled FROM users WHERE id=?',(sub['user_id'],)).fetchone()
        if not sub['auto_renew'] or sub['canceled_at'] or not enabled(settings) or not account or not account['enabled']:
            if charge['started_at']:
                # Replaying a POST with an unknown outcome could create its
                # first real charge after cancellation if the original request
                # never reached the gateway. Only GET/webhook/manual review is
                # permitted once consent or the operator switch is withdrawn.
                _stop(db,sub['id'],'review','payment_requires_review')
                return {**billing.public_order(current),'state':'review'}
            if not sub['auto_renew'] or sub['canceled_at'] or not account or not account['enabled']:
                _stop(db,sub['id'],'canceled','account_disabled' if not account or not account['enabled'] else None)
                db.execute("UPDATE payment_orders SET state='canceled' WHERE id=?", (current['id'],))
                return {**billing.public_order(current), 'state': 'canceled'}
            raise HTTPException(503, 'Новые списания по подпискам приостановлены.')
        if now - datetime.fromisoformat(current['created_at']) >= timedelta(hours=23):
            _stop(db, sub['id'], 'review', 'payment_requires_review')
            return {**billing.public_order(current), 'state': 'review'}
        # A lease avoids overlapping browser polls and workers. Same-key replay
        # remains safe if the process exits after sending an HTTP request.
        if charge['started_at'] and charge['next_attempt_at'] > now.isoformat():
            return {**billing.public_order(current), 'state': 'checking', 'retry_after': 10}
        db.execute('''UPDATE billing_subscription_charges SET started_at=coalesce(started_at,?),next_attempt_at=?,attempts=attempts+1
            WHERE order_id=?''', (now.isoformat(), (now + timedelta(seconds=45)).isoformat(), current['id']))
    try:
        value = provider.create_subscription(current, settings.public_origin, charge['payment_method_id'])
        return billing.apply_payment(repo, settings, value)
    except ProviderMismatch:
        with repo.connection() as db:
            db.execute('BEGIN IMMEDIATE'); _stop(db, sub['id'], 'review', 'payment_mismatch')
        raise HTTPException(502, 'Платёж требует проверки поддержки. Повторную оплату не создавайте.') from None
    except ProviderUnavailable as error:
        with repo.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if error.definitive:
                db.execute("UPDATE payment_orders SET state='failed',error_code=?,updated_at=? WHERE id=? AND state='creating'",
                           ('provider_rejected', accounts.now(), current['id']))
                _stop(db, sub['id'], 'past_due', 'provider_rejected')
            else:
                db.execute('UPDATE billing_subscription_charges SET next_attempt_at=? WHERE order_id=?',
                           ((now + timedelta(seconds=max(45, error.retry_after))).isoformat(), current['id']))
        return {**billing.public_order(current), 'state': 'failed' if error.definitive else 'checking', 'retry_after': 45}


def refund_verified(db, order):
    if not order['subscription_id']: return
    _stop(db, order['subscription_id'], 'canceled', 'payment_refunded')
    db.execute('UPDATE billing_subscription_periods SET refunded=1 WHERE order_id=?', (order['id'],))


def process_one(repo, settings):
    now = accounts.now()
    # Always reconcile already-sent payments even when new subscriptions are
    # disabled. Pausing sales must not lose a paid period or its receipt.
    with repo.connection() as db:
        row = db.execute('''SELECT o.* FROM payment_orders o JOIN billing_subscription_charges c ON c.order_id=o.id
            JOIN billing_subscriptions s ON s.id=c.subscription_id WHERE o.mode=? AND o.state IN ('creating','pending')
            AND s.state!='review' AND c.next_attempt_at<=?'''+('' if enabled(settings) else ' AND c.started_at IS NOT NULL')+
            ' ORDER BY o.created_at LIMIT 1', (settings.billing_mode, now)).fetchone()
    if row:
        if not enabled(settings):
            with repo.connection() as db:
                c = db.execute('SELECT started_at FROM billing_subscription_charges WHERE order_id=?', (row['id'],)).fetchone()
            if not c['started_at']: return False
        try:
            reconcile(repo, settings, dict(row))
            with repo.connection() as db:
                db.execute('UPDATE billing_subscription_charges SET next_attempt_at=? WHERE order_id=?',
                           ((datetime.now(timezone.utc)+timedelta(seconds=45)).isoformat(), row['id']))
        except (ProviderMismatch, ProviderUnavailable, HTTPException): pass
        return True
    if not enabled(settings): return False
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        sub = db.execute('''SELECT s.* FROM billing_subscriptions s JOIN users u ON u.id=s.user_id
            WHERE s.mode=? AND s.auto_renew=1 AND s.state='active' AND s.period_end_at<=?
            AND NOT EXISTS(SELECT 1 FROM payment_orders o WHERE o.subscription_id=s.id AND o.state IN ('creating','pending'))
            ORDER BY s.period_end_at LIMIT 1''', (settings.billing_mode, now)).fetchone()
        if not sub: return False
        sub = dict(sub)
        user = db.execute('SELECT enabled FROM users WHERE id=?', (sub['user_id'],)).fetchone()
        if not user or not user['enabled']:
            _stop(db, sub['id'], 'canceled', 'account_disabled'); return True
        if datetime.now(timezone.utc)-datetime.fromisoformat(sub['period_end_at']) > timedelta(days=1):
            _stop(db, sub['id'], 'past_due', 'renewal_missed'); return True
        period = db.execute('SELECT coalesce(max(period_number),0)+1 FROM billing_subscription_charges WHERE subscription_id=?', (sub['id'],)).fetchone()[0]
        order = _order(db, settings, sub, period)
    reconcile(repo, settings, order)
    return True


class Worker:
    def __init__(self, repo, settings): self.repo=repo; self.settings=settings; self.event=Event(); self.thread=None
    def start(self):
        if not self.settings.yookassa_secret_key: return
        self.thread=Thread(target=self.run, name='subscription-payments', daemon=True); self.thread.start()
    def stop(self):
        self.event.set()
        if self.thread: self.thread.join(timeout=30)
    def run(self):
        import logging
        while not self.event.wait(5):
            try: process_one(self.repo, self.settings)
            except Exception:
                logging.getLogger('uvicorn.error').warning('Subscription worker: reconciliation delayed')


def install(app, repo, settings):
    def demo_plan():
        from .pricing import checkout
        plans=checkout(repo,settings)['subscription_plans']
        if not plans:raise HTTPException(422,'Для демонстрации нужен хотя бы один доступный тариф подписки.')
        return plans[min(1,len(plans)-1)]

    def demo_wallet(user):
        with repo.connection() as db:
            row=db.execute('SELECT value FROM site_settings WHERE key=?',('billing_unlink_demo:'+user['id'],)).fetchone()
        saved=json.loads(row['value']) if row else {}
        plan=demo_plan();hours=period_hours(plan)
        stamp=accounts.now();end=(saved.get('ends_at') if saved.get('plan_version')==plan['id'] else None) or (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()
        canceled=saved.get('canceled_at')
        return {'demonstration':True,'balance':plan['credits'],'bonus_balance':0,'adjustment_due':0,'receipt_email':None,
            'test_mode':False,'orders':[],'subscription':{'current':{
                'id':'demonstration','plan_id':plan['id'],'credits':plan['credits'],'amount_kopecks':plan['renewal_amount_kopecks'],'period_hours':hours,
                'state':'canceled' if canceled else 'active','auto_renew':0 if canceled else 1,
                'period_end_at':end,'created_at':stamp,'canceled_at':canceled,'error_code':None,'unlimited':plan['unlimited']},
                'history':[],'unlimited_active':plan['unlimited']}}

    @app.get('/api/admin/billing/subscription-demo')
    def subscription_demo(request:Request):
        return JSONResponse(demo_wallet(accounts.admin_user(request)),headers={'Cache-Control':'private, no-store'})

    @app.post('/api/admin/billing/subscription-demo/{action}')
    def change_demo(action:str,request:Request):
        user=accounts.admin_user(request);accounts.mutation(request)
        if action not in ('cancel','reset'):raise HTTPException(404,'Действие демонстрации не найдено.')
        accounts.limited(repo,'billing-demo:'+user['id'],30,60)
        # This isolated setting never creates a subscription, payment, token,
        # credit or receipt. Actual cancellation uses the ordinary endpoint.
        plan=demo_plan()
        value={'ends_at':(datetime.now(timezone.utc)+timedelta(hours=period_hours(plan))).isoformat(),'plan_version':plan['id'],
               'canceled_at':accounts.now() if action=='cancel' else None}
        with repo.connection() as db:
            db.execute('INSERT OR REPLACE INTO site_settings VALUES (?,?,?)',
                ('billing_unlink_demo:'+user['id'],json.dumps(value),accounts.now()))
        return JSONResponse(demo_wallet(user),headers={'Cache-Control':'private, no-store'})

    @app.get('/api/billing/subscriptions')
    def subscriptions(request: Request):
        user=accounts.current_user(request)
        with repo.connection() as db: value=summary(db,user['id'],settings.billing_mode)
        return JSONResponse(value,headers={'Cache-Control':'private, no-store'})

    @app.post('/api/billing/subscriptions')
    async def subscribe(request: Request):
        from starlette.concurrency import run_in_threadpool
        user=accounts.current_user(request); accounts.mutation(request)
        if not accounts.agreements_current(repo,user['id']):
            raise HTTPException(403,'Подтвердите документы перед оплатой.',headers={'X-Legal-Required':'1'})
        accounts.limited(repo,'subscription:'+user['id'],8,60)
        return await run_in_threadpool(create,repo,settings,user,await accounts.body(request))

    @app.post('/api/billing/subscriptions/{identifier}/cancel')
    def unsubscribe(identifier: str, request: Request):
        user=accounts.current_user(request); accounts.mutation(request)
        accounts.limited(repo,'subscription-cancel:'+user['id'],20,60)
        return cancel(repo,settings,user,identifier)

    @app.get('/api/admin/subscriptions')
    def admin_subscriptions(request: Request):
        accounts.admin_user(request)
        with repo.connection() as db:
            rows=db.execute('''SELECT s.*,u.username FROM billing_subscriptions s LEFT JOIN users u ON u.id=s.user_id
                WHERE s.mode=? ORDER BY s.created_at DESC LIMIT 100''',(settings.billing_mode,)).fetchall()
        return {'enabled':enabled(settings),'items':[public(r)|{'username':r['username'],'user_id':r['user_id'],
            'consent_at':r['consent_at'],'recurring_version':r['recurring_version']} for r in rows]}
