"""Read actual selector contracts with offline StatsHub/public fragments and a real API."""
import asyncio
from datetime import datetime, timezone
from html import escape
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
from matchlab.statistics import collect_statistics
from matchlab.storage import Repository
from matchlab.winline_markets import discover_statistics_link
from sports_fixtures import PROJECTIONS, document

PUBLIC=json.loads((ROOT/'tests/fixtures/team_statistics_projection.json').read_text())


def widget(blocked=False, view='overview', frozen=False, dynamic=False, delayed=False):
    if blocked: return '<h1>Access Denied</h1><p>You do not have permission to access this server.</p>'
    result='<body data-testid="hydrated"><div data-testid="match-header">'+escape(PUBLIC['header']).replace('\n','<br>')+'</div>'
    result+='<nav><a href="/winline/ru/match/72334410">Обзор</a><a href="/winline/ru/match/72334410/report">Отчет о матче</a><a href="/winline/ru/match/72334410/statistics">Статистика</a></nav>'
    if view=='report': return result+'<p>Матч не начался</p></body>'
    if view=='statistics': return result+'<article><h3>Таблица | 1. Liga</h3><div>Велес — 28 очков</div></article></body>'
    for section in PUBLIC['sections']:
        if section['title']=='Статистика сезона':
            result+='<article class="source-card" id="season"><header><h3>Статистика сезона</h3>'
            for group, labels in (('mode',['Итоги','За матч','Серия']),('scope',['Общее','Хозяева / Гости'])):
                for index,label in enumerate(labels):
                    result+='<button data-group="'+group+'" aria-pressed="'+('true' if index==0 else 'false')+'" onclick="changeStats(this)">'+label+'</button>'
            result+='</header><div id="season-data">'+escape(section['text']).replace('\n','<br>')+'<br>Итоги Общее</div></article>'
        else:
            result+='<article class="source-card"><header><h3>'+escape(section['title'])+'</h3></header><div>'+escape(section['text']).replace('\n','<br>')+'</div></article>'
    result+='<article><h3>Статистика матча</h3><div>Удары в створ: 5 — 3</div></article>'
    result+='<article><h3>Лучшие Игроки</h3><div>Tarasenko, Zakhar — 4 гола</div></article>'
    result+='<article><h3>Сезонная форма</h3><div>Велес — форма 40%</div></article>'
    result+="""<script>function changeStats(button) {
      document.querySelectorAll('#season button[data-group="'+button.dataset.group+'"]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
      if (FROZEN) return;
      const selected=[...document.querySelectorAll('#season button[aria-pressed="true"]')].map(b=>b.innerText);
      document.querySelector('#season-data').textContent='Показатели: '+selected.join(' / ');
    }</script>""".replace('FROZEN','true' if frozen else 'false')
    if dynamic:
        result+='<div id="unrelated-clock">0</div><script>setInterval(()=>document.querySelector("#unrelated-clock").textContent=String(Date.now()),100)</script>'
    result+='<form>private-statistics-form<input value="private-statistics-input"></form></body>'
    if delayed:
        result=result.replace('<body data-testid="hydrated">',
            '<body data-testid="hydrated" style="display:none"><script>setTimeout(()=>document.body.style.display="block",2800)</script>',1)
    return result


async def exercise(base,repo):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            context=await browser.new_context()
            blocked=False
            blocked_report=False
            frozen=False
            dynamic=False
            delayed=False
            async def fixture(route):
                if blocked_report and route.request.url.endswith('/report'):
                    await route.fulfill(status=403,content_type='text/html',body='Access Denied')
                    return
                if '/stats/match/' in route.request.url:
                    body='<iframe src="https://statshub.yunai.tech/winline/ru/match/72334410"></iframe>'
                elif 'statshub.yunai.tech' in route.request.url:
                    view=route.request.url.rsplit('/',1)[-1]
                    body=widget(blocked,view if view in ('report','statistics') else 'overview',frozen,dynamic,delayed)
                elif '/stavki/sport/' in route.request.url: body='<p>Market fixture</p>'
                else: raise AssertionError(route.request.url)
                await route.fulfill(status=200,content_type='text/html',body='<meta charset="utf-8">'+body)
            await context.route('**/*',fixture)
            page=await context.new_page()
            parent=repo.event_markets('123')
            snapshot=await collect_statistics(page,parent,wait_seconds=8)
            assert snapshot['state']=='ok',snapshot
            assert snapshot['match_phase']=='finished'
            assert '80 / 100' in snapshot['header']
            assert all('private-statistics' not in section['text'] for section in snapshot['sections'])
            repo.import_statistics(snapshot)
            assert len(snapshot['sections'])==13
            assert snapshot['observed_views']==['overview','report','statistics']
            assert snapshot['season_filters_complete'] is True
            variants=[s for s in snapshot['sections'] if s.get('filters')]
            assert len(variants)==6 and len({tuple(s['filters']) for s in variants})==6
            assert any(s['filters']==['За матч','Общее'] and 'За матч / Общее' in s['text'] for s in variants)
            assert any(s['view']=='statistics' and 'Велес' in s['text'] for s in snapshot['sections'])
            assert all(s['observed_at']<=snapshot['attempted_at'] for s in snapshot['sections'])
            dynamic=True
            ticking=await collect_statistics(page,parent,wait_seconds=5)
            assert ticking['state']=='ok' and ticking['season_filters_complete'] is True,ticking
            assert not any('unrelated-clock' in s['text'] for s in ticking['sections'])
            dynamic=False
            delayed=True
            late=await collect_statistics(page,parent,wait_seconds=3.3)
            assert late['state']=='ok' and late['sections'],late
            delayed=False
            frozen=True
            stale=await collect_statistics(page,parent,wait_seconds=8)
            assert stale['state']=='ok' and stale['season_filters_complete'] is False
            assert [s['filters'] for s in stale['sections'] if s.get('filters')]==[['Итоги','Общее']]
            frozen=False
            blocked_report=True
            partial_block=await collect_statistics(page,parent,wait_seconds=8)
            assert partial_block['state']=='blocked' and 'sections' not in partial_block
            blocked_report=False
            assert any(s['title']=='Статистика матча' and 'Удары в створ' in s['text'] for s in snapshot['sections'])
            assert any(s['title']=='Лучшие Игроки' and 'Tarasenko' in s['text'] for s in snapshot['sections'])
            assert any(s['title']=='Сезонная форма' and '40%' in s['text'] for s in snapshot['sections'])
            blocked=True
            failure=await collect_statistics(page,parent,wait_seconds=5)
            assert failure['state']=='blocked' and 'sections' not in failure
            repo.import_statistics(failure)
            assert repo.statistics('123')['sections']==snapshot['sections']
            # An explicit statistics icon can expose the real ID via a popup;
            # its ID must not be derived from the bookmaker's event number.
            for event_id,statistics_id in (('123','72334410'),('124','72334422')):
                await page.goto(parent['url'].rsplit('/',1)[0]+'/'+event_id)
                await page.set_content('<div class="ww-markets"><button class="plain-button" onclick="window.open(\'https://winline.ru/stats/match/'+statistics_id+'\')">Статистика</button><button class="market__row-btn" onclick="window.betClicks++">Статистика</button></div><a href="https://winline.ru/stats/match/999">Чужая статистика вне матча</a><script>window.betClicks=0;</script>')
                links=await discover_statistics_link(page,{'statistics_links':[]})
                assert links==['https://winline.ru/stats/match/'+statistics_id],(event_id,links)
                assert await page.evaluate('window.betClicks')==0
            await context.close()
            for width in (1440,390):
                page=await browser.new_page(viewport={'width':width,'height':900})
                errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto(base+'/#match/123')
                await page.locator('#age-accept').click()
                await page.locator('#privacy-reject').click()
                await page.locator("#team-statistics-status").filter(has_text="краткая сводка").wait_for()
                await page.locator('#team-statistics>summary').click()
                assert 'результат этой встречи' in await page.locator('#team-statistics-note').inner_text()
                groups=await page.locator('.statistics-brief-card h3').all_inner_texts()
                assert 'Статистика сезона' in groups and 'Обзор участников' in groups,groups
                assert '25 Забитые мячи 17' in await page.locator('#team-statistics-sections').inner_text()
                assert 'Удары в створ' in await page.locator('#team-statistics-sections').inner_text()
                assert 'Лучшие Игроки' in groups and 'Tarasenko' in await page.locator('#team-statistics-sections').inner_text()
                await page.evaluate('''() => renderTeamStatistics({statistics:null,link_status:'found',source_blocked_until:new Date(Date.now()+3600000).toISOString()})''')
                assert 'Источник временно ограничил доступ' in await page.locator('#team-statistics-note').inner_text()
                await page.evaluate('''() => renderTeamStatistics({statistics:null,link_status:'missing',source_blocked_until:new Date(Date.now()+3600000).toISOString()})''')
                assert 'пока недоступна' in await page.locator('#team-statistics-note').inner_text()
                assert not await page.evaluate('document.documentElement.scrollWidth>window.innerWidth')
                assert not errors,errors
                await page.close()
        finally: await browser.close()
    print('PASS: public StatsHub extraction, finished-result provenance, blocked-page rejection, cache preservation, different statistics buttons for two events, unrelated-link rejection and desktop/mobile interface')


def main():
    with tempfile.TemporaryDirectory(prefix='statistics-browser-') as folder:
        repo=Repository(Path(folder)/'matchlab.sqlite3'); repo.initialize()
        parent=document(PROJECTIONS[0]); parent.update(source_event_id='123',url='https://winline.ru/stavki/sport/futbol/rossiya/rpl/123',home='Спартак М',away='Акрон Тольятти',statistics_url='https://winline.ru/stats/match/72334410')
        repo.import_event_markets(parent)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,QWEN_BASE_URL='',QWEN_MODEL='',BOOKMAKER_INGEST_TOKEN='')
        server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            base=f'http://127.0.0.1:{port}'
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                try: opener.open(base+'/api/bookmakers/events',timeout=1).close();break
                except OSError:time.sleep(.1)
            else:raise AssertionError('API did not start')
            asyncio.run(exercise(base,repo))
        finally:server.terminate();server.wait(timeout=20)


if __name__=='__main__':main()
