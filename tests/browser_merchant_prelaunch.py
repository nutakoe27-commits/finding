"""Merchant-facing documents and public/admin payment states, without payments."""
import os
import shutil
from uuid import uuid4

import browser_verdict_outcomes as fixture
from matchlab.billing import OFFER_VERSION


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for width,theme in [(1440,'light'),(390,'dark'),(320,'light')]:
                context=await browser.new_context(viewport={'width':width,'height':1000},color_scheme=theme,locale='ru-RU')
                page=await context.new_page();page.set_default_timeout(15000);errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(base+'/#matches')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await expect(page.locator('#credits-open')).to_have_text('Тарифы')
                await page.locator('#credits-open').click()
                await expect(page.locator('#credits-dialog')).to_be_visible()
                await expect(page.locator('#auth-dialog')).not_to_be_visible()
                await expect(page.locator('#credits-launch-note')).to_contain_text('Приём оплаты ещё не открыт')
                await expect(page.locator('#credits-pay')).to_be_disabled()
                await expect(page.locator('#credits-consent-row')).to_be_hidden()
                await expect(page.locator('#credits-mode')).to_have_text('Пакеты разборов')
                await expect(page.locator('#credits-packages .credit-package')).to_have_count(3)
                await expect(page.locator('#credits-dialog')).not_to_contain_text('Тестовая оплата')
                await page.screenshot(path=f'/workspace/outputs/merchant-prices-{width}.png',full_page=True)
                await page.locator('#credits-close').click()
                for name in ('pricing','offer','payment-refund','contacts'):
                    await page.goto(base+'/static/legal/'+name+'.html')
                    await expect(page.locator('h1')).to_be_visible()
                    await expect(page.locator('body')).not_to_contain_text('Тестовая покупка')
                    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(name,width)
                    if name=='pricing':await page.screenshot(path=f'/workspace/outputs/merchant-tariffs-{width}.png',full_page=True)
                assert not errors,errors
                await context.close()
            context=await browser.new_context(viewport={'width':1440,'height':1000},locale='ru-RU')
            await context.request.post(base+'/api/auth/login',data={'username':'outcomes_admin','password':fixture.PASSWORD})
            page=await context.new_page();page.set_default_timeout(15000)
            await page.goto(base+'/#matches')
            if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
            if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
            await page.locator('#credits-open').click()
            await expect(page.locator('#credits-mode')).to_contain_text('тестовый магазин')
            await expect(page.locator('#credits-pay')).to_be_enabled()
            await expect(page.locator('#credits-consent-row')).to_be_visible()
            await expect(page.locator('#credits-offer-link')).to_have_attribute('href','/static/legal/test-purchase.html')
            await page.locator('#credits-close').click()
            await page.evaluate("Account.api('/api/auth/logout',{method:'POST',body:'{}'}).then(()=>Account.refresh())")
            await expect(page.locator('#credits-open')).to_have_text('Тарифы')
            await page.locator('#credits-open').click()
            await expect(page.locator('#credits-mode')).to_have_text('Пакеты разборов')
            await expect(page.locator('#credits-pay')).to_be_disabled()
            await context.request.post(base+'/api/auth/login',data={'username':'outcomes_reader','password':fixture.PASSWORD})
            me=await (await context.request.get(base+'/api/auth/me')).json()
            # Use the same CSRF wrapper as the application after refreshing the account.
            await page.locator('#credits-close').click();await page.evaluate('Account.refresh()')
            result=await page.evaluate("""async body=>{try{await Account.api('/api/billing/orders',{method:'POST',body:JSON.stringify(body)});return 200;}catch(e){return e.status;}}""",
                {'package_id':'one','request_key':uuid4().hex,'offer_version':OFFER_VERSION,'offer_accepted':True,'role':'admin'})
            assert result==503,result
            with repo.connection() as db:assert db.execute('SELECT count(*) FROM payment_orders').fetchone()[0]==0
            await context.close()
        finally:await browser.close()
    print('PASS: public tariffs/documents without login; checkout closed; admin test mode explicit; logout resets state; 1440/390/320; no payment/model calls')


if __name__=='__main__':
    os.environ.update(BILLING_ENABLED='true',BILLING_PUBLIC_TESTING='false',YOOKASSA_SHOP_ID='100500',YOOKASSA_SECRET_KEY='test_browser_review')
    fixture.exercise=exercise
    fixture.main()
