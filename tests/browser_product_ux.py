"""Targeted source reader + product UX checks in real Chromium, no model calls."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from account_helpers import signed_in_browser
from browser_archive_smoke import feed
from test_archive import header
from matchlab.storage import Repository
from matchlab import archive
from winline.browser import SNAPSHOT


async def source_reader(browser):
    page=await browser.new_page()
    await page.set_content('''<ww-feature-frontpage-dsk>
      <ww-feature-motd-card-dsk><span class="main-event__title">Хозяева A</span><span class="main-event__title">Гости A</span></ww-feature-motd-card-dsk>
      <ww-feature-live-panel-dsk block="top">
        <ww-feature-event-mini-card-dsk><a href="https://winline.ru/stavki/event/102">Match</a><a href="/stavki/event/102">Duplicate</a></ww-feature-event-mini-card-dsk>
        <ww-feature-event-mini-card-dsk><a href="/live/sport/hokkej/103">Live</a></ww-feature-event-mini-card-dsk>
        <ww-feature-event-mini-card-dsk><a href="https://example.org/stavki/event/104">Wrong origin</a></ww-feature-event-mini-card-dsk>
      </ww-feature-live-panel-dsk>
      <ww-feature-live-panel-dsk block="live"><ww-feature-event-mini-card-dsk><a href="/live/event/104">Not main</a></ww-feature-event-mini-card-dsk></ww-feature-live-panel-dsk>
    </ww-feature-frontpage-dsk>''')
    # Use source URL resolution without making external network requests.
    await page.route('https://winline.ru/**',lambda route:route.fulfill(status=200,content_type='text/html',body='<body></body>'))
    content=await page.content();await page.goto('https://winline.ru/');await page.set_content(content)
    await page.evaluate('''()=>{
      const events=[{id:101,members:['Хозяева A','Гости A']},{id:102,members:['A','B']},{id:103,members:['C','D']},{id:104,members:['E','F']}];
      window.apiWlb={version:'fixture',client:{websocket:{readyState:1}},getEvents:()=>({toArray:()=>events}),getService:()=>({query:()=>[],getEventMainLine:()=>null}),getSports:()=>({serialize:()=>[]}),getCountries:()=>({serialize:()=>[]}),getChampionships:()=>({serialize:()=>[]})};
    }''')
    data=json.loads(await page.evaluate(SNAPSHOT,{'all_lines':False}))
    assert {e['id']:e['_featured_rank'] for e in data['events']}=={101:0,102:1,103:2,104:None}
    await page.locator('ww-feature-frontpage-dsk').evaluate('(node)=>node.remove()')
    data=json.loads(await page.evaluate(SNAPSHOT,{'all_lines':False}))
    assert all(e['_featured_rank'] is None for e in data['events'])
    await page.close()


async def journey_checks(page,base,repo,fixtures,width):
    from playwright.async_api import expect
    await page.locator('#match-search').fill('Спартак')
    await expect(page.locator('[data-filter-key="match-search"]')).to_contain_text('Спартак')
    await expect(page.locator('#match-search-clear')).to_be_visible()
    await page.locator('#match-search').press('Escape')
    await expect(page.locator('#match-search')).to_have_value('')
    await expect(page.locator('#featured-events')).to_be_visible()
    await page.locator('#day-filter').select_option('tomorrow')
    await expect(page.locator('[data-filter-key="day-filter"]')).to_contain_text('Завтра')
    await page.locator('[data-filter-key="day-filter"]').click()
    await expect(page.locator('#day-filter')).to_have_value('')
    await page.locator('#competition-filter').select_option('Россия. Премьер-лига')
    await expect(page.locator('[data-filter-key="competition-filter"]')).to_be_visible()
    await page.wait_for_function('()=>document.getElementById("market-event-rows").getAttribute("aria-busy")==="false"')
    link=page.locator('.match-card[data-event-id="106"] .match-open')
    await link.scroll_into_view_if_needed()
    before=await page.evaluate('scrollY')
    await link.click();await page.wait_for_function('()=>state.marketDocument?.source_event_id==="106"')
    await page.locator('#match-back').click()
    await page.wait_for_function('()=>state.route==="#matches"&&document.getElementById("market-event-rows").getAttribute("aria-busy")==="false"')
    await expect(page.locator('#competition-filter')).to_have_value('Россия. Премьер-лига')
    await page.wait_for_function('(y)=>Math.abs(scrollY-y)<6',arg=before)
    await page.locator('[data-filter-key="competition-filter"]').click()
    focus=page.locator('[data-focus-key="101:ai"]')
    await focus.focus();await page.evaluate('()=>loadCatalogue()');await expect(focus).to_be_focused()
    await page.locator('.sport-tab').first.focus();await page.evaluate('()=>loadCatalogue()')
    await expect(page.locator('.sport-tab').first).to_be_focused()
    if width<=650:
        rect=await page.locator('.sidebar nav').bounding_box()
        assert rect['y']+rect['height']<=1051 and rect['height']<100,rect
    async def unavailable(route):await route.fulfill(status=503,content_type='application/json',body='{"detail":"fixture unavailable"}')
    await page.route('**/api/bookmakers/events?*',unavailable)
    await page.locator('#market-refresh').click();await expect(page.locator('#catalog-retry')).to_be_visible()
    await page.unroute('**/api/bookmakers/events?*',unavailable)
    await page.locator('#catalog-retry').click();await expect(page.locator('#catalog-retry')).to_be_hidden()
    await expect(page.locator('#market-list-error')).to_be_hidden()
    if width==1440:
        extra=[]
        for i in range(200,235):
            value=header(str(i));value.update(members=['Тестовая команда '+str(i),'Соперник'],competition='Тестовая лига')
            extra.append(value)
        feed(repo,fixtures+extra)
        await page.evaluate('()=>loadCatalogue()');await expect(page.locator('#market-next')).to_be_enabled()
        await page.locator('#market-next').click();await page.wait_for_function('()=>state.marketOffset===30&&document.querySelectorAll(".match-card").length<30')
        link=page.locator('.match-card .match-open').first
        event_id=await link.evaluate('node=>node.closest(".match-card").dataset.eventId')
        await link.click();await page.wait_for_function('(id)=>state.marketDocument?.source_event_id===id',arg=event_id)
        await page.locator('#match-back').click();await page.wait_for_function('()=>state.marketOffset===30&&state.route==="#matches"')
        await expect(page.locator('#catalog-page')).to_have_text('2 / 2')
        for value in fixtures:
            value['observed_at']=archive.now();value['primary']['observed_at']=value['observed_at']
        feed(repo,fixtures);await page.evaluate('()=>resetFilters()')
        await page.wait_for_function('()=>state.marketOffset===0&&document.querySelectorAll(".match-card").length<10')
    await page.goto(base+'/#history');await expect(page.locator('#history-guest')).to_be_visible()
    await expect(page.locator('#auth-dialog')).not_to_be_visible()
    await page.locator('#history-login').click();await expect(page.locator('#auth-dialog')).to_be_visible()
    await page.locator('#auth-password').fill('private-browser-fixture')
    await page.locator('#auth-password-toggle').click();await expect(page.locator('#auth-password')).to_have_attribute('type','text')
    await page.locator('#auth-password-toggle').click();await expect(page.locator('#auth-password')).to_have_attribute('type','password')
    await page.locator('#auth-close').click()
    await page.goto(base+'/#matches')
    await page.evaluate('()=>{ProductUX.startWait();ProductUX.wait("statistics")}')
    await expect(page.locator('#ai-progress')).to_have_attribute('data-stage','data')
    assert 'Задание принято' not in await page.locator('#ai-wait-note').inner_text()
    await page.evaluate('()=>ProductUX.wait("background")')
    await expect(page.locator('#ai-progress')).to_have_attribute('data-stage','analysis')
    await expect(page.locator('#ai-wait-note')).to_contain_text('Задание принято')


async def exercise(base,repo,fixtures):
    from playwright.async_api import async_playwright,expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for theme,width in [('light',1440),('dark',1024),('dark',390),('light',320)]:
                context=await browser.new_context(viewport={'width':width,'height':1050},color_scheme=theme,reduced_motion='reduce',locale='ru-RU',permissions=['clipboard-read','clipboard-write'])
                page=await context.new_page();page.set_default_timeout(12000);errors=[];requests=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda request:requests.append(request.url))
                await page.goto(base+'/#matches')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await page.evaluate('document.documentElement.dataset.theme='+repr(theme))
                await expect(page.locator('#featured-track .featured-card')).to_have_count(8)
                order=await page.locator('#featured-track .featured-card').evaluate_all('nodes=>nodes.map(n=>n.dataset.eventId)')
                assert order==[str(i) for i in (108,107,106,105,104,103,102,101)],order
                await expect(page.locator('#featured-track')).to_contain_text('Хоккей')
                await page.locator('#featured-next').click();await page.wait_for_function('()=>document.getElementById("featured-track").scrollLeft>0')
                await page.locator('#featured-prev').click();await page.wait_for_function('()=>document.getElementById("featured-track").scrollLeft<=3')
                for target in ('a.nav-link[href="#archive"]','#count-fresh','#catalog-updated'):
                    await expect(page.locator(target)).to_be_hidden()
                await expect(page.locator('#quick-guide')).to_be_hidden()
                await page.locator('#guide-toggle').click();await expect(page.locator('#quick-guide')).to_be_visible();await page.locator('#guide-close').click()
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width,'catalogue overflow')
                await page.screenshot(path=f'/workspace/outputs/product-matches-{theme}-{width}.png',full_page=True)
                await journey_checks(page,base,repo,fixtures,width)
                await page.locator('[data-phase="live"]').click()
                await expect(page.locator('#featured-track .featured-card')).to_have_count(8)
                await page.locator('#match-search').fill('Матч');await expect(page.locator('#featured-events')).to_be_hidden()
                await page.locator('#match-search').fill('');await expect(page.locator('#featured-events')).to_be_visible()
                for route in ('archive','status','calculator'):
                    await page.goto(base+'/#'+route);await page.wait_for_function('()=>state.route==="#matches"')
                await page.goto(base+'/#stream/101');await page.wait_for_function('()=>state.marketDocument?.source_event_id==="101"')
                for target in ('#team-statistics','#archive-team-history','#tab-analysis','#analysis-panel'):
                    await expect(page.locator(target)).to_be_hidden()
                await expect(page.locator('#ai-mobile-host #ai-desk')).to_be_visible()
                await expect(page.locator('#market-detail')).to_be_visible()
                assert not any('/101/statistics' in u or '/101/history?' in u or '/101/analysis' in u for u in requests),'ordinary UI loaded technical data'
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width,'match overflow')
                await page.screenshot(path=f'/workspace/outputs/product-match-{theme}-{width}.png',full_page=True)
                await signed_in_browser(page,base);await page.evaluate('Account.refresh()')
                await expect(page.locator('#team-statistics')).to_be_hidden()
                await page.goto(base+'/#history');await expect(page.locator('#view-history')).to_be_visible()
                owner=await page.evaluate('Account.user.id')
                identifier=f'{width:032x}';document=archive.document(repo,'101')
                verdict={'engine':'data_guard','decision':'skip','preference':None,'preference_label':'Никто','data_forced_skip':True,'stake_percent':0,'arguments':[],'reason':{'text':'Для обоснованного выбора пока недостаточно сведений.'}}
                repo.save_event_analysis({'id':identifier,'generated_at':archive.now(),'observed_at':document['observed_at'],'event':document,'market_name':'Событие целиком','analysis_type':'neural_verdict','text':'Претендент: Никто. Решение: Пропустить матч.','context':{'neural_verdict':verdict,'markets':[],'selection':{},'snapshot':{'observed_at':document['observed_at']}}},'browser-'+identifier,user_id=owner)
                await page.goto(base+'/#analysis/'+identifier)
                await expect(page.locator('#manual-heading h1')).to_have_text('Спартак — Зенит')
                await expect(page.locator('#saved-analysis .verdict-preference')).to_have_text('Никто')
                await expect(page.locator('#saved-analysis .verdict-choice')).to_have_text('Пропустить матч')
                await expect(page.locator('#saved-analysis-coverage')).to_be_hidden()
                assert 'снимок' not in await page.locator('#manual-heading').inner_text()
                await page.evaluate('''()=>{const d=document.createElement('details'),s=document.createElement('summary'),p=document.createElement('p');s.textContent='Ограничения';p.textContent='Ограничение тестового отчёта';d.append(s,p);document.getElementById('saved-analysis-text').append(d)}''')
                await page.locator('[data-copy-target="saved-analysis-text"]').click()
                await expect(page.locator('[data-copy-target="saved-analysis-text"]')).to_have_text('Скопировано')
                copied=await page.evaluate('()=>navigator.clipboard.readText()')
                assert 'Пропустить матч' in copied and 'Ограничение тестового отчёта' in copied
                await page.goto(base+'/#history');await expect(page.locator('.history-card')).to_have_count(1)
                await page.locator('#history-search').fill('Неизвестный клуб');await expect(page.locator('#history-search-empty')).to_be_visible()
                await page.locator('#history-search').fill('Спартак');await expect(page.locator('#history-search-empty')).to_be_hidden()
                await expect(page.locator('.history-card:visible')).to_have_count(1)
                await page.locator('#history-search').fill('')
                await page.screenshot(path=f'/workspace/outputs/product-history-{theme}-{width}.png',full_page=True)

                username=await page.evaluate('Account.user.username')
                await page.goto(base+'/#analysis/'+identifier)
                await expect(page.locator('#saved-analysis .verdict-preference')).to_have_text('Никто')
                await page.evaluate('async()=>{await Account.api("/api/auth/logout",{method:"POST",body:"{}"});await Account.refresh()}')
                await page.wait_for_function('()=>state.route==="#history"')
                await expect(page.locator('#history-guest')).to_be_visible()
                login=await context.request.post(base+'/api/auth/login',data={'username':username,'password':'browser-fixture-password-123'})
                assert login.status==200,await login.text()
                await page.evaluate('Account.refresh()')
                # Promote the fixture through the repository, then refresh the real session.
                username=await page.evaluate('Account.user.username')
                with repo.connection() as db:db.execute('UPDATE users SET role=? WHERE username=?',('admin',username))
                await page.evaluate('Account.refresh()');await page.goto(base+'/#stream/101')
                await page.wait_for_function('()=>state.marketDocument?.source_event_id==="101"')
                await expect(page.locator('#team-statistics')).to_be_visible()
                await expect(page.locator('a.nav-link[href="#archive"]')).to_be_visible()
                await page.goto(base+'/#archive');await expect(page.locator('#view-archive')).to_be_visible()
                await page.evaluate('async()=>{await Account.api("/api/auth/logout",{method:"POST",body:"{}"});await Account.refresh()}')
                await page.wait_for_function('()=>state.route==="#matches"')
                await expect(page.locator('#view-archive')).to_be_hidden()
                assert not errors,errors
                await context.close()
        finally:await browser.close()
    print('PASS: catalogue/search/filter chips; focus and scroll restoration; pagination; retry; password visibility; honest wait stages; history search and clipboard; guest/user/admin; 1440/1024/390/320 light/dark; no model calls')


def main():
    Path('/workspace/outputs').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='product-ux-') as folder:
        repo=Repository(Path(folder)/'matchlab.sqlite3');repo.initialize()
        events=[]
        for i in range(101,109):
            value=header(str(i),live=i==107,score='2:1' if i==107 else '0:0')
            value.update(members=['Спартак' if i==101 else 'Команда '+str(i),'Зенит'],competition='Россия. Премьер-лига',featured_rank=108-i)
            if i==108:value.update(sport_id=4,sport_label='Хоккей',competition='КХЛ')
            events.append(value)
        feed(repo,events)
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,PUBLIC_ORIGIN=base,QWEN_BASE_URL='',QWEN_MODEL='',EVENT_ANALYSIS_MODE='neural-verdict',BOOKMAKER_INGEST_TOKEN='')
        with open('/tmp/product-ux-server.log','w') as log:
            server=subprocess.Popen([sys.executable,'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:opener.open(base+'/api/bookmakers/events',timeout=1).close();break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('App did not start')
                asyncio.run(exercise(base,repo,events))
            finally:server.terminate();server.wait(timeout=20)

if __name__=='__main__':main()
