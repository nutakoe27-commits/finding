"""Virtual bank and daily system picks, using only local saved fixtures."""
from datetime import datetime,timedelta,timezone
import json
import shutil
from uuid import uuid4

import browser_verdict_outcomes as fixture
from browser_public_results import seed
from outcome_helpers import forecast


def today_seed(repo,base_id=20000):
    now=datetime.now(timezone.utc);items=[]
    rows=[('Бразилия','Аргентина','bet','home',1.8),('Португалия','Греция','bet','away',2.2),('Торренсе','Лейрия','bet','home',3.4),('Пропущенный матч','Соперник','skip','away',3.4),('Без выбора','Соперник','skip',None,None)]
    for i,(home,away,decision,preference,price) in enumerate(rows):
        event={'source_event_id':str(base_id+i),'sport':'football','home':home,'away':away,'competition':'Международный турнир · групповой этап','phase':'prematch','kickoff_at':(now+timedelta(hours=i+1)).isoformat()}
        item=forecast(repo,uuid4().hex,event=event,at=now-timedelta(seconds=20),decision=decision,preference=preference,score=None)
        c=item['context'];c.pop('notes',None);c['analysis_origin']='system';c['neural_input']['unverified_user_notes']=''
        c['neural_verdict']['reason']['text']='Этот системный ответ сохранён до матча. Встречные доводы не позволяют рекомендовать ставку.' if decision=='skip' else 'Сохранённые результаты участников дали основание для выбора этой команды.'
        if preference:
            c['neural_input']['quote']['odds'][preference]=price
            if decision=='bet':c['neural_verdict']['odds']=price
        with repo.connection() as db:db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(item),item['id']))
        items.append(item)
    return items


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    from browser_archive_smoke import feed
    from test_archive import header
    seed(repo);picks=today_seed(repo)
    event=header('101');event['featured_rank']=0;feed(repo,[event])
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for theme,width in [('light',1440),('dark',390),('light',320)]:
                context=await browser.new_context(viewport={'width':width,'height':1100},color_scheme=theme,locale='ru-RU',reduced_motion='reduce')
                page=await context.new_page();page.set_default_timeout(15000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(base+'/#matches')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await expect(page.locator('#featured-events')).to_be_visible()
                assert await page.locator('#catalog-title').count()==1
                assert await page.locator('#featured-events').evaluate("e=>e.previousElementSibling.contains(document.getElementById('catalog-title'))")
                await expect(page.locator('#tp-cards .tp-card')).to_have_count(3)
                await expect(page.locator('#today-picks form,#tp-min,#tp-max')).to_have_count(0)
                await expect(page.locator('#tp-cards')).not_to_contain_text('Бразилия')
                await expect(page.locator('#tp-cards')).not_to_contain_text('Пропущенный матч')
                await expect(page.locator('#tp-cards')).not_to_contain_text('Сохранённые результаты участников')
                blurred=page.locator('.tp-match-blurred').first
                assert await blurred.evaluate('e=>getComputedStyle(e).filter')=='blur(6px)'
                await expect(blurred).to_have_attribute('aria-hidden','true')
                await expect(page.locator('#tp-cards .tp-open').first).to_have_text('Посмотреть прогноз →')
                assert 'Бразилия' not in await page.locator('#tp-cards .tp-open').first.get_attribute('aria-label')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width)
                await page.locator('#today-picks').screenshot(path=f'/workspace/outputs/today-hidden-{theme}-{width}.png')
                await page.locator('#tp-cards .tp-open').first.click()
                await expect(page.locator('#public-result-title')).to_have_text('Бразилия — Аргентина')
                await expect(page.locator('#public-result-content')).to_contain_text('Решение: ставить')
                await expect(page.locator('#public-result-content')).to_contain_text('Претендент: Бразилия')
                await expect(page.locator('#public-result-content')).to_contain_text('Сохранённые результаты участников')
                await expect(page.locator('#auth-dialog')).not_to_be_visible()
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width)
                await page.route('**/api/picks/today',lambda route:route.fulfill(status=503,body='unavailable'),times=1)
                await page.locator('#public-result-back').click()
                await expect(page.locator('#tp-retry')).to_be_visible()
                await page.locator('#tp-retry').click()
                await expect(page.locator('#tp-cards .tp-card')).to_have_count(3)
                await expect(page.locator('#tp-retry')).to_be_hidden()
                await expect(page.locator('#tp-status')).to_contain_text('3 разбора')
                await page.locator('#market-refresh').click()
                await expect(page.locator('#featured-track .featured-card')).to_have_count(1)
                assert not errors,errors
                await context.close()
        finally:await browser.close()
    print('PASS: heading before featured; no coefficient form; blurred anonymous previews; names and answer reveal on click; retry; 1440/390/320px; no model calls')


if __name__=='__main__':
    fixture.exercise=exercise
    fixture.main()
