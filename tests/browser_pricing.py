"""Editor, preview, documents and first-payment promotion on local fake payments."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import shutil
from urllib.parse import urlsplit,parse_qs

import browser_profile as fixture
from matchlab import accounts


async def exercise(base,folder):
    from playwright.async_api import async_playwright,expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for width,theme in ((1440,'light'),(390,'dark'),(320,'light')):
                ctx=await browser.new_context(viewport={'width':width,'height':1000},locale='ru-RU',color_scheme=theme,reduced_motion='reduce')
                password='pricing-browser-password-123'
                registered=await ctx.request.post(base+'/api/auth/register',data={'username':'pricing_'+str(width),'password':password,
                    'terms_accepted':True,'personal_data_consent':True,'age_confirmed':True,'legal_version':accounts.LEGAL_VERSION})
                assert registered.status==201,await registered.text()
                await ctx.request.post(base+'/fixture/subscription-admin')
                page=await ctx.new_page();page.set_default_timeout(12000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.route('https://yoomoney.ru/**',lambda route:route.fulfill(content_type='text/html',body='<h1>Фиктивная оплата</h1><a id="return" href="'+base+'/#account">Вернуться</a>'))
                await page.goto(base+'/#admin')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await page.locator('[data-admin-tab=pricing]').click()
                await expect(page.locator('#pricing-cards .pricing-editor-card')).to_have_count(3)
                first=page.locator('.pricing-editor-card').first
                await first.locator('[data-field=credits]').fill('7')
                await first.locator('[data-field=amount]').fill('799')
                await first.locator('[data-field=period_days]').fill('7')
                await first.locator('[data-field=percent]').fill('25')
                until=(datetime.now(timezone.utc)+timedelta(days=2)).astimezone(timezone(timedelta(hours=3))).strftime('%Y-%m-%dT%H:%M')
                await first.locator('[data-field=ends_at]').fill(until)
                await page.locator('[data-pricing-kind=packages]').click();await expect(page.locator('.pricing-editor-card')).to_have_count(3)
                await page.locator('[data-pricing-kind=subscriptions]').click();await expect(first.locator('[data-field=credits]')).to_have_value('7')
                await page.locator('#pricing-save').click();await expect(page.locator('#pricing-message')).to_contain_text('Черновик сохранён')
                await page.reload();await page.locator('[data-admin-tab=pricing]').click()
                await expect(first.locator('[data-field=percent]')).to_have_value('25')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(width,'editor overflow')
                await page.screenshot(path=f'/workspace/outputs/pricing-editor-{width}.png',full_page=False)
                await page.locator('#pricing-preview-open').click();await expect(page.locator('#pricing-preview')).to_be_visible()
                await expect(page.locator('#pricing-preview-cards')).to_contain_text('599,25 ₽')
                await page.locator('#pricing-document-select').select_option('offer')
                await expect(page.locator('#pricing-document')).to_contain_text('599,25 ₽')
                await expect(page.locator('#pricing-document')).to_contain_text('799 ₽')
                await expect(page.locator('#pricing-document')).to_contain_text('только к первому платежу')
                await page.locator('#pricing-document-select').select_option('recurring')
                await expect(page.locator('#pricing-document')).to_contain_text('599,25 ₽')
                assert not await page.locator('#pricing-preview').evaluate('e=>e.scrollWidth>e.clientWidth+1'),(width,'preview overflow')
                await page.screenshot(path=f'/workspace/outputs/pricing-preview-{width}.png',full_page=False)
                await page.locator('#pricing-publish').click();await expect(page.locator('#admin-password-dialog')).to_be_visible()
                await page.locator('#admin-password').fill(password);await page.locator('#admin-password-form button[type=submit]').click()
                await expect(page.locator('#pricing-preview')).to_be_hidden();await expect(page.locator('#pricing-message')).to_contain_text('опубликованы')
                await page.locator('#credits-open').click();await expect(page.locator('#credits-dialog')).to_be_visible()
                plan=page.locator('.subscription-plan').first
                await expect(plan.locator('.subscription-price strong')).to_have_text('599,25 ₽')
                await expect(plan.locator('.credit-old-price')).to_have_text('799 ₽')
                await expect(plan).to_contain_text('Далее 799 ₽ раз в 7 дней')
                assert not await page.locator('#credits-dialog').evaluate('e=>e.scrollWidth>e.clientWidth+1'),(width,'public card overflow')
                await page.screenshot(path=f'/workspace/outputs/pricing-public-{width}.png',full_page=False)
                await plan.locator('button').click()
                await expect(page.locator('#subscription-checkout-price')).to_contain_text('599,25 ₽ сейчас, затем 799 ₽')
                await expect(page.locator('#subscription-consent-text')).to_contain_text('599,25 ₽ сейчас, затем 799 ₽')
                assert not await page.locator('#subscription-offer').is_checked()
                assert not await page.locator('#subscription-recurring').is_checked()
                await page.locator('#subscription-email').fill('pricing@example.ru')
                await page.locator('#subscription-offer').check();await page.locator('#subscription-recurring').check()
                await page.locator('#subscription-pay').click();await page.wait_for_url('https://yoomoney.ru/**')
                order_id=parse_qs(urlsplit(page.url).query)['orderId'][0]
                await ctx.request.post(base+'/fixture/succeed/'+order_id);await page.locator('#return').click()
                await expect(page.locator('#credits-order-title')).to_have_text('Оплачено')
                wallet=await (await ctx.request.get(base+'/api/billing/wallet')).json()
                assert wallet['balance']==7 and wallet['orders'][0]['amount_kopecks']==59925
                assert wallet['subscription']['current']['amount_kopecks']==79900
                assert wallet['subscription']['current']['period_hours']==168
                await page.locator('#credits-close').click()
                await page.goto(base+'/static/legal/offer.html')
                await expect(page.locator('table').first).to_contain_text('599,25 ₽')
                await expect(page.locator('table').first).to_contain_text('799 ₽')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(width,'public document overflow')
                assert not errors,(width,errors)
                await ctx.close()
        finally:await browser.close()
    print('PASS: 1440/390/320; subscription/package editor, saved draft, price/period/discount preview, auto documents, reauth publication, public promo, exact first payment and full renewal price; no real money or AI calls')


if __name__=='__main__':
    fixture.exercise=exercise
    fixture.main()
