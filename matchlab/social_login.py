"""Server-side OAuth code/PKCE login. No provider tokens in the browser or DB."""
import base64
from datetime import datetime,timedelta,timezone
import hashlib
import hmac
import logging
import re
import secrets
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse,RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import accounts

LABELS={'local':'Логин и пароль','yandex':'Яндекс','vk':'ВК','ok':'ОК','mail':'Mail.ru'}
VK_PROVIDERS={'vk':'vkid','ok':'ok_ru','mail':'mail_ru'}
SCHEMA='''
CREATE TABLE IF NOT EXISTS social_login_attempts (
 state_hash TEXT PRIMARY KEY,provider TEXT NOT NULL,client_id TEXT NOT NULL,browser_hash TEXT NOT NULL,
 verifier TEXT,return_to TEXT NOT NULL,action TEXT NOT NULL,link_user_id TEXT,link_session_hash TEXT,
 status TEXT NOT NULL,subject TEXT,display_name TEXT,created_at TEXT NOT NULL,expires_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS social_attempts_expire ON social_login_attempts(expires_at);
CREATE TABLE IF NOT EXISTS social_login_sources (
 user_id TEXT NOT NULL,provider TEXT NOT NULL,subject TEXT NOT NULL,at TEXT NOT NULL,
 PRIMARY KEY(user_id,provider,subject));
'''
LOG=logging.getLogger('uvicorn.error')


class AuthFailure(Exception):pass


def digest(value):return hashlib.sha256(value.encode()).hexdigest()


def current_time():return datetime.now(timezone.utc)


def configured(settings,provider):
    if provider=='yandex':return bool(settings.yandex_client_id and settings.yandex_client_secret)
    return provider in VK_PROVIDERS and bool(settings.vkid_client_id)


def identity_provider(provider):return 'yandex' if provider=='yandex' else 'vkid'


def callback_url(request,provider):return accounts.origin(request)+'/api/auth/social/'+identity_provider(provider)+'/callback'


def cookie_name(request,pending=False):
    return ('__Host-' if accounts.origin(request).startswith('https://') else '')+'prognozich-social'+('-pending' if pending else '')


def cookie(response,request,value,pending=False):
    response.set_cookie(cookie_name(request,pending),value,max_age=600,httponly=True,
        secure=accounts.origin(request).startswith('https://'),samesite='lax',path='/')


def clear_cookies(response,request):
    for pending in (False,True):response.delete_cookie(cookie_name(request,pending),path='/',httponly=True,
        secure=accounts.origin(request).startswith('https://'),samesite='lax')


def invalidate(db,request):
    browser=request.cookies.get(cookie_name(request))
    if browser:db.execute('DELETE FROM social_login_attempts WHERE browser_hash=?',(digest(browser),))


def safe_return(value):
    return value if isinstance(value,str) and re.fullmatch(r'#(?:matches|account|admin|results|analysis/[a-f0-9]{32}|result/[a-f0-9]{32})',value) else '#matches'


def landing(request,code,return_to='#matches'):
    return RedirectResponse(accounts.origin(request)+'/?'+urlencode({'social':code})+safe_return(return_to),status_code=303,
        headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})


def request_json(method,url,**kw):
    try:
        response=httpx.request(method,url,timeout=20,follow_redirects=False,**kw)
        response.raise_for_status();value=response.json()
        if not isinstance(value,dict) or value.get('error'):raise ValueError
        return value
    except (httpx.HTTPError,ValueError):raise AuthFailure('provider_unavailable') from None


def checked_text(value,maximum=8192):
    if not isinstance(value,str) or not 1<=len(value)<=maximum or any(not 33<=ord(c)<=126 for c in value):raise AuthFailure('invalid_response')
    return value


def fetch_identity(settings,attempt,query,redirect):
    code=checked_text(query.get('code'));provider=attempt['provider']
    data={'grant_type':'authorization_code','code':code,'redirect_uri':redirect,
        'code_verifier':attempt['verifier'],'client_id':attempt['client_id']}
    if provider=='yandex':
        if settings.yandex_client_id!=attempt['client_id']:raise AuthFailure('configuration_changed')
        token=request_json('POST','https://oauth.yandex.ru/token',data=data,
            auth=(settings.yandex_client_id,settings.yandex_client_secret))
        access=checked_text(token.get('access_token'))
        info=request_json('GET','https://login.yandex.ru/info',params={'format':'json'},headers={'Authorization':'OAuth '+access})
        if info.get('client_id') is not None and info['client_id']!=settings.yandex_client_id:raise AuthFailure('invalid_response')
        subject=str(info.get('id') or '')
        name=info.get('display_name') or info.get('first_name') or info.get('login')
    else:
        if settings.vkid_client_id!=attempt['client_id']:raise AuthFailure('configuration_changed')
        data.update(device_id=checked_text(query.get('device_id'),1024),state=query['state'])
        token=request_json('POST','https://id.vk.ru/oauth2/auth',data=data)
        if not isinstance(token.get('state'),str) or not hmac.compare_digest(token['state'].encode(),query['state'].encode()):raise AuthFailure('invalid_response')
        access=checked_text(token.get('access_token'))
        info=request_json('POST','https://id.vk.ru/oauth2/user_info',data={'client_id':attempt['client_id'],'access_token':access})
        user=info.get('user')
        if not isinstance(user,dict) or str(user.get('user_id') or '')!=str(token.get('user_id') or ''):raise AuthFailure('invalid_response')
        subject=str(user.get('user_id') or '')
        name=user.get('first_name')
    if not re.fullmatch(r'[0-9]{1,30}',subject):raise AuthFailure('invalid_response')
    if provider!='yandex':subject=attempt['client_id']+':'+subject
    name=re.sub(r'[\x00-\x1f\x7f]','',name)[:64].strip() if isinstance(name,str) else ''
    return subject,name or 'Пользователь'


def session_redirect(request,user_id,provider,return_to):
    repo=request.app.state.repository
    # Rotate a prior local session when explicitly linking an identity.
    old=request.cookies.get(accounts.cookie_name(request))
    with repo.connection() as db:
        if old:db.execute('DELETE FROM user_sessions WHERE token_hash=?',(accounts.token_hash(old),))
        db.execute('UPDATE users SET last_login_provider=? WHERE id=?',(provider,user_id))
    token=accounts.make_session(repo,user_id);response=landing(request,'success',return_to)
    response.set_cookie(accounts.cookie_name(request),token,max_age=7*86400,httponly=True,
        secure=accounts.origin(request).startswith('https://'),samesite='lax',path='/')
    clear_cookies(response,request);return response


def bound_attempt(db,request,state,status):
    if not isinstance(state,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',state):raise AuthFailure('expired')
    row=db.execute('SELECT * FROM social_login_attempts WHERE state_hash=?',(digest(state),)).fetchone()
    browser=request.cookies.get(cookie_name(request),'')
    if not row or row['status']!=status or row['expires_at']<=accounts.now() or not browser or not hmac.compare_digest(row['browser_hash'],digest(browser)):
        raise AuthFailure('expired')
    return dict(row)


def install(app,repo,settings):
    @app.get('/api/auth/social/providers')
    def providers():
        return {'providers':[{'id':p,'label':LABELS[p],'enabled':configured(settings,p)} for p in ('yandex','vk','ok','mail')]}

    @app.post('/api/auth/social/{provider}/start')
    async def start(provider:str,request:Request):
        accounts.mutation(request,False)
        if not configured(settings,provider):raise HTTPException(503,'Вход через этот сервис пока недоступен.')
        accounts.limited(repo,'social-start:'+accounts.peer(request),15,60)
        body=await accounts.body(request,8192);action=body.get('action','login')
        user=accounts.current_user(request,False)
        if action not in ('login','link'):raise HTTPException(422,'Некорректный способ входа.')
        if action=='link':
            if not user:raise HTTPException(401,'Сначала войдите в свой аккаунт.')
            accounts.mutation(request)
            if str(user['password_hash']).startswith('scrypt$'):
                password=body.get('password')
                if not isinstance(password,str) or not 12<=len(password)<=128 or not await run_in_threadpool(accounts.password_matches,password,user['password_hash']):
                    raise HTTPException(403,'Подтвердите текущий пароль для подключения способа входа.')
            elif current_time()-datetime.fromisoformat(user['session_created_at'])>timedelta(minutes=10):
                raise HTTPException(403,'Для подключения нового способа войдите в аккаунт заново.')
        elif user:raise HTTPException(409,'Вы уже вошли. Подключите способ входа в настройках аккаунта.')
        state=secrets.token_urlsafe(32);browser=request.cookies.get(cookie_name(request)) or secrets.token_urlsafe(32)
        verifier=secrets.token_urlsafe(48);challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        client=settings.yandex_client_id if provider=='yandex' else settings.vkid_client_id
        at=accounts.now();expiry=(current_time()+timedelta(minutes=10)).isoformat(timespec='microseconds')
        with repo.connection() as db:
            db.execute('DELETE FROM social_login_attempts WHERE expires_at<=?',(at,))
            db.execute('DELETE FROM social_login_attempts WHERE browser_hash=?',(digest(browser),))
            db.execute('''INSERT INTO social_login_attempts(state_hash,provider,client_id,browser_hash,verifier,return_to,action,
                link_user_id,link_session_hash,status,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,'created',?,?)''',
                (digest(state),provider,client,digest(browser),verifier,safe_return(body.get('return_to')),action,
                 user['id'] if action=='link' else None,user['session_hash'] if action=='link' else None,at,expiry))
        params={'response_type':'code','client_id':client,'redirect_uri':callback_url(request,provider),
            'state':state,'code_challenge':challenge,'code_challenge_method':'S256'}
        if provider=='yandex':
            params['scope']='login:info';url='https://oauth.yandex.ru/authorize'
            if action=='link':params['force_confirm']='yes'
        else:
            params.update(provider=VK_PROVIDERS[provider],scope='vkid.personal_info')
            # As in VK ID's multibranding flow, select the external provider;
            # its own web/app SSO handles the user's already authorised account.
            prompts=['login'] if provider!='vk' else []
            if action=='link':prompts.append('select_account')
            if prompts:params['prompt']=' '.join(prompts)
            url='https://id.vk.ru/authorize'
        response=JSONResponse({'authorization_url':url+'?'+urlencode(params)},headers={'Cache-Control':'no-store'})
        cookie(response,request,browser);return response

    @app.get('/api/auth/social/{callback_provider}/callback')
    async def callback(callback_provider:str,request:Request):
        if callback_provider not in ('yandex','vkid'):raise HTTPException(404,'Страница входа не найдена.')
        query=getattr(request.state,'social_query',{});return_to='#matches';bound=False
        try:
            with repo.connection() as db:
                db.execute('BEGIN IMMEDIATE');attempt=bound_attempt(db,request,query.get('state'),'created')
                bound=True
                return_to=attempt['return_to']
                if identity_provider(attempt['provider'])!=callback_provider:raise AuthFailure('expired')
                db.execute("UPDATE social_login_attempts SET status='processing',verifier=NULL WHERE state_hash=?",(attempt['state_hash'],))
            if query.get('error'):raise AuthFailure('cancelled')
            user=accounts.current_user(request,False)
            if attempt['action']=='link':
                if not user or user['id']!=attempt['link_user_id'] or user['session_hash']!=attempt['link_session_hash']:raise AuthFailure('session_changed')
            elif user:raise AuthFailure('session_changed')
            subject,name=await run_in_threadpool(fetch_identity,settings,attempt,query,callback_url(request,attempt['provider']))
            canonical=identity_provider(attempt['provider'])
            with repo.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                active=db.execute("SELECT status,expires_at FROM social_login_attempts WHERE state_hash=?",(attempt['state_hash'],)).fetchone()
                if not active or active['status']!='processing' or active['expires_at']<=accounts.now():raise AuthFailure('expired')
                if attempt['action']=='link' and not db.execute('''SELECT 1 FROM user_sessions s JOIN users u ON u.id=s.user_id
                    WHERE u.id=? AND u.enabled=1 AND s.token_hash=? AND s.expires_at>?''',
                    (attempt['link_user_id'],attempt['link_session_hash'],accounts.now())).fetchone():raise AuthFailure('session_changed')
                existing=db.execute('''SELECT u.* FROM user_identities i JOIN users u ON u.id=i.user_id
                    WHERE i.provider=? AND i.subject=?''',(canonical,subject)).fetchone()
                if existing and not existing['enabled']:raise AuthFailure('account_disabled')
                if attempt['action']=='link':
                    if existing and existing['id']!=user['id']:raise AuthFailure('identity_in_use')
                    if not existing:db.execute('INSERT INTO user_identities VALUES (?,?,?)',(canonical,subject,user['id']))
                    accounts.audit(db,user['id'],'account.identity_linked',user['id'],{'provider':attempt['provider']})
                    identifier=user['id']
                elif existing:identifier=existing['id']
                else:
                    db.execute("""UPDATE social_login_attempts SET subject=?,display_name=?,status='pending',expires_at=?
                        WHERE state_hash=?""",(subject,name,(current_time()+timedelta(minutes=10)).isoformat(timespec='microseconds'),attempt['state_hash']))
                    identifier=None
                if identifier:
                    db.execute('INSERT OR IGNORE INTO social_login_sources VALUES (?,?,?,?)',(identifier,attempt['provider'],subject,accounts.now()))
                    invalidate(db,request)
            if identifier:return session_redirect(request,identifier,attempt['provider'],return_to)
            response=landing(request,'complete',return_to)
            cookie(response,request,request.cookies[cookie_name(request)])
            cookie(response,request,query['state'],True);return response
        except AuthFailure as exc:
            code=str(exc)
            if code not in ('expired','cancelled','session_changed','identity_in_use','account_disabled'):code='unavailable'
            response=landing(request,code,return_to)
            if bound:clear_cookies(response,request)
            return response

    @app.get('/api/auth/social/pending')
    def pending(request:Request):
        try:
            with repo.connection() as db:attempt=bound_attempt(db,request,request.cookies.get(cookie_name(request,True)),'pending')
        except AuthFailure:raise HTTPException(409,'Время регистрации истекло. Повторите вход через выбранный сервис.') from None
        return {'provider':attempt['provider'],'label':LABELS[attempt['provider']],'display_name':attempt['display_name'],
            'legal_version':accounts.LEGAL_VERSION}

    @app.post('/api/auth/social/complete')
    async def complete(request:Request):
        accounts.mutation(request,False);body=await accounts.body(request,4096);accounts.validate_agreements(body)
        if accounts.current_user(request,False):raise HTTPException(409,'Вы уже вошли. Используйте подключение способа входа в аккаунте.')
        try:
            with repo.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                attempt=bound_attempt(db,request,request.cookies.get(cookie_name(request,True)),'pending')
                canonical=identity_provider(attempt['provider'])
                existing=db.execute('''SELECT u.id,u.enabled FROM user_identities i JOIN users u ON u.id=i.user_id
                    WHERE i.provider=? AND i.subject=?''',(canonical,attempt['subject'])).fetchone()
                if existing:
                    if not existing['enabled']:raise AuthFailure('account_disabled')
                    identifier=existing['id']
                else:
                    identifier=uuid4().hex;username=('ya_' if canonical=='yandex' else 'vk_')+identifier[:20]
                    db.execute('''INSERT INTO users(id,username,password_hash,role,created_at,display_name,last_login_provider)
                        VALUES (?,?,?,'user',?,?,?)''',(identifier,username,'social-only$'+secrets.token_hex(16),accounts.now(),attempt['display_name'],attempt['provider']))
                    db.execute('INSERT INTO user_identities VALUES (?,?,?)',(canonical,attempt['subject'],identifier))
                accounts.save_agreements(db,identifier)
                db.execute('INSERT OR IGNORE INTO social_login_sources VALUES (?,?,?,?)',(identifier,attempt['provider'],attempt['subject'],accounts.now()))
                db.execute('UPDATE users SET last_login_provider=? WHERE id=?',(attempt['provider'],identifier))
                invalidate(db,request)
        except AuthFailure:raise HTTPException(409,'Регистрация недоступна. Повторите вход или обратитесь в поддержку.') from None
        response=accounts.authenticated_response(request,identifier)
        clear_cookies(response,request);return response

    @app.get('/api/auth/social/connections')
    def connections(request:Request):
        user=accounts.current_user(request)
        with repo.connection() as db:identities=[dict(r) for r in db.execute('SELECT provider,subject FROM social_login_sources WHERE user_id=?',(user['id'],))]
        return {'connections':[{'id':p,'label':LABELS[p],'enabled':configured(settings,p),
            'linked':any(i['provider']==p and (p=='yandex' or i['subject'].startswith(settings.vkid_client_id+':')) for i in identities)}
            for p in ('yandex','vk','ok','mail')]}
