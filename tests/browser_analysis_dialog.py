"""Only AI modal lifecycle, error/retry, mobile layout and match deep links."""
import asyncio
import shutil
import time
from pathlib import Path

import browser_featured_recovery as fixture
from account_helpers import signed_in_browser


class SlowModel(fixture.Model):
    def do_POST(self):
        time.sleep(.35)
        super().do_POST()


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    async def deliver():
        while True:
            job=repo.claim_statistics_request()
            if job:repo.finish_statistics_request(job['job_id'],job['lease_id'],'ok')
            await asyncio.sleep(.05)
    delivery=asyncio.create_task(deliver())
    try:
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
            try:
                for theme,width in [('light',1440),('dark',390),('light',320)]:
                    context=await browser.new_context(viewport={'width':width,'height':1000},color_scheme=theme,locale='ru-RU',reduced_motion='reduce')
                    page=await context.new_page();page.set_default_timeout(15000);errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    await page.goto(base+'/#matches')
                    if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                    if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                    await signed_in_browser(page,base);await page.reload()
                    await page.wait_for_function('()=>state.ai&&state.route==="#matches"')
                    card=page.locator('.match-card').filter(has=page.locator('a[href="#match/201"]'))
                    await card.locator('.match-ai-button').click()
                    await expect(page.locator('#ai-dialog')).to_be_visible()
                    await expect(page.locator('#ai-wait')).to_be_visible()
                    assert await page.locator('#ai-mobile-host,#ai-desktop-host').count()==0
                    assert await page.locator('#ai-dialog #ai-desk').count()==1
                    await page.screenshot(path=f'/workspace/outputs/ai-popup-loading-{theme}-{width}.png')
                    await page.locator('#ai-dialog-close').click()
                    await expect(page.locator('#ai-dialog')).not_to_be_visible()
                    await expect(page.locator('#event-ai-button')).to_have_text('Посмотреть разбор',timeout=30000)
                    assert not await page.locator('#ai-dialog').evaluate('n=>n.open'),'closing must not reopen the dialog on completion'
                    calls=SlowModel.calls['201']
                    await page.locator('#event-ai-button').click()
                    await expect(page.locator('#explanation .verdict-preference')).to_have_text('Спартак')
                    await expect(page.locator('#ai-result-toolbar')).to_be_visible()
                    await expect(page.locator('#ai-notes-details')).to_be_visible()
                    assert SlowModel.calls['201']==calls,'reopening launched another model request'
                    assert await page.evaluate('()=>document.activeElement.closest("#ai-dialog")!==null')
                    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                    assert not await page.locator('#ai-dialog').evaluate('n=>n.scrollWidth>n.clientWidth')
                    await page.screenshot(path=f'/workspace/outputs/ai-popup-result-{theme}-{width}.png')
                    await page.keyboard.press('Escape')
                    await expect(page.locator('#ai-dialog')).not_to_be_visible()
                    await expect(page.locator('#event-ai-button')).to_be_focused()
                    assert await page.locator('main').evaluate('n=>n.getBoundingClientRect().right')>width-45
                    destination=await context.request.get(base+await page.locator('#event-source').get_attribute('href'),max_redirects=0)
                    assert destination.status==302 and destination.headers['location']=='https://winline.ru/stavki/sport/futbol/201'
                    await page.locator('#event-ai-button').click()
                    await page.mouse.click(2,2)
                    await expect(page.locator('#ai-dialog')).not_to_be_visible()
                    if width==1440:
                        # Model validation failure is a readable skip inside the popup.
                        await page.goto(base+'/#match/202');await page.wait_for_function('()=>state.marketDocument?.source_event_id==="202"')
                        await page.locator('#event-ai-button').click()
                        await expect(page.locator('#explanation .verdict-preference')).to_have_text('Никто',timeout=30000)
                        await expect(page.locator('#explanation .verdict-choice')).to_have_text('Пропустить матч')
                        assert '9999' not in await page.locator('#explanation').inner_text()
                        await page.locator('#ai-dialog-close').click()
                        # A transport error remains in the dialog, with an enabled retry.
                        failed=False
                        async def fail_once(route):
                            nonlocal failed
                            if not failed:
                                failed=True;await route.fulfill(status=503,content_type='application/json',body='{"detail":"Проверка ошибки соединения"}')
                            else:await route.continue_()
                        await page.route('**/api/analysis-jobs/current',fail_once)
                        await page.goto(base+'/#match/204');await page.wait_for_function('()=>state.marketDocument?.source_event_id==="204"')
                        await page.locator('#event-ai-button').click()
                        await expect(page.locator('#ai-request-error')).to_contain_text('Проверка ошибки соединения')
                        await expect(page.locator('#explain-button')).to_be_enabled()
                        await page.locator('#explain-button').click()
                        await page.wait_for_function('()=>state.aiAnalysis?.event?.source_event_id==="204"',timeout=30000)
                        await expect(page.locator('#explanation .verdict-preference')).to_have_text('Спартак',timeout=30000)
                        await page.locator('#ai-dialog-close').click()
                        # A failed initial match load has its own retry before AI submission.
                        first=True
                        async def fail_match(route):
                            nonlocal first
                            if first:
                                first=False;await route.fulfill(status=503,content_type='application/json',body='{"detail":"Не удалось загрузить матч"}')
                            else:await route.continue_()
                        await page.route('**/api/bookmakers/events/205/markets',fail_match)
                        await page.goto(base+'/#matches');await page.wait_for_function('()=>state.route==="#matches"')
                        await page.locator('.match-card').filter(has=page.locator('a[href="#match/205"]')).locator('.match-ai-button').click()
                        await expect(page.locator('#ai-context-retry')).to_be_visible()
                        await page.locator('#ai-context-retry').click()
                        await page.wait_for_function('()=>state.aiAnalysis?.event?.source_event_id==="205"',timeout=30000)
                        await expect(page.locator('#explanation .verdict-preference')).to_have_text('Спартак',timeout=30000)
                        await page.locator('#ai-dialog-close').click()
                        for identifier in ('203','299'):
                            await page.goto(base+'/#match/'+identifier);await page.wait_for_function('id=>state.marketDocument?.source_event_id===id',arg=identifier)
                            await page.locator('#event-ai-button').click()
                            await page.wait_for_function('id=>state.aiAnalysis?.event?.source_event_id===id',arg=identifier,timeout=15000)
                            await expect(page.locator('#explanation .verdict-preference')).to_have_text('Никто',timeout=15000)
                            await expect(page.locator('#explanation .verdict-choice')).to_have_text('Пропустить матч')
                            assert SlowModel.calls[identifier]==0
                            await page.locator('#ai-dialog-dismiss').click()
                    assert not errors,errors
                    await context.close()
            finally:await browser.close()
    finally:delivery.cancel();await asyncio.gather(delivery,return_exceptions=True)
    print('PASS: catalogue/detail modal; loading; close/reopen without duplicate AI; Escape/backdrop/focus; retry errors; explicit skip; full-width page; 1440/390/320; exact Winline match links')


if __name__=='__main__':
    Path('/workspace/outputs').mkdir(exist_ok=True)
    fixture.Model=SlowModel
    fixture.exercise=exercise
    fixture.main()
