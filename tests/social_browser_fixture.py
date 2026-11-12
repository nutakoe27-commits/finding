"""Loopback-only OAuth fixture. Provider pages/API are faked, never real login."""
import os
from pathlib import Path

from fastapi import Request
import httpx
from matchlab import accounts,social_login
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository

settings=Settings(Path(os.environ['MATCHLAB_DATA_DIR']),public_origin=os.environ['PUBLIC_ORIGIN'],
    yandex_client_id='a'*32,yandex_client_secret='fake-browser-secret',vkid_client_id='51400001')
repo=Repository(settings.database);repo.initialize()


def provider(method,url,**kw):
    if url.endswith('/token') or url.endswith('/oauth2/auth'):
        subject=kw['data']['code'].removeprefix('SECRET_BROWSER_CODE_')
        value={'access_token':'fake_token_'+subject,'user_id':subject,'state':kw['data'].get('state')}
    elif url.endswith('/info'):
        subject=kw['headers']['Authorization'].rsplit('_',1)[-1]
        value={'id':subject,'client_id':'a'*32,'display_name':'Михаил'}
    elif url.endswith('/oauth2/user_info'):
        subject=kw['data']['access_token'].rsplit('_',1)[-1];value={'user':{'user_id':subject,'first_name':'Михаил'}}
    else:raise AssertionError('Unexpected fake provider URL')
    return httpx.Response(200,json=value,request=httpx.Request(method,url))


social_login.httpx.request=provider
app=create_app(settings)


@app.post('/fixture/bonus')
def bonus(request:Request):
    user=accounts.current_user(request)
    with repo.connection() as db:db.execute('UPDATE users SET bonus_analyses=7 WHERE id=?',(user['id'],))
    return {'ok':True}


@app.post('/fixture/admin')
def admin(request:Request):
    user=accounts.current_user(request)
    with repo.connection() as db:db.execute("UPDATE users SET role='admin' WHERE id=?",(user['id'],))
    return {'ok':True}
