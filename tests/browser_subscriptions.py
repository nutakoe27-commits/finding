"""Daily subscription UX at mobile/desktop widths, entirely fake money and AI."""
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
from urllib.parse import urlsplit,parse_qs
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from matchlab import accounts


async def exercise(base):
    from playwright.async_api import async_playwright,expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for width,theme in ((1440,'light'),(390,'dark'),(320,'light')):
                ctx=await browser.new_context(viewport={'width':width,'height':1000},locale='ru-RU',color_scheme=theme,
                    reduced_motion='reduce',extra_http_headers={'X-Real-IP':'192.0.2.'+str(width%250+1)})
                registered=await ctx.request.post(base+'/api/auth/register',data={'username':'daily_'+str(width),
                    'password':'subscription-browser-pass-123','terms_accepted':True,'personal_data_consent':True,
                    'age_confirmed':True,'legal_version':accounts.LEGAL_VERSION})
                assert registered.status==201,await registered.text()
                seeded=await (await ctx.request.post(base+'/fixture/seed')).json()
                page=await ctx.new_page();page.set_default_timeout(15000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.route('https://yoomoney.ru/**',lambda route:route.fulfill(content_type='text/html',body='<h1>Фиктивная оплата</h1><a id="return" href="'+base+'/#account">Вернуться</a>'))
                await page.goto(base+'/#matches')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await page.locator('#credits-open').click()
                await expect(page.locator('#subscription-plans .subscription-plan')).to_have_count(3)
                await expect(page.locator('.subscription-quantity strong')).to_have_text(['1','5','∞'])
                await expect(page.locator('.subscription-quantity small')).to_have_text(['/1 день']*3)
                await expect(page.locator('#credits-dialog')).to_contain_text('ТОЛЬКО когда ИИ рекомендует «Ставить»')
                assert not await page.locator('#credits-one-time').evaluate('(e)=>e.open')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
                await page.screenshot(path=f'/workspace/outputs/subscription-plans-{width}.png',full_page=True)
                await page.locator('.subscription-plan-featured button').click()
                await expect(page.locator('#subscription-checkout')).to_be_visible()
                assert not await page.locator('#subscription-offer').is_checked()
                assert not await page.locator('#subscription-recurring').is_checked()
                await page.locator('#subscription-pay').click()
                await expect(page.locator('#credits-message')).to_contain_text('ежедневные автоматические списания')
                await page.locator('#subscription-email').fill('buyer@example.ru')
                await page.locator('#subscription-offer').check();await page.locator('#subscription-recurring').check()
                await page.locator('#subscription-pay').click();await page.wait_for_url('https://yoomoney.ru/**')
                order_id=parse_qs(urlsplit(page.url).query)['orderId'][0]
                assert (await (await ctx.request.get(base+'/api/billing/wallet')).json())['balance']==0
                await ctx.request.post(base+'/fixture/succeed/'+order_id);await page.locator('#return').click()
                await expect(page.locator('#credits-order-title')).to_have_text('Оплачено')
                await expect(page.locator('#credits-balance')).to_have_text('5')
                await page.locator('#credits-close').click()
                await expect(page.locator('#account-subscription')).to_be_visible()
                await expect(page.locator('#subscription-summary')).to_contain_text('Следующее списание')
                await page.screenshot(path=f'/workspace/outputs/subscription-account-{width}.png',full_page=True)
                await page.locator('#subscription-cancel').click()
                await page.locator('.subscription-cancel-confirm .secondary-button').click()
                await expect(page.locator('#subscription-state')).to_have_text('Автопродление отключено')
                assert (await (await ctx.request.get(base+'/api/billing/wallet')).json())['balance']==5
                await page.goto(base+'/#analysis/'+seeded['skip'])
                await expect(page.locator('#saved-analysis-text')).to_contain_text('СЕКРЕТНОЕ ОБОСНОВАНИЕ')
                await expect(page.locator('#saved-analysis-text')).to_contain_text('«Пропустить» — бесплатно')
                assert await page.locator('#saved-analysis-text .credit-gate').count()==0
                assert (await (await ctx.request.get(base+'/api/billing/wallet')).json())['balance']==5
                await page.goto(base+'/#analysis/'+seeded['private'])
                await expect(page.locator('#saved-analysis-text .credit-gate')).to_be_visible()
                await page.locator('#saved-analysis-text .credit-gate .primary-button').click()
                await expect(page.locator('#saved-analysis-text')).to_contain_text('СЕКРЕТНОЕ ОБОСНОВАНИЕ')
                assert (await (await ctx.request.get(base+'/api/billing/wallet')).json())['balance']==4
                await page.evaluate("Account.api('/fixture/subscription-admin',{method:'POST',body:'{}'}).then(()=>Account.refresh())")
                await page.goto(base+'/#admin');await page.locator('[data-admin-tab="billing"]').click()
                await expect(page.locator('#ab-subscriptions')).to_contain_text('5 разборов каждый день')
                await expect(page.locator('#ab-subscriptions')).to_contain_text('Отключено')
                await page.locator('#ab-orders button').first.click()
                await expect(page.locator('#ab-detail-content')).to_contain_text('Согласие на ежедневные списания')
                await expect(page.locator('#ab-detail-content')).to_contain_text('2190 ₽ сейчас')
                assert not errors,errors
                await ctx.close()
        finally:await browser.close()
    print('PASS: 1440/390/320; daily plan cards, explicit consent, payment return, cancellation, accumulation, free skip and one-credit bet; no real payments or model calls')


def main():
    Path('/workspace/outputs').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='subscription-browser-') as folder:
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        base='http://127.0.0.1:'+str(port)
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,PUBLIC_ORIGIN=base,LIVE_BILLING_FIXTURE='true',SUBSCRIPTION_BROWSER_FIXTURE='true',
            QWEN_BASE_URL='',QWEN_MODEL='',PYTHONPATH=str(ROOT/'tests')+os.pathsep+os.environ.get('PYTHONPATH',''))
        with open('/tmp/subscription-browser-server.log','w') as log:
            server=subprocess.Popen([sys.executable,'-m','uvicorn','billing_browser_fixture:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:opener.open(base+'/api/auth/me',timeout=1).close();break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('Browser fixture did not start')
                asyncio.run(exercise(base))
            finally:server.terminate();server.wait(timeout=20)
if __name__=='__main__':main()
