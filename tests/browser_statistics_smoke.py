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


def widget(blocked=False):
    if blocked: return '<h1>Access Denied</h1><p>You do not have permission to access this server.</p>'
    result='<body data-testid="hydrated"><div data-testid="match-header">'+escape(PUBLIC['header']).replace('\n','<br>')+'</div>'
    for section in PUBLIC['sections']:
        result+='<article class="source-card"><header><h3>'+escape(section['title'])+'</h3></header><div>'+escape(section['text']).replace('\n','<br>')+'</div></article>'
    return result+'<form>private-statistics-form<input value="private-statistics-input"></form></body>'


async def exercise(base,repo):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            context=await browser.new_context()
            blocked=False
            async def fixture(route):
                if '/stats/match/' in route.request.url:
                    body='<iframe src="https://statshub.yunai.tech/winline/ru/match/72334410"></iframe>'
                elif 'statshub.yunai.tech' in route.request.url: body=widget(blocked)
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
            assert len(snapshot['sections'])==3
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
                await page.locator("#team-statistics-status").filter(has_text="краткая сводка").wait_for()
                await page.locator('#team-statistics>summary').click()
                assert 'результат этой встречи' in await page.locator('#team-statistics-note').inner_text()
                groups=await page.locator('.statistics-brief-card h3').all_inner_texts()
                assert 'Статистика сезона' in groups and 'Обзор участников' in groups,groups
                assert '25 Забитые мячи 17' in await page.locator('#team-statistics-sections').inner_text()
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
