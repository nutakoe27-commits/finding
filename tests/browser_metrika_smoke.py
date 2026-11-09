"""Offline browser test: consent, exact goal events, masking and revocation."""
import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from matchlab.storage import Repository
from test_product import event
from scripts.configure_metrika_goals import GOALS

FAKE_TAG='''(() => {const pending=window.ym?.a || [];window.__ymCalls=[];
window.ym=function(){window.__ymCalls.push(Array.from(arguments));};
for (const args of pending) window.ym.apply(null,args);})();'''


async def exercise(base):
    from playwright.async_api import async_playwright, expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for replay in (False,True):
                context=await browser.new_context(viewport={'width':320 if replay else 1280,'height':720 if replay else 900})
                requests=[]
                async def fake_yandex(route):
                    requests.append(route.request.url)
                    await route.fulfill(status=200,content_type='application/javascript',body=FAKE_TAG)
                await context.route('https://mc.yandex.ru/**',fake_yandex)
                page=await context.new_page()
                await page.goto(base+'/?private_query=do-not-send#matches')
                await page.locator('#age-accept').click()
                await expect(page.locator('#privacy-banner')).to_be_visible()
                await page.screenshot(path='/tmp/metrika-consent-mobile.png' if replay else '/tmp/metrika-consent-desktop.png')
                assert not await page.evaluate('document.documentElement.scrollWidth > innerWidth')
                await page.evaluate("ProductAnalytics.goal('registration_success')")
                assert requests==[] and not await page.evaluate('Boolean(window.ym)')
                legal=await context.new_page()
                await legal.goto(base+'/static/legal/privacy.html?private_query=do-not-send')
                await legal.wait_for_timeout(200)
                assert requests==[] and not await legal.evaluate('Boolean(window.ym)')
                await legal.close()
                if replay: await page.locator('#privacy-webvisor').check()
                await page.locator('#privacy-allow').click()
                await page.wait_for_function("() => Array.isArray(window.__ymCalls) && window.__ymCalls.length > 0")
                calls=await page.evaluate('window.__ymCalls')
                init=next(x for x in calls if x[1]=='init')
                assert init[0]==114139580 and init[2]['webvisor']==replay
                assert init[2]['clickmap'] is True and init[2]['trackLinks'] is False
                assert 'private_query' not in init[2]['url']
                assert await page.locator('body').evaluate("e=>e.classList.contains('ym-hide-content')")
                assert await page.locator('#view-matches').evaluate("e=>e.classList.contains('ym-show-content')")
                assert await page.locator('#auth-password').evaluate("e=>e.classList.contains('ym-disable-keys')")
                legal=await context.new_page()
                await legal.goto(base+'/static/legal/privacy.html?private_query=do-not-send')
                await legal.wait_for_function("() => Array.isArray(window.__ymCalls) && window.__ymCalls.length > 0")
                legal_init=next(x for x in await legal.evaluate('window.__ymCalls') if x[1]=='init')
                assert legal_init[2]['url']==base+'/legal/privacy' and legal_init[2]['webvisor'] is False
                await legal.close()
                if not replay:
                    await page.locator('#account-button').click()
                    await page.locator('#auth-switch').click()
                    await page.locator('#auth-username').fill('metrika_test')
                    await page.locator('#auth-password').fill('fixture-password-long-enough')
                    for selector in ('#auth-terms','#auth-personal-data','#auth-age'): await page.locator(selector).check()
                    await page.locator('#auth-submit').click()
                    await expect(page.locator('#auth-dialog')).to_be_hidden()
                    await page.wait_for_function("() => window.__ymCalls.some(x=>x[1]==='reachGoal' && x[2]==='registration_success')")
                    await page.locator('.match-card[data-event-id="123"] .favorite-button').click()
                    await page.wait_for_function("() => window.__ymCalls.some(x=>x[1]==='reachGoal' && x[2]==='favorite_add')")
                    state={'kind':'model'}
                    async def fake_explain(route):
                        if state['kind']=='error': await route.fulfill(status=502,json={'detail':'Тестовая ошибка'})
                        else: await route.fulfill(status=202,json={'id':'fixture','state':'completed','result':{'text':'Тестовый разбор','analysis_type':state['kind']}})
                    await page.route('**/api/bookmakers/events/123/analysis-jobs',fake_explain)
                    await page.evaluate("location.hash='#match/123'")
                    await page.wait_for_function("() => window.__ymCalls.some(x=>x[1]==='reachGoal' && x[2]==='match_view')")
                    await page.wait_for_function("() => window.__ymCalls.some(x=>x[1]==='hit')")
                    await page.locator('#explain-button').wait_for(state='visible')
                    await expect(page.locator('#explain-button')).to_be_enabled()
                    await page.locator('#explain-button').click()
                    await page.wait_for_function("() => window.__ymCalls.some(x=>x[1]==='reachGoal' && x[2]==='ai_model_result')")
                    state['kind']='limited_summary';await page.locator('#explain-button').click()
                    await page.wait_for_function("() => window.__ymCalls.some(x=>x[1]==='reachGoal' && x[2]==='ai_summary_result')")
                    state['kind']='error';await page.locator('#explain-button').click()
                    await page.wait_for_function("() => window.__ymCalls.some(x=>x[1]==='reachGoal' && x[2]==='ai_request_error')")
                    await page.route('**/go/winline/**',lambda route:route.fulfill(status=200,body='fixture'))
                    async with page.expect_popup() as pop:
                        await page.locator('#event-source').click()
                    popup=await pop.value;await popup.close()
                    await page.wait_for_function("() => window.__ymCalls.some(x=>x[1]==='reachGoal' && x[2]==='winline_click')")
                    await page.evaluate("location.hash='#account'")
                    await page.locator('#promo-code').fill('TESTCODE')
                    await page.locator('#promo-redeem-form button').click()
                    await page.wait_for_function("() => window.__ymCalls.some(x=>x[1]==='reachGoal' && x[2]==='promo_redeemed')")
                    await page.locator('#account-logout').click()
                    await page.locator('#account-button').click()
                    await page.locator('#auth-username').fill('metrika_test')
                    await page.locator('#auth-password').fill('fixture-password-long-enough')
                    await page.locator('#auth-submit').click()
                    await page.wait_for_function("() => window.__ymCalls.some(x=>x[1]==='reachGoal' && x[2]==='login_success')")
                    for identifier,_ in GOALS:
                        await page.evaluate('goal => ProductAnalytics.goal(goal)',identifier)
                    calls=await page.evaluate('window.__ymCalls')
                    assert {x[2] for x in calls if x[1]=='reachGoal'}=={identifier for identifier,_ in GOALS}
                    assert any(x[1]=='reachGoal' and x[2]=='ai_intent' for x in calls)
                    assert any(x[1]=='reachGoal' and x[2]=='ai_request_start' for x in calls)
                    assert all(len(x)==3 for x in calls if x[1]=='reachGoal')
                    assert not any('metrika_test' in str(x) or 'TESTCODE' in str(x) or 'private_query' in str(x) for x in calls)
                    hits=[x[2] for x in calls if x[1]=='hit']
                    assert all('123' not in hit and 'private_query' not in hit for hit in hits),hits
                await page.evaluate("document.cookie='_ym_uid=fixture; path=/'")
                count=len(requests)
                await page.locator('#privacy-settings').click()
                async with page.expect_navigation(wait_until='domcontentloaded'):
                    await page.locator('#privacy-reject').click()
                await page.wait_for_function("() => window.Privacy && !window.Privacy.analyticsAllowed")
                await page.evaluate("ProductAnalytics.goal('winline_click')")
                await page.wait_for_timeout(300)
                assert len(requests)==count,requests
                assert '_ym_uid=' not in await page.evaluate('document.cookie')
                assert (await (await page.request.get(base+'/api/privacy/preferences')).json())=={'analytics':False,'webvisor':False}
                await context.close()
        finally:
            await browser.close()
    print('PASS: analytics off by default, separate Webvisor consent, safe event payloads, SPA, masking and withdrawal')


def main():
    with tempfile.TemporaryDirectory(prefix='metrika-browser-') as folder:
        repo=Repository(Path(folder)/'matchlab.sqlite3');repo.initialize();repo.import_event_markets(event('123'))
        now=datetime.now(UTC)
        with repo.connection() as db:
            db.execute('INSERT INTO promo_codes VALUES (?,?,?,?,?,?,?,?,?,?)',('promo-fixture','TESTCODE','Тест',1,99,(now-timedelta(days=1)).isoformat(),(now+timedelta(days=1)).isoformat(),'active',now.isoformat(),'fixture'))
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,PUBLIC_ORIGIN=base,QWEN_BASE_URL='http://127.0.0.1:9/v1',QWEN_MODEL='fixture')
        server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                try: opener.open(base+'/api/bookmakers/events',timeout=1).close();break
                except OSError:time.sleep(.1)
            else:raise AssertionError('API did not start')
            asyncio.run(exercise(base))
        finally:
            server.terminate();server.wait(timeout=20)


if __name__=='__main__': main()
