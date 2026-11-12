"""Mobile/desktop social and native login against entirely fake provider pages."""
import asyncio
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import parse_qs,urlsplit,urlencode

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))


async def exercise(base):
    from playwright.async_api import async_playwright,expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for width,theme in ((1440,'light'),(390,'dark'),(320,'light')):
                context=await browser.new_context(viewport={'width':width,'height':1000},color_scheme=theme,locale='ru-RU',reduced_motion='reduce',extra_http_headers={'X-Real-IP':'192.0.2.'+str(width%250+1)})
                page=await context.new_page();page.set_default_timeout(12000);errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                subject=str(1000000+width)
                async def authorise(route):
                    q={k:v[0] for k,v in parse_qs(urlsplit(route.request.url).query).items()}
                    target=q['redirect_uri']+'?'+urlencode({'state':q['state'],'code':'SECRET_BROWSER_CODE_'+subject,'device_id':'fake-device'})
                    await route.fulfill(status=302,headers={'Location':target})
                await page.route('https://oauth.yandex.ru/authorize?*',authorise)
                await page.route('https://id.vk.ru/authorize?*',authorise)
                await page.goto(base+'/#matches')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await page.locator('#account-button').click()
                await expect(page.locator('#auth-form')).to_be_hidden()
                await expect(page.locator('#social-yandex')).to_have_text('Войти через Яндекс')
                await expect(page.locator('#social-icon-row button:visible')).to_have_count(3)
                assert all(not text.strip() for text in await page.locator('#social-icon-row button').all_text_contents())
                await page.screenshot(path=f'/workspace/outputs/social-login-{width}.png',full_page=True)
                await page.locator('#social-yandex').click()
                await expect(page.locator('#social-completion')).to_be_visible()
                await expect(page.locator('#auth-form')).to_be_hidden()
                for key in ('social-terms','social-data','social-age'):await page.locator('#'+key).check()
                await page.locator('#social-complete-submit').click()
                await expect(page.locator('#auth-dialog')).to_be_hidden()
                await expect(page.locator('#account-button')).to_have_text('Михаил')
                await page.goto(base+'/#account')
                await page.locator('[data-profile-tab="settings"]').click()
                await expect(page.locator('#account-password-card')).to_be_hidden()
                await expect(page.locator('#account-social-card')).to_be_visible()
                await page.locator('#account-logout').click()
                family=None
                subject=str(3000000+width)
                for provider in ('vk','ok','mail'):
                    await page.locator('#account-button').click();await page.locator('[data-social="'+provider+'"]').click()
                    if family is None:
                        await expect(page.locator('#social-completion')).to_be_visible()
                        for key in ('social-terms','social-data','social-age'):await page.locator('#'+key).check()
                        await page.locator('#social-complete-submit').click()
                    await expect(page.locator('#auth-dialog')).to_be_hidden()
                    await expect(page.locator('#account-button')).to_have_text('Михаил')
                    me=await (await context.request.get(base+'/api/auth/me')).json()
                    if family is None:family=me['user']['id']
                    assert me['user']['id']==family and me['user']['login_provider']==provider
                    await page.goto(base+'/#account');await page.locator('#account-logout').click()
                await page.locator('#account-button').click();await expect(page.locator('#auth-form')).to_be_hidden()
                await page.locator('#auth-native-toggle').click();await page.locator('#auth-switch').click()
                username='native_browser_'+str(width)
                await page.locator('#auth-username').fill(username);await page.locator('#auth-password').fill('native-browser-password-123')
                for key in ('auth-terms','auth-personal-data','auth-age'):await page.locator('#'+key).check()
                await page.locator('#auth-submit').click();await expect(page.locator('#auth-dialog')).to_be_hidden()
                before=(await (await context.request.get(base+'/api/auth/me')).json())['user']['id']
                await page.goto(base+'/#account');await page.evaluate("Account.api('/fixture/bonus',{method:'POST',body:'{}'}).then(()=>Account.refresh())")
                await page.goto(base+'/#account');await page.locator('[data-profile-tab="settings"]').click();await page.locator('#account-social-card > summary').click();await page.locator('#social-link-password').fill('native-browser-password-123')
                subject=str(2000000+width)
                await page.get_by_role('button',name='Подключить Яндекс',exact=True).click()
                await expect(page.locator('#account-social-connections')).to_contain_text('Подключён')
                me=await (await context.request.get(base+'/api/auth/me')).json();assert me['user']['id']==before and me['user']['bonus_analyses']==7
                await page.locator('#account-logout').click();await page.locator('#account-button').click();await page.locator('#social-yandex').click()
                await expect(page.locator('#account-button')).to_have_text(username)
                await expect(page.locator('#social-completion')).to_be_hidden()
                me=await (await context.request.get(base+'/api/auth/me')).json();assert me['user']['id']==before and me['user']['bonus_analyses']==7
                await page.evaluate("Account.api('/fixture/admin',{method:'POST',body:'{}'}).then(()=>Account.refresh())")
                await page.goto(base+'/#admin');await page.locator('[data-admin-tab="users"]').click()
                await expect(page.locator('#admin-users thead')).to_contain_text('Способ входа')
                await expect(page.locator('#admin-users tbody tr').filter(has_text=username)).to_contain_text('Яндекс')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
                assert not errors,errors
                await context.close()
        finally:await browser.close()
    print('PASS: Yandex/VK/OK/Mail redirects, consent, existing login, native collapse, safe linking with retained balance, admin method; 1440/390/320; no real provider/model/payment calls')


def main():
    Path('/workspace/outputs').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='matchlab-social-browser-') as folder:
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,PUBLIC_ORIGIN=base,QWEN_BASE_URL='',QWEN_MODEL='',
            PYTHONPATH=str(ROOT/'tests')+os.pathsep+os.environ.get('PYTHONPATH',''))
        with open('/tmp/matchlab-social-browser-server.log','w') as log:
            server=subprocess.Popen([sys.executable,'-m','uvicorn','social_browser_fixture:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:opener.open(base+'/api/auth/me',timeout=1).close();break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('Social fixture failed to start')
                asyncio.run(exercise(base))
            finally:server.terminate();server.wait(timeout=20)
        assert 'SECRET_BROWSER_CODE_' not in Path('/tmp/matchlab-social-browser-server.log').read_text(),'OAuth code leaked to access log'


if __name__=='__main__':main()
