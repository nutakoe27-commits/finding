"""Versioned catalogue. Publishing never rewrites an existing payment contract."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import hashlib
import json
import re
from uuid import uuid4

from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse
from . import accounts

CURRENT='pricing:current'
DRAFT='pricing:draft'
VERSION_PREFIX='pricing:version:'
MSK=timezone(timedelta(hours=3))


def encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()


def defaults():
    from .billing import PACKAGES
    from .subscriptions import PLANS
    def item(p,sub=False):
        return {'key':p['id'],'credits':p['credits'],'amount_kopecks':p['amount_kopecks'],
            'period_days':p['period_hours']//24 if sub else None,'enabled':True,
            'discount':{'percent':0,'starts_at':None,'ends_at':None}}
    return {'revision':'builtin','published_at':None,'subscriptions':[item(p,True) for p in PLANS],
            'packages':[item(p) for p in PACKAGES]}


def load(repo):
    with repo.connection() as db:row=db.execute('SELECT value FROM site_settings WHERE key=?',(CURRENT,)).fetchone()
    return json.loads(row['value']) if row else defaults()


def instant(value):
    if value is None or value=='':return None
    if not isinstance(value,str):raise HTTPException(422,'Проверьте даты акции.')
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None or not 1970<=result.year<=2100:raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError,OverflowError):raise HTTPException(422,'У даты акции должен быть указан часовой пояс.') from None


def validate(value):
    if not isinstance(value,dict) or set(value)-{'subscriptions','packages'}:raise HTTPException(422,'Некорректный каталог тарифов.')
    clean={};keys=set()
    for kind in ('subscriptions','packages'):
        rows=value.get(kind)
        if not isinstance(rows,list) or len(rows)>8:raise HTTPException(422,'В одном разделе допускается не более 8 тарифов.')
        clean[kind]=[]
        for row in rows:
            if not isinstance(row,dict) or set(row)-{'key','credits','amount_kopecks','period_days','enabled','discount'}:raise HTTPException(422,'Некорректные поля тарифа.')
            key=row.get('key');n=row.get('credits');amount=row.get('amount_kopecks');days=row.get('period_days');on=row.get('enabled')
            if not isinstance(key,str) or not re.fullmatch('[a-z][a-z0-9_]{0,47}',key) or key in keys:raise HTTPException(422,'Идентификаторы тарифов должны быть уникальными.')
            keys.add(key)
            if type(n) is not int or not (0 if kind=='subscriptions' else 1)<=n<=10000:raise HTTPException(422,'Укажите количество разборов от 1 до 10 000; 0 — безлимитная подписка.')
            if type(amount) is not int or not max(100,n)<=amount<=100000000:raise HTTPException(422,'Цена — от 1 до 1 000 000 ₽, минимум одна копейка на разбор.')
            if kind=='subscriptions' and (type(days) is not int or not 1<=days<=365):raise HTTPException(422,'Период подписки — от 1 до 365 дней.')
            if kind=='packages' and days is not None:raise HTTPException(422,'У разового пакета нет периода списания.')
            if type(on) is not bool:raise HTTPException(422,'Укажите доступность тарифа.')
            discount=row.get('discount')
            if not isinstance(discount,dict) or set(discount)-{'percent','starts_at','ends_at'}:raise HTTPException(422,'Проверьте скидку.')
            percent=discount.get('percent')
            if type(percent) is not int or not 0<=percent<=95:raise HTTPException(422,'Скидка — целое число от 0 до 95%.')
            start=instant(discount.get('starts_at'));end=instant(discount.get('ends_at'))
            if start and end and end<=start:raise HTTPException(422,'Окончание акции должно быть позже начала.')
            if amount*(100-percent)//100<max(100,n):raise HTTPException(422,'Цена со скидкой должна быть не меньше 1 ₽ и одной копейки на разбор.')
            clean[kind].append({'key':key,'credits':n,'amount_kopecks':amount,'period_days':days,'enabled':on,
                'discount':{'percent':percent,'starts_at':start.isoformat() if start else None,'ends_at':end.isoformat() if end else None}})
    if not any(p['enabled'] for rows in clean.values() for p in rows):raise HTTPException(422,'Оставьте доступным хотя бы один тариф или разовый пакет.')
    return clean


def draft_of(catalog):return {k:deepcopy(catalog[k]) for k in ('subscriptions','packages')}


def document_url(version,document):
    match=re.fullmatch(r'[0-9-]+\.pricing\.([a-f0-9]{32})(?:\.test|\.recurring)?',str(version))
    if match:return '/legal/versions/'+match[1]+'/'+document
    if document=='recurring' and version=='2026-11-12.3days.v1':
        return '/static/legal/archive/recurring-2026-11-12-v4.html'
    if document=='recurring' and version=='2026-11-12.daily.v1':
        return '/static/legal/archive/recurring-2026-11-12-v3.html'
    if version in ('2026-11-12.v4','2026-11-12.test.v3'):
        return '/static/legal/archive/'+document+'-2026-11-12-'+('test-v3' if document=='test-purchase' else 'v4')+'.html'
    return '/static/legal/'+document+'.html'


def effective(catalog,kind,now=None):
    now=now or datetime.now(timezone.utc);result=[]
    for p in catalog[kind]:
        if not p['enabled']:continue
        d=p['discount'];start=instant(d['starts_at']);end=instant(d['ends_at'])
        sale=bool(d['percent'] and (not start or start<=now) and (not end or now<end))
        base=p['amount_kopecks'];amount=base*(100-d['percent'])//100 if sale else base
        if catalog['revision']=='builtin':identifier=p['key']
        else:
            key=hashlib.sha256(p['key'].encode()).hexdigest()[:12]
            identifier=('sub_'+str(p['period_days']*24) if kind=='subscriptions' else 'pkg')+'_'+catalog['revision']+'_'+key+('_d' if sale else '_r')
        result.append({'id':identifier,'credits':p['credits'],'amount_kopecks':amount,'original_amount_kopecks':base,
            'renewal_amount_kopecks':base,'period_hours':p['period_days']*24 if kind=='subscriptions' else None,
            'unlimited':p['credits']==0,'discount_percent':d['percent'] if sale else 0,
            'discount_ends_at':d['ends_at'] if sale else None})
    return result


def versions(catalog,settings):
    from . import billing,subscriptions
    if catalog['revision']=='builtin':return billing.offer_version(settings),subscriptions.VERSION
    date=datetime.fromisoformat(catalog['published_at']).astimezone(MSK).date().isoformat()
    version=date+'.pricing.'+catalog['revision']
    return version+('.test' if settings.billing_mode=='test' else ''),version+'.recurring'


def checkout(repo,settings):
    catalog=load(repo);offer,recurring=versions(catalog,settings)
    from .subscriptions import consent_text
    subs=[p|{'consent_text':consent_text(p)} for p in effective(catalog,'subscriptions')]
    return {'catalog':catalog,'revision':catalog['revision'],'packages':effective(catalog,'packages'),
            'subscription_plans':subs,'offer_version':offer,'recurring_version':recurring}


def preview_payload(catalog,body):
    if not isinstance(body,dict) or body.get('base_revision')!=catalog['revision']:raise HTTPException(409,'Тарифы уже изменены. Загрузите актуальную версию перед публикацией.')
    return validate(body.get('draft'))


def publish(repo,settings,user,body):
    from .pricing_documents import DOCUMENTS,render
    current=load(repo);clean=preview_payload(current,body)
    expected=digest({'base_revision':current['revision'],'draft':clean})
    if body.get('preview_hash')!=expected:raise HTTPException(422,'Сначала откройте предпросмотр этих изменений.')
    catalog=clean|{'revision':uuid4().hex,'published_at':accounts.now()}
    documents={name:render(name,catalog,archive=True) for name in DOCUMENTS}
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        # Conditional replacement prevents a second administrator from silently
        # overwriting a publication made while their editor was open.
        old=db.execute('SELECT value FROM site_settings WHERE key=?',(CURRENT,)).fetchone()
        if (json.loads(old['value'])['revision'] if old else 'builtin')!=current['revision']:raise HTTPException(409,'Тарифы уже изменены другим администратором.')
        stamp=catalog['published_at'];serialized=encode(catalog)
        if old:
            changed=db.execute('UPDATE site_settings SET value=?,updated_at=? WHERE key=? AND value=?',(serialized,stamp,CURRENT,old['value'])).rowcount
        else:changed=db.execute('INSERT OR IGNORE INTO site_settings VALUES (?,?,?)',(CURRENT,serialized,stamp)).rowcount
        if changed!=1:raise HTTPException(409,'Тарифы уже изменены другим администратором.')
        db.execute('INSERT INTO site_settings VALUES (?,?,?)',(VERSION_PREFIX+catalog['revision'],serialized,stamp))
        for name,html in documents.items():db.execute('INSERT INTO site_settings VALUES (?,?,?)',('pricing:document:'+catalog['revision']+':'+name,html,stamp))
        db.execute('DELETE FROM site_settings WHERE key=?',(DRAFT+':'+user['id'],))
        accounts.audit(db,user['id'],'pricing.published',catalog['revision'],{'subscriptions':len(clean['subscriptions']),'packages':len(clean['packages'])})
    return catalog


def install(app,repo,settings):
    @app.get('/api/admin/pricing')
    def admin_catalog(request:Request):
        user=accounts.admin_user(request);current=load(repo)
        with repo.connection() as db:
            draft=db.execute('SELECT value FROM site_settings WHERE key=?',(DRAFT+':'+user['id'],)).fetchone()
            rows=db.execute('SELECT value FROM site_settings WHERE key LIKE ? ORDER BY updated_at DESC LIMIT 20',(VERSION_PREFIX+'%',)).fetchall()
        history=[{'revision':v['revision'],'published_at':v['published_at']} for v in (json.loads(r['value']) for r in rows)]
        saved=json.loads(draft['value']) if draft else None
        return JSONResponse({'current':current,'draft':saved,'history':history},headers={'Cache-Control':'private, no-store'})

    @app.post('/api/admin/pricing/draft')
    async def save_draft(request:Request):
        user=accounts.admin_user(request);accounts.mutation(request);body=await accounts.body(request)
        current=load(repo);clean=preview_payload(current,body)
        with repo.connection() as db:db.execute('INSERT OR REPLACE INTO site_settings VALUES (?,?,?)',(DRAFT+':'+user['id'],encode({'base_revision':current['revision'],'draft':clean}),accounts.now()))
        return {'ok':True}

    @app.post('/api/admin/pricing/preview')
    async def preview(request:Request):
        accounts.admin_user(request);accounts.mutation(request);body=await accounts.body(request)
        current=load(repo);clean=preview_payload(current,body)
        from .pricing_documents import DOCUMENTS,render
        candidate=clean|{'revision':current['revision'],'published_at':accounts.now()}
        return {'preview_hash':digest({'base_revision':current['revision'],'draft':clean}),
            'subscriptions':effective(candidate,'subscriptions'),'packages':effective(candidate,'packages'),
            'documents':{name:render(name,candidate,preview=True) for name in DOCUMENTS}}

    @app.post('/api/admin/pricing/publish')
    async def publish_catalog(request:Request):
        user=accounts.admin_user(request,elevated=True);accounts.mutation(request)
        accounts.limited(repo,'pricing-publish:'+user['id'],20,60)
        return {'current':publish(repo,settings,user,await accounts.body(request))}
