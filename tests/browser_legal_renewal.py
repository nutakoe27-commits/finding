"""Real account UI and consent guard; missing fixture never starts an AI call."""
import os
import shutil

import browser_verdict_outcomes as fixture
from matchlab import accounts


PATH='/api/bookmakers/events/98765432/analysis-jobs'


async def prepare(page,base):
    await page.goto(base+'/#matches')
    await page.evaluate('Account.ready')
    if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
    if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()


async def start_action(page):
    await page.evaluate("""path=>{
        window.legalAction='pending';
        Account.api(path,{method:'POST',body:'{}'}).then(
            ()=>window.legalAction=200,error=>window.legalAction=error.status);
    }""",PATH)


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for name,width in [('outcomes_admin',1440),('outcomes_reader',390)]:
                with repo.connection() as db:
                    uid=db.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()[0]
                    db.execute('DELETE FROM registration_agreements WHERE user_id=?',(uid,))
                context=await browser.new_context(viewport={'width':width,'height':1000},locale='ru-RU')
                await context.request.post(base+'/api/auth/login',data={'username':name,'password':fixture.PASSWORD})
                page=await context.new_page();page.set_default_timeout(10000)
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                if name=='outcomes_admin':
                    # An already open tab can have an old metadata snapshot after deployment.
                    async def old_metadata(route):
                        response=await route.fetch();data=await response.json();data['legal_version']='obsolete'
                        await route.fulfill(response=response,json=data)
                    await page.route('**/api/auth/me',old_metadata)
                await prepare(page,base)
                confirmations=[];statuses=[]
                page.on('request',lambda r:confirmations.append(r.post_data_json) if r.url.endswith('/api/auth/confirm-documents') else None)
                page.on('response',lambda r:statuses.append(r.status) if r.url.endswith(PATH) else None)
                await start_action(page)
                await expect(page.locator('#legal-renewal')).to_be_visible()
                for selector in ('#legal-terms','#legal-data','#legal-age'):await page.locator(selector).check()
                await page.locator('#legal-submit').click()
                await expect(page.locator('#legal-renewal')).to_be_hidden()
                await page.wait_for_function('()=>window.legalAction===404')
                assert statuses==[403,404],statuses
                assert len(confirmations)==1 and confirmations[0]['legal_version']==accounts.LEGAL_VERSION,confirmations
                assert accounts.agreements_current(repo,uid)
                await start_action(page);await page.wait_for_function('()=>window.legalAction===404')
                await expect(page.locator('#legal-renewal')).to_be_hidden()
                assert statuses==[403,404,404],statuses
                # Closing or pressing Escape must cancel, without retry or implied consent.
                with repo.connection() as db:db.execute('DELETE FROM registration_agreements WHERE user_id=?',(uid,))
                for dismiss in ('close','escape'):
                    await start_action(page);await expect(page.locator('#legal-renewal')).to_be_visible()
                    if dismiss=='close':await page.locator('#legal-close').click()
                    else:await page.keyboard.press('Escape')
                    await page.wait_for_function('()=>window.legalAction===403')
                    await expect(page.locator('#legal-renewal')).to_be_hidden()
                    assert not accounts.agreements_current(repo,uid)
                assert len(confirmations)==1 and statuses==[403,404,404,403,403],(confirmations,statuses)
                assert not errors,errors
                await context.close()
            context=await browser.new_context(viewport={'width':390,'height':1000},locale='ru-RU')
            page=await context.new_page();await prepare(page,base)
            await page.evaluate('Account.show("register")')
            if await page.locator('#auth-form').is_hidden():await page.locator('#auth-native-toggle').click()
            await page.locator('#auth-username').fill('legal_new_reader')
            await page.locator('#auth-password').fill(fixture.PASSWORD)
            for selector in ('#auth-terms','#auth-personal-data','#auth-age'):await page.locator(selector).check()
            await page.locator('#auth-submit').click()
            await expect(page.locator('#auth-dialog')).to_be_hidden()
            await expect(page.locator('#account-button')).to_have_text('legal_new_reader')
            with repo.connection() as db:uid=db.execute("SELECT id FROM users WHERE username='legal_new_reader'").fetchone()[0]
            assert accounts.agreements_current(repo,uid)
            await start_action(page);await page.wait_for_function('()=>window.legalAction===404')
            await expect(page.locator('#legal-renewal')).to_be_hidden()
            await context.close()
        finally:await browser.close()
    print('PASS: server legal version; admin/reader renewal resumes once; old tab; cancel/Escape; new registration; no AI calls')


if __name__=='__main__':
    os.environ.update(BILLING_ENABLED='false',BILLING_PUBLIC_TESTING='false')
    fixture.exercise=exercise
    fixture.main()
