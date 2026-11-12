"""Shared interface checks on populated local screens; no model/provider calls."""
from pathlib import Path
import shutil

import browser_verdict_outcomes as fixture
from browser_archive_smoke import feed
from browser_public_results import seed
from browser_today_picks import today_seed
from test_archive import header
from matchlab import archive
from matchlab.seo import match_path


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    first,*_=seed(repo)
    today_seed(repo)
    today_seed(repo,21000,day_offset=1)
    events=[]
    for i,(home,away) in enumerate((('Манчестер Юнайтед','Ньюкасл Юнайтед'),('Барселона','Атлетико Мадрид'),('Спартак Москва','Зенит Санкт-Петербург'))):
        item=header(str(101+i));item.update(members=[home,away],featured_rank=i)
        events.append(item)
    feed(repo,events)
    with repo.connection() as db:
        uid=db.execute("SELECT id FROM users WHERE username='outcomes_admin'").fetchone()[0]
        db.execute('UPDATE event_analyses SET user_id=? WHERE id=?',(uid,first['id']))
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for width,theme in ((1440,'light'),(1024,'light'),(390,'dark'),(320,'light')):
                ctx=await browser.new_context(viewport={'width':width,'height':1000},locale='ru-RU',color_scheme=theme,reduced_motion='reduce')
                page=await ctx.new_page();page.set_default_timeout(12000);errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                async def fits(label,selector=None):
                    overflow=await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                    assert not overflow,(width,theme,label,'page overflow')
                    if selector:
                        assert not await page.locator(selector).evaluate('e=>e.scrollWidth>e.clientWidth+1'),(width,label,'dialog overflow')
                async def shot(name):
                    await fits(name)
                    if width in (1440,390):
                        await page.evaluate('window.scrollTo(0,0)')
                        await page.screenshot(path=f'/workspace/outputs/interface-{name}-{width}.png',full_page=False)
                await page.goto(base+'/#matches')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await expect(page.locator('.featured-card')).to_have_count(3)
                await expect(page.locator('.tp-card')).to_have_count(6)
                await expect(page.locator('#today-picks')).not_to_contain_text('Бразилия')
                await expect(page.locator('#tp-cards .tp-open').first).to_contain_text('Посмотреть прогноз')
                await shot('home')
                # A source carousel arrow remains a real visible icon on narrow screens.
                assert await page.locator('#featured-next svg').evaluate('e=>e.getBoundingClientRect().width')>=18
                await page.locator('#credits-open').click();await expect(page.locator('#credits-dialog')).to_be_visible()
                await expect(page.locator('#credits-title')).to_have_text('Разборы и подписки')
                await expect(page.locator('.subscription-plan')).to_have_count(3)
                await expect(page.locator('.credit-rule-hero')).to_contain_text('ТОЛЬКО')
                await expect(page.locator('.subscription-money-note')).to_contain_text('каждые 72 часа')
                await fits('tariffs','#credits-dialog');await shot('tariffs')
                await page.locator('#credits-close').click()
                await page.locator('#account-button').click();await expect(page.locator('#auth-dialog')).to_be_visible()
                await expect(page.locator('#auth-form')).to_be_hidden()
                await page.locator('#auth-native-toggle').click();await expect(page.locator('#auth-form')).to_be_visible()
                await fits('auth','#auth-dialog');await shot('auth');await page.locator('#auth-close').click()
                await page.goto(base+'/#results');await expect(page.locator('.pr-row')).to_have_count(5)
                await shot('results')
                await page.locator(f'.pr-row[href="#result/{first["id"]}"]').click()
                await expect(page.locator('#public-result-content')).to_contain_text('2:1')
                await shot('answer')
                await page.goto(base+'/#stream/101');await expect(page.locator('#event-header')).to_be_visible()
                await expect(page.locator('#team-statistics')).to_be_hidden()
                await expect(page.locator('#event-ai-button')).to_be_visible()
                await shot('match')
                response=await ctx.request.post(base+'/api/auth/login',data={'username':'outcomes_admin','password':fixture.PASSWORD})
                assert response.status==200
                await page.evaluate('Account.refresh()')
                await page.goto(base+'/#history');await expect(page.locator('.history-card')).to_have_count(1)
                await page.locator('#history-search').fill('нет такого матча');await expect(page.locator('#history-search-empty')).to_be_visible()
                await page.locator('#history-search').fill('');await expect(page.locator('.history-card')).to_be_visible()
                await shot('history')
                await page.goto(base+'/#admin');await expect(page.locator('#admin-content')).to_be_visible()
                await expect(page.locator('#admin-metrics .admin-metric').first).to_be_visible()
                tabs=page.locator('[data-admin-tab]')
                # Arrows move focus in the strip. Enter keeps normal button activation.
                await tabs.first.focus();await page.keyboard.press('End');await expect(tabs.last).to_be_focused()
                await page.keyboard.press('Home');await expect(tabs.first).to_be_focused()
                for name in ('overview','results','automation','tokens','billing','quality','users','push','promos','links','audit'):
                    await page.locator(f'[data-admin-tab="{name}"]').click()
                    await expect(page.locator(f'[data-admin-panel="{name}"]')).to_be_visible()
                    await expect(page.locator('#admin-refresh')).to_be_enabled()
                    await fits('admin-'+name)
                    if name in ('overview','results','billing','users'):await shot('admin-'+name)
                for route in ('archive','status','calculator'):
                    await page.goto(base+'/#'+route)
                    await expect(page.locator('#view-'+('analysis' if route=='calculator' else route))).to_be_visible()
                    await fits(route)
                    if route!='calculator':await shot(route)
                # Standalone pages use the same theme and fit without the SPA shell.
                await page.goto(base+match_path(archive.document(repo,'101')))
                await expect(page.locator('.seo-analysis')).to_be_visible();await shot('search-match')
                for document in Path('matchlab/static/legal').glob('*.html'):
                    await page.goto(base+'/static/legal/'+document.name)
                    await expect(page.locator('h1')).to_be_visible()
                    await fits('document-'+document.stem)
                await page.goto(base+'/static/legal/pricing.html');await shot('documents')
                assert not errors,(width,errors)
                await ctx.close()
        finally:await browser.close()
    print('PASS: shared interface, populated/empty states, tariffs/disclosures, auth, matches, history, all 11 admin tabs, technical and standalone pages; 1440/1024/390/320; no model/provider calls')


if __name__=='__main__':
    fixture.exercise=exercise
    fixture.main()
