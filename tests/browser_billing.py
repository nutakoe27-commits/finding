"""Paid forecasts and real HTTP wallet flow against a local fake payment adapter."""
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

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from matchlab import accounts


async def exercise(base):
    from playwright.async_api import async_playwright,expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for width,theme in [(1440,'light'),(390,'dark'),(320,'light')]:
                context=await browser.new_context(viewport={'width':width,'height':1000},color_scheme=theme,locale='ru-RU',reduced_motion='reduce')
                response=await context.request.post(base+'/api/auth/register',data={'username':'billing_'+str(width),'password':'browser-billing-password-123',
                    'terms_accepted':True,'personal_data_consent':True,'age_confirmed':True,'legal_version':accounts.LEGAL_VERSION})
                assert response.status==201,await response.text()
                seeded=await (await context.request.post(base+'/fixture/seed')).json()
                page=await context.new_page();page.set_default_timeout(15000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.route('https://yoomoney.ru/**',lambda route:route.fulfill(content_type='text/html',body='<h1>Фиктивная тестовая платёжная страница</h1><a id="return" href="'+base+'/#account">Вернуться в сервис</a>'))
                await page.goto(base+'/#result/'+seeded['system'])
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await expect(page.locator('#public-result-content .credit-gate')).to_be_visible()
                await expect(page.locator('#public-result-content')).not_to_contain_text('СЕКРЕТНОЕ')
                await page.locator('#public-result-content .credit-gate .primary-button').click()
                await expect(page.locator('#credits-dialog')).to_be_visible()
                await expect(page.locator('#credits-packages .credit-package')).to_have_count(3)
                await expect(page.locator('#credits-packages')).to_contain_text('490 ₽')
                await expect(page.locator('#credits-packages')).to_contain_text('2\u00a0190 ₽')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
                await page.screenshot(path=f'/workspace/outputs/billing-packages-{width}.png',full_page=True)
                await page.locator('#credits-consent').check();await page.locator('#credits-pay').click()
                await page.wait_for_url('https://yoomoney.ru/**')
                from urllib.parse import urlsplit,parse_qs
                order_id=parse_qs(urlsplit(page.url).query)['orderId'][0]
                before=await (await context.request.get(base+'/api/billing/wallet')).json();assert before['balance']==0
                await context.request.post(base+'/fixture/succeed/'+order_id)
                await page.locator('#return').click()
                await expect(page.locator('#credits-order-title')).to_have_text('Оплачено')
                await expect(page.locator('#credits-balance')).to_have_text('1')
                await page.locator('#credits-order-open').click()
                await expect(page.locator('#public-result-content')).to_contain_text('СЕКРЕТНОЕ ОБОСНОВАНИЕ')
                await expect(page.locator('#credits-open')).to_have_text('Разборы: 0')
                await page.reload();await expect(page.locator('#public-result-content')).to_contain_text('СЕКРЕТНОЕ ОБОСНОВАНИЕ')
                wallet=await (await context.request.get(base+'/api/billing/wallet')).json();assert wallet['balance']==0
                await page.goto(base+'/#analysis/'+seeded['skip'])
                await expect(page.locator('#saved-analysis-text')).to_contain_text('Рекомендуем пропустить матч')
                await expect(page.locator('#saved-analysis-text')).not_to_contain_text('СЕКРЕТНОЕ')
                await page.locator('#saved-analysis-text .credit-gate .primary-button').click()
                await expect(page.locator('#credits-dialog')).to_be_visible()
                await page.locator('input[name="credit-package"][value="five"]').check();await page.locator('#credits-consent').check();await page.locator('#credits-pay').click()
                await page.wait_for_url('https://yoomoney.ru/**');order_id=parse_qs(urlsplit(page.url).query)['orderId'][0]
                await context.request.post(base+'/fixture/succeed/'+order_id);await page.locator('#return').click()
                await expect(page.locator('#credits-order-title')).to_have_text('Оплачено')
                await expect(page.locator('#credits-balance')).to_have_text('5')
                await page.locator('#credits-order-open').click()
                await expect(page.locator('#saved-analysis-text')).to_contain_text('СЕКРЕТНОЕ ОБОСНОВАНИЕ')
                await expect(page.locator('#credits-open')).to_have_text('Разборы: 4')
                await page.goto(base+'/#analysis/'+seeded['private'])
                await expect(page.locator('#saved-analysis-text')).not_to_contain_text('СЕКРЕТНОЕ')
                await page.locator('#saved-analysis-text .credit-gate .primary-button').click()
                await expect(page.locator('#saved-analysis-text')).to_contain_text('СЕКРЕТНОЕ ОБОСНОВАНИЕ')
                await expect(page.locator('#credits-open')).to_have_text('Разборы: 3')
                await page.goto(base+'/#account');await expect(page.locator('#account-credit-balance')).to_have_text('3 разбора')
                await expect(page.locator('#account-credit-orders .credit-order-row')).to_have_count(2)
                await expect(page.locator('#account-open-analyses .credit-opened')).to_have_count(3)
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
                await page.screenshot(path=f'/workspace/outputs/billing-wallet-{width}.png',full_page=True)
                await page.evaluate("Account.api('/api/auth/logout',{method:'POST',body:'{}'}).then(()=>Account.refresh())")
                await page.goto(base+'/#result/'+seeded['system'])
                await expect(page.locator('#public-result-content .credit-gate')).to_be_visible()
                await expect(page.locator('#public-result-content')).not_to_contain_text('СЕКРЕТНОЕ')
                assert not errors,errors
                await context.close()
        finally:await browser.close()
    print('PASS: local fake YooKassa redirect/return; one/five packs; verified credit; paid system/private/skip; repeat free; wallet/history/logout; 1440/390/320; no real payment/model calls')


def main():
    Path('/workspace/outputs').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='matchlab-billing-browser-') as folder:
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,PUBLIC_ORIGIN=base,QWEN_BASE_URL='',QWEN_MODEL='',BOOKMAKER_INGEST_TOKEN='',PYTHONPATH=str(ROOT/'tests')+os.pathsep+os.environ.get('PYTHONPATH',''))
        with open('/tmp/matchlab-billing-browser.log','w') as log:
            server=subprocess.Popen([sys.executable,'-m','uvicorn','billing_browser_fixture:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:opener.open(base+'/api/auth/me',timeout=1).close();break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('Fixture did not start; see /tmp/matchlab-billing-browser.log')
                asyncio.run(exercise(base))
            finally:server.terminate();server.wait(timeout=20)


if __name__=='__main__':main()
