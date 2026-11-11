"""Public guest results, original answers, mobile charts and admin automation switch."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import shutil
from uuid import uuid4

import browser_verdict_outcomes as fixture
from outcome_helpers import forecast,archive_result


def seed(repo):
    with repo.connection() as db:
        db.execute('DELETE FROM event_analyses');db.execute('DELETE FROM archive_latest_results')
    now=datetime.now(timezone.utc)
    sequence=[0]
    def add(home,away,kickoff,score,price=2,notes=False,event=None):
        event=event or {'source_event_id':str(100+int((now-kickoff).total_seconds())),'sport':'football','home':home,'away':away,'competition':'Тестовая лига','phase':'prematch','kickoff_at':kickoff.isoformat()}
        at=kickoff-timedelta(minutes=10)+timedelta(seconds=sequence[0]);sequence[0]+=1
        item=forecast(repo,uuid4().hex,event=event,kickoff=kickoff,at=at,score=score)
        c=item['context'];c.pop('notes',None);c['neural_input']['unverified_user_notes']='PRIVATE_OWNER_NOTE' if notes else ''
        c['neural_input']['quote']['odds']['home']=price;c['neural_verdict']['odds']=price
        c['neural_verdict']['comparison']={'home':{'text':'Исходные результаты первого участника давали основание для предпочтения до матча.'},'away':{'text':'Результаты соперника оставляли встречный довод к выбору первой команды.'}}
        c['neural_verdict']['reason']['text']='Этот аргумент был сохранён до матча и не переписан после результата.'
        with repo.connection() as db:db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(item),item['id']))
        return item
    first=add('Реал Мадрид','Барселона',now-timedelta(days=2),'2:1')
    changed=add('Реал Мадрид','Барселона',now-timedelta(days=2),None,2.4,event=first['event'])
    add('Реал Мадрид','Барселона',now-timedelta(days=2),None,event=first['event']) # duplicate price, not another outcome
    add('Порту','Бенфика',now-timedelta(days=1),'0:2')
    pending=add('Аякс','ПСВ',now-timedelta(hours=3),None)
    private=add('Милан','Интер',now-timedelta(days=3),'1:0',notes=True)
    return first,changed,pending,private


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    first,changed,pending,private=seed(repo)
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for theme,width in [('light',1440),('dark',390),('light',320)]:
                context=await browser.new_context(viewport={'width':width,'height':1050},color_scheme=theme,locale='ru-RU',reduced_motion='reduce')
                page=await context.new_page();page.set_default_timeout(15000);errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                await page.route('**/api/results**',lambda route:route.continue_())
                await page.goto(base+'/#matches')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await expect(page.locator('#results-widget-metrics .pr-metric')).to_have_count(4)
                await expect(page.locator('#results-widget-metrics')).to_contain_text('5')
                await page.screenshot(path=f'/workspace/outputs/public-results-widget-{theme}-{width}.png',full_page=True)
                await page.locator('#results-widget a').click()
                await expect(page.locator('#view-results')).to_be_visible()
                await expect(page.locator('#pr-list .pr-row')).to_have_count(5)
                await expect(page.locator('#auth-dialog')).not_to_be_visible()
                await expect(page.locator('#pr-chart .pr-day')).to_have_count(30)
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                await page.screenshot(path=f'/workspace/outputs/public-results-history-{theme}-{width}.png',full_page=True)
                await page.locator(f'a[href="#result/{first["id"]}"]').click()
                await expect(page.locator('#public-result-title')).to_have_text('Реал Мадрид — Барселона')
                await expect(page.locator('#public-result-content')).to_contain_text('Этот аргумент был сохранён до матча')
                await expect(page.locator('#public-result-content')).to_contain_text('2:1')
                await expect(page.locator('#public-result-content')).not_to_contain_text('Претендент')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                await page.screenshot(path=f'/workspace/outputs/public-results-answer-{theme}-{width}.png',full_page=True)
                await page.goto(base+'/#result/'+private['id'])
                await expect(page.locator('#public-result-content')).to_contain_text('личные заметки')
                await expect(page.locator('#public-result-content')).not_to_contain_text('PRIVATE_')
                if width==1440:
                    archive_result(repo,pending['event'],'3:0','pending-result')
                    await page.goto(base+'/#results')
                    await expect(page.locator('#pr-metrics .pr-metric').first).to_contain_text('80%')
                    await expect(page.locator('#pr-list')).not_to_contain_text('Ожидаем итог')
                # Admin setting uses the existing password confirmation flow.
                await context.request.post(base+'/api/auth/login',data={'username':'outcomes_admin','password':fixture.PASSWORD})
                await page.evaluate('()=>Account.refresh()')
                await page.goto(base+'/#admin');await page.locator('[data-admin-tab="automation"]').click()
                await expect(page.locator('#auto-analysis-status')).not_to_be_empty()
                await expect(page.locator('.auto-lanes')).to_contain_text('1 поток — запросы пользователей')
                if width==1440:
                    await page.locator('#auto-analysis-enabled').uncheck()
                    await page.locator('#auto-analysis-form button').click()
                    await expect(page.locator('#admin-password-dialog')).to_be_visible()
                    await page.locator('#admin-password').fill(fixture.PASSWORD)
                    await page.locator('#admin-password-form button').click()
                    await expect(page.locator('#admin-message')).to_contain_text('Настройка системного анализа сохранена')
                    await expect(page.locator('#auto-analysis-enabled')).not_to_be_checked()
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                await page.screenshot(path=f'/workspace/outputs/public-results-automation-{theme}-{width}.png',full_page=True)
                assert not errors,errors
                await context.close()
        finally:await browser.close()
    print('PASS: guest widget/history/answers; distinct lines; original text; notes redacted; corrections update stats; charts; admin toggle with reauth; 1440/390/320; no model calls')


if __name__=='__main__':
    Path('/workspace/outputs').mkdir(exist_ok=True)
    fixture.exercise=exercise
    fixture.main()
