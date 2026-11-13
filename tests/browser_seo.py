"""SSR landing pages without JS, and ordinary in-app match navigation."""
import shutil

import browser_verdict_outcomes as fixture
from browser_archive_smoke import feed
from test_archive import header


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    from matchlab import archive
    from matchlab.seo import match_path
    a=header('101');a.update(members=['Темпл Оулс','Чарльстон Кугарс'],featured_rank=0)
    b=header('102');b['members']=['Первая команда','Вторая команда']
    feed(repo,[a,b]);path=match_path(archive.document(repo,'101'))
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for theme,width in [('light',1440),('dark',390),('light',320)]:
                context=await browser.new_context(viewport={'width':width,'height':1050},locale='ru-RU',color_scheme=theme,java_script_enabled=False)
                page=await context.new_page();page.set_default_timeout(12000)
                await page.goto(base+'/matches/')
                await expect(page.locator('h1')).to_have_text('Матчи и разборы от ИИ')
                await expect(page.locator('.seo-card')).to_have_count(2)
                await page.locator('a[href="'+path+'"]').click()
                await expect(page.locator('h1')).to_have_text('Темпл Оулс — Чарльстон Кугарс')
                await expect(page).to_have_title(__import__('re').compile('Темпл Оулс.*разбор ИИ'))
                await expect(page.locator('.seo-analysis')).to_contain_text('Готового вердикта на этой странице нет')
                await expect(page.locator('script[type="application/ld+json"]')).to_have_count(2)
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width)
                assert await page.locator('link[rel="canonical"]').get_attribute('href')==base+path
                await page.screenshot(path=f'/workspace/outputs/seo-match-{theme}-{width}.png',full_page=True)
                await context.close()
            context=await browser.new_context(viewport={'width':1440,'height':1050},locale='ru-RU')
            page=await context.new_page();page.set_default_timeout(15000);errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            await page.goto(base+'/#matches')
            await expect(page).to_have_title('Спортивные разборы от ИИ — prognozich ai')
            if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
            if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
            title=page.locator('.match-card[data-event-id="101"] .match-card-title')
            await expect(title).to_have_attribute('href',path)
            await page.evaluate('window.__seo_navigation_probe=123')
            await title.click();await page.wait_for_url('**/#stream/101')
            assert await page.evaluate('window.__seo_navigation_probe')==123
            await page.goto(base+'/#matches')
            main=page.locator('.featured-card[data-event-id="101"] .featured-main')
            await expect(main).to_have_attribute('href',path)
            await page.evaluate('window.__seo_navigation_probe=456')
            await main.click();await page.wait_for_url('**/#stream/101')
            assert await page.evaluate('window.__seo_navigation_probe')==456
            assert not errors,errors
            await context.close()
        finally:await browser.close()
    print('PASS: complete SSR content without JavaScript; unique metadata/schema; 1440/390/320; crawlable match links preserve normal SPA navigation; no AI calls')


if __name__=='__main__':
    fixture.exercise=exercise
    fixture.main()
