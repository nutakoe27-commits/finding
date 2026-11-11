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
    first,changed,pending,private=seed(repo);picks=today_seed(repo)
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for theme,width in [('light',1440),('dark',1440),('light',390),('dark',320)]:
                context=await browser.new_context(viewport={'width':width,'height':1100},color_scheme=theme,locale='ru-RU',reduced_motion='reduce')
                page=await context.new_page();page.set_default_timeout(15000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(base+'/#matches')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await expect(page.locator('#results-widget-metrics .pr-metric')).to_have_count(4)
                await expect(page.locator('#results-widget-metrics .win-rate strong')).to_have_text('75%')
                await expect(page.locator('#results-widget-metrics .pr-metric').last).to_contain_text('+12%')
                color=await page.locator('#results-widget-metrics .win-rate strong').evaluate('e=>getComputedStyle(e).color')
                assert color in ('rgb(35, 123, 83)','rgb(130, 198, 160)'),color
                await page.locator('#widget-stake').fill('10');await page.locator('#widget-stake').dispatch_event('input')
                await expect(page.locator('#results-widget-metrics .pr-metric').last).to_contain_text('+24%')
                await expect(page.locator('#widget-stake-note')).to_contain_text('1\u00a0000 ₽')
                await expect(page.locator('#tp-cards .tp-card')).to_have_count(3)
                await expect(page.locator('#tp-cards')).not_to_contain_text('Пропущенный матч')
                await expect(page.locator('#tp-cards')).not_to_contain_text('Претендент')
                await expect(page.locator('#tp-cards')).not_to_contain_text('Сохранённые результаты участников')
                await expect(page.locator('#tp-cards .tp-open').first).to_have_text('Посмотреть прогноз →')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width)
                await page.locator('#results-widget').screenshot(path=f'/workspace/outputs/bank-example-{theme}-{width}.png')
                await page.locator('#today-picks').screenshot(path=f'/workspace/outputs/today-picks-{theme}-{width}.png')
                await page.locator('#tp-min').fill('3');await page.locator('#tp-max').fill('4');await page.locator('#tp-filters button[type="submit"]').click()
                await expect(page.locator('#tp-cards .tp-card')).to_have_count(1)
                await expect(page.locator('#tp-cards')).to_contain_text('Лейрия')
                await page.locator('#tp-cards .tp-open').click()
                await expect(page.locator('#public-result-title')).to_have_text('Торренсе — Лейрия')
                await expect(page.locator('#public-result-content')).to_contain_text('Решение: ставить')
                await expect(page.locator('#public-result-content')).to_contain_text('Претендент: Торренсе')
                await expect(page.locator('#public-result-content')).to_contain_text('Сохранённые результаты участников')
                await expect(page.locator('#auth-dialog')).not_to_be_visible()
                await page.locator('#public-result-back').click()
                await page.locator('#tp-reset').click();await expect(page.locator('#tp-cards .tp-card')).to_have_count(3)
                await page.locator('#tp-cards .tp-open').first.click()
                await expect(page.locator('#public-result-content')).to_contain_text('Решение: ставить')
                await expect(page.locator('#public-result-content')).to_contain_text('0,25%')
                await page.goto(base+'/#results')
                await expect(page.locator('#pr-list .pr-row')).to_have_count(5)
                await expect(page.locator('#pr-stake')).to_have_value('10')
                await expect(page.locator('#pr-metrics .pr-metric').last).to_contain_text('+24%')
                await page.locator('#pr-stake').fill('5');await page.locator('#pr-stake').dispatch_event('input')
                await expect(page.locator('#pr-metrics .pr-metric').last).to_contain_text('+12%')
                await page.locator(f'#pr-list a[href="#result/{private["id"]}"]').click()
                await expect(page.locator('#public-result-content')).to_contain_text('личные заметки')
                await expect(page.locator('#public-result-content')).not_to_contain_text('PRIVATE_')
                await page.goto(base+'/#matches');await page.locator('#tp-min').fill('50');await page.locator('#tp-max').fill('100');await page.locator('#tp-filters button[type="submit"]').click()
                await expect(page.locator('#tp-cards .tp-card')).to_have_count(0)
                await expect(page.locator('#tp-cards')).to_contain_text('По этим фильтрам прогнозов нет')
                await page.locator('#tp-reset').click();await expect(page.locator('#tp-cards .tp-card')).to_have_count(3)
                if theme=='light' and width==1440:
                    extras=today_seed(repo,21000)+today_seed(repo,22000)
                    await page.locator('#tp-reset').click();await expect(page.locator('#tp-cards .tp-card')).to_have_count(6)
                    await page.locator('#tp-more').click();await expect(page.locator('#tp-cards .tp-card')).to_have_count(9)
                    await expect(page.locator('#tp-more')).to_be_hidden()
                    with repo.connection() as db:db.executemany('DELETE FROM event_analyses WHERE id=?',[(e['id'],) for e in extras])
                assert not errors,errors
                await context.close()
        finally:await browser.close()
    print('PASS: green win rate; 5–10% initial-bank examples; shared sliders; betting-only today picks and odds filters; answer hidden until click; original/redacted answers; empty/reset/more; light/dark 1440/390/320; no model calls')


if __name__=='__main__':
    fixture.exercise=exercise
    fixture.main()
