"""Only retired catalogue sport and the extended automation controls."""
from datetime import datetime,timezone
import shutil

import browser_verdict_outcomes as fixture
from browser_archive_smoke import feed
from test_archive import header


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    hidden=header('301');hidden.update(sport_id=20,sport_label='Настольный теннис',featured_rank=0)
    visible=header('302');visible['featured_rank']=1
    feed(repo,[hidden,visible])
    with repo.connection() as db:
        for identifier,source in [('401','featured'),('402','catalogue')]:
            db.execute('''INSERT INTO featured_analysis_events(event_id,home,away,sport,kickoff_at,state,created_at,updated_at,selection_source)
                VALUES (?,?,?,?,?,'completed',?,?,?)''',(identifier,'Альфа '+identifier,'Бета '+identifier,'football',visible['kickoff_at'],datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat(),source))
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for width in (1440,390):
                context=await browser.new_context(viewport={'width':width,'height':1000},locale='ru-RU')
                await context.add_init_script("localStorage.setItem('matchlab:sport:v1','table_tennis')")
                page=await context.new_page();page.set_default_timeout(12000);errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                await context.request.post(base+'/api/auth/login',data={'username':'outcomes_admin','password':fixture.PASSWORD})
                await page.goto(base+'/#matches')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await expect(page.locator('#sport-tabs')).to_contain_text('Футбол')
                await expect(page.locator('#sport-tabs')).not_to_contain_text('Настольный теннис')
                await expect(page.locator('#sport-filter option[value="table_tennis"]')).to_have_count(0)
                assert await page.evaluate('state.sport')=='football'
                await page.goto(base+'/#admin')
                await page.locator('[data-admin-tab="automation"]').click()
                await expect(page.locator('#auto-analysis-form')).to_contain_text('до 5 рекомендаций')
                await expect(page.locator('#auto-analysis-status')).to_contain_text('0 из 5')
                await expect(page.locator('#auto-analysis-events')).to_contain_text('Предстоящие матчи')
                await expect(page.locator('#auto-analysis-events')).to_contain_text('Главные события')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                assert not errors,errors
                await page.screenshot(path=f'/workspace/outputs/pick-pool-{width}.png',full_page=True)
                await context.close()
        finally:await browser.close()
    print('PASS: no table-tennis tabs/options or stale saved selection; five-pick target and queue sources in admin; 1440/390; no model calls')


if __name__=='__main__':
    fixture.exercise=exercise
    fixture.main()
