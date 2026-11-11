"""Read-only administrator payment reports. No provider calls or payment actions."""
from collections import Counter,defaultdict
from datetime import datetime,timedelta,timezone
import re

from fastapi import HTTPException,Request
from . import accounts
from .bookmakers import instant

STATES=('creating','pending','succeeded','canceled','refunded','failed')
ORDER_FIELDS=('id','user_id','package_id','credits','amount_kopecks','state','created_at','updated_at',
              'payment_id','analysis_id','offer_version','refunded_kopecks','error_code','mode','username')
MSK=timezone(timedelta(hours=3))


def validate(days,mode,state,query,offset,limit):
    if not 0<=days<=365 or mode not in ('test','live') or state not in ('',*STATES) or len(query)>100 or not 0<=offset<=100000 or not 1<=limit<=100:
        raise HTTPException(422,'Некорректные фильтры отчёта об оплатах.')


def report(repo,*,days=30,mode='test',state='',query='',offset=0,limit=25):
    validate(days,mode,state,query,offset,limit)
    cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    with repo.connection() as db:
        rows=db.execute('''SELECT o.*,u.username FROM payment_orders o LEFT JOIN users u ON u.id=o.user_id
            WHERE o.mode=?'''+(' AND o.created_at>=?' if days else '')+' ORDER BY o.created_at DESC,o.id DESC',
            (mode,cutoff) if days else (mode,)).fetchall()
    needle=query.strip().casefold()
    rows=[dict(r) for r in rows]
    rows=[r for r in rows if (not state or r['state']==state) and (not needle or any(needle in str(r.get(k) or '').casefold() for k in ('id','username','user_id','payment_id')))]
    counts=Counter(r['state'] for r in rows)
    paid=[r for r in rows if r['state'] in ('succeeded','refunded')]
    gross=sum(r['amount_kopecks'] for r in paid);refunds=sum(r['refunded_kopecks'] for r in rows)
    daily=defaultdict(lambda:dict(orders=0,paid=0,gross_kopecks=0,refunded_kopecks=0))
    packages=defaultdict(lambda:dict(orders=0,paid=0,credits=0,gross_kopecks=0,refunded_kopecks=0))
    for row in rows:
        day=instant(row['created_at']).astimezone(MSK).date().isoformat()
        for bucket in (daily[day],packages[(row['package_id'],row['credits'])]):
            bucket['orders']+=1
            bucket['refunded_kopecks']+=row['refunded_kopecks']
            if row['state'] in ('succeeded','refunded'):
                bucket['paid']+=1;bucket['gross_kopecks']+=row['amount_kopecks']
        if row['state'] in ('succeeded','refunded'):packages[(row['package_id'],row['credits'])]['credits']+=row['credits']
    return {'format':'matchlab.admin-billing.v1','checked_at':accounts.now(),'mode':mode,'days':days,
        'metrics':{'orders':len(rows),'paid':len(paid),'buyers':len({r['user_id'] for r in paid}),
            'pending':counts['pending']+counts['creating'],'failed':counts['failed'],'canceled':counts['canceled'],
            'gross_kopecks':gross,'refunded_kopecks':refunds,'net_kopecks':gross-refunds,
            'average_check_kopecks':round(gross/len(paid)) if paid else None,
            'payment_rate':len(paid)/len(rows) if rows else None,'credited_units':sum(r['credits'] for r in paid)},
        'statuses':dict(counts),'daily':[{'day':k,**v} for k,v in sorted(daily.items())],
        'packages':[{'id':k[0],'units':k[1],**v} for k,v in sorted(packages.items())],
        'orders':[{k:r.get(k) for k in ORDER_FIELDS} for r in rows[offset:offset+limit]],
        'offset':offset,'limit':limit,'total':len(rows),
        'notice':'Тестовые операции. Реальные деньги не списывались.' if mode=='test' else 'Только реальные платежи. Боевой приём оплаты пока не открыт.',
        'methodology':'Отбор по дате создания заказа. Показан его текущий подтверждённый статус; возвраты относятся к выбранным заказам. Сумма после возвратов не является прибылью. Историческое время фактической оплаты отдельно не записывалось; updated_at — время обновления записи, не дата оплаты.'}


def detail(repo,identifier,offset=0,limit=30):
    if not re.fullmatch(r'[a-f0-9]{32}',identifier) or not 0<=offset<=100000 or not 1<=limit<=100:raise HTTPException(422,'Некорректный заказ или страница операций.')
    with repo.connection() as db:
        db.execute('BEGIN')
        row=db.execute('''SELECT o.*,u.username FROM payment_orders o LEFT JOIN users u ON u.id=o.user_id WHERE o.id=?''',(identifier,)).fetchone()
        if not row:raise HTTPException(404,'Заказ не найден.')
        row=dict(row)
        refunds=[dict(r) for r in db.execute('SELECT id,amount_kopecks,at FROM payment_refunds WHERE order_id=? ORDER BY at,id',(identifier,))]
        ledger=[dict(r) for r in db.execute('''SELECT id,delta,kind,reference,at FROM analysis_credit_ledger
            WHERE user_id=? AND mode=? ORDER BY at DESC,id DESC LIMIT ? OFFSET ?''',(row['user_id'],row['mode'],limit,offset))]
        paid=db.execute('SELECT coalesce(sum(delta),0) FROM analysis_credit_ledger WHERE user_id=? AND mode=?',(row['user_id'],row['mode'])).fetchone()[0]
        bonus=db.execute('SELECT bonus_analyses FROM users WHERE id=?',(row['user_id'],)).fetchone()
        total=db.execute('SELECT count(*) FROM analysis_credit_ledger WHERE user_id=? AND mode=?',(row['user_id'],row['mode'])).fetchone()[0]
        opens=db.execute('SELECT count(*) FROM analysis_unlocks WHERE user_id=? AND mode=?',(row['user_id'],row['mode'])).fetchone()[0]
    return {'order':{k:row.get(k) for k in ORDER_FIELDS},'refunds':refunds,'ledger':ledger,'ledger_total':total,'offset':offset,'limit':limit,
        'wallet':{'ledger_units':paid,'bonus_units':bonus[0] if bonus else 0,'balance':paid+(bonus[0] if bonus else 0),'opened_answers':opens},
        'ledger_notice':'Операции всего аккаунта в выбранном режиме. Единицы разных пакетов объединены; списания не приписываются конкретному заказу. Бонусный остаток показан отдельно.'}


def install(app,repo):
    @app.get('/api/admin/billing')
    def summary(request:Request,days:int=30,mode:str='test',state:str='',query:str='',offset:int=0,limit:int=25):
        accounts.admin_user(request)
        return report(repo,days=days,mode=mode,state=state,query=query,offset=offset,limit=limit)

    @app.get('/api/admin/billing/orders/{identifier}')
    def order(request:Request,identifier:str,offset:int=0,limit:int=30):
        accounts.admin_user(request)
        return detail(repo,identifier,offset,limit)
