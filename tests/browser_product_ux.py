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


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            await source_reader(browser)
            for theme,width in [('light',1440),('dark',390),('light',320)]:
                context=await browser.new_context(viewport={'width':width,'height':1050},color_scheme=theme,reduced_motion='reduce',locale='ru-RU')
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
    print('PASS: source DOM order and missing-source reset; mixed-sport carousel; filters/auth; admin/guest/user separation and logout; desktop/mobile light/dark; no model calls')


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
                asyncio.run(exercise(base,repo))
            finally:server.terminate();server.wait(timeout=20)

if __name__=='__main__':main()
