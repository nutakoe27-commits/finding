"""Payment dashboard, local-only fixtures; no provider calls or real payments."""
from datetime import datetime,timedelta,timezone
import shutil
from uuid import uuid4

import browser_verdict_outcomes as fixture
from matchlab import accounts,billing


def seed(repo):
    with repo.connection() as db:
        uid=db.execute("SELECT id FROM users WHERE username='outcomes_reader'").fetchone()[0]
        for index,(package,state,returned) in enumerate([('one','succeeded',0),('five','succeeded',43800),('ten','pending',0),('one','failed',0)]):
            p=next(p for p in billing.PACKAGES if p['id']==package);oid=uuid4().hex
            at=(datetime.now(timezone.utc)-timedelta(days=index)).isoformat()
            db.execute('''INSERT INTO payment_orders(id,user_id,request_key,mode,package_id,credits,amount_kopecks,state,created_at,updated_at,payment_id,offer_version,refunded_kopecks,error_code)
                VALUES (?,?,?,'test',?,?,?,?,?,?,?,?,?,?)''',(oid,uid,uuid4().hex,package,p['credits'],p['amount_kopecks'],state,at,at,'payment-'+oid,billing.OFFER_VERSION,returned,'provider_rejected' if state=='failed' else None))
            if state=='succeeded':db.execute('INSERT INTO analysis_credit_ledger VALUES (?,?,?,?,?,?,?)',(uuid4().hex,uid,'test',p['credits'],'payment',oid,at))
            if returned:
                rid='refund-'+uuid4().hex
                db.execute('INSERT INTO payment_refunds VALUES (?,?,?,?)',(rid,oid,returned,accounts.now()))
                db.execute('INSERT INTO analysis_credit_ledger VALUES (?,?,?,?,?,?,?)',(uuid4().hex,uid,'test',-1,'refund',rid,accounts.now()))


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    seed(repo)
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for width,theme in [(1440,'light'),(390,'dark'),(320,'light')]:
                context=await browser.new_context(viewport={'width':width,'height':1000},color_scheme=theme,locale='ru-RU')
                await context.request.post(base+'/api/auth/login',data={'username':'outcomes_admin','password':fixture.PASSWORD})
                page=await context.new_page();page.set_default_timeout(12000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(base+'/#admin')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await page.locator('[data-admin-tab="billing"]').click()
                await expect(page.locator('#ab-metrics .ar-metric')).to_have_count(6)
                await expect(page.locator('#ab-orders tbody tr')).to_have_count(4)
                await expect(page.locator('#ab-notice')).to_contain_text('Тестовые операции')
                await expect(page.locator('#ab-chart svg')).to_have_count(1)
                await page.locator('#ab-days').focus();await page.evaluate('window.scrollTo(0,0)')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(width,'dashboard overflow',await page.evaluate("Array.from(document.querySelectorAll('#admin-billing *')).filter(e=>e.getBoundingClientRect().right>innerWidth).slice(0,12).map(e=>[e.tagName,e.id,e.className,e.getBoundingClientRect().width,getComputedStyle(e).minWidth])"))
                await page.screenshot(path=f'/workspace/outputs/admin-billing-{width}.png',full_page=True)
                await page.locator('#ab-state').select_option('succeeded')
                await expect(page.locator('#ab-orders tbody tr')).to_have_count(2)
                await page.locator('#ab-orders button').nth(1).click()
                await expect(page.locator('#ab-detail')).to_be_visible()
                await expect(page.locator('#ab-detail-content')).to_contain_text('Движение разборов аккаунта')
                await expect(page.locator('#ab-detail-content')).to_contain_text('Возвраты по заказу')
                assert not await page.locator('#ab-detail').evaluate('(e)=>e.scrollWidth>e.clientWidth+1'),(width,'dialog overflow',await page.locator('#ab-detail').evaluate("e=>Array.from(e.querySelectorAll('*')).filter(x=>x.getBoundingClientRect().right>e.getBoundingClientRect().right).slice(0,8).map(x=>[x.tagName,x.className,x.getBoundingClientRect().width])"))
                await page.screenshot(path=f'/workspace/outputs/admin-billing-detail-{width}.png',full_page=True)
                await page.keyboard.press('Escape');await expect(page.locator('#ab-detail')).to_be_hidden()
                await page.locator('#ab-mode').select_option('live')
                await expect(page.locator('#ab-page')).to_have_text('Заказов нет')
                await expect(page.locator('#ab-notice')).to_contain_text('Боевой приём оплаты пока не открыт')
                await page.evaluate("Account.api('/api/auth/logout',{method:'POST',body:'{}'}).then(()=>Account.refresh())")
                await expect(page.locator('#ab-metrics')).to_be_empty()
                assert (await context.request.get(base+'/api/admin/billing')).status==401
                assert not errors,errors
                await context.close()
        finally:await browser.close()
    print('PASS: admin payment metrics/filters/charts/details/empty-live/logout; 1440/390/320; no payment/model calls')


if __name__=='__main__':fixture.exercise=exercise;fixture.main()
