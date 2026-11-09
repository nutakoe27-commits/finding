"""Real UI, refresh queue, browser, local WebSocket feed and receiver; no external sites."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import httpx
from aiohttp import web
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from browser_market_smoke import fixture_html
from browser_statistics_smoke import widget
from matchlab.statistics_worker import refresh_job
from matchlab.storage import Repository
from sports_fixtures import PROJECTIONS,document

KEY='refresh-test-collector-key-1234567890'


async def exercise(base,repo):
    async def websocket(request):
        ws=web.WebSocketResponse(); await ws.prepare(request)
        await ws.send_str('fixture update')
        async for message in ws: pass
        return ws
    app=web.Application(); app.router.add_get('/data_ng',websocket)
    runner=web.AppRunner(app); await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
    port=site._server.sockets[0].getsockname()[1]
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(headless=True,executable_path=shutil.which('chromium'),
            args=['--host-resolver-rules=MAP feed.winline.ru 127.0.0.1','--disable-web-security','--allow-running-insecure-content','--no-proxy-server'])
        try:
            source=await browser.new_context(locale='ru-RU',timezone_id='Europe/Moscow')
            html=fixture_html().replace('Сандерленд','Спартак М').replace('Челси','Акрон Тольятти')
            html=html.replace('<div class="prematch-info">','<a href="https://winline.ru/stats/match/72334410">Статистика</a><div class="prematch-info">')
            html+=f'<script>window.feed=new WebSocket("ws://feed.winline.ru:{port}/data_ng");</script>'
            blocked=[False]; stat_requests=[]
            async def route(request):
                url=request.request.url
                if '/stats/match/' in url:
                    body='<iframe src="https://statshub.yunai.tech/winline/ru/match/72334410"></iframe>'
                elif 'statshub.yunai.tech' in url:
                    stat_requests.append(url); body=widget(blocked[0])
                elif '/stavki/sport/' in url: body=html.replace('/stats/match/72334410','/stats/match/72334422') if url.endswith('/124') else html
                else: raise AssertionError(url)
                await request.fulfill(status=200,content_type='text/html',body='<meta charset="utf-8">'+body)
            await source.route('**/*',route)
            front=await browser.new_page(viewport={'width':1440,'height':900})
            errors=[]; front.on('pageerror',lambda error:errors.append(str(error)))
            await front.goto(base+'/#match/123')
            await front.locator('#age-accept').click(); await front.locator('#privacy-reject').click()
            await front.locator('#event-refresh').wait_for(state='visible')
            await front.locator('#event-refresh').click()
            await front.locator("#event-refresh-status").filter(has_text='очереди').wait_for()
            async with httpx.AsyncClient(base_url=base,headers={'X-Matchlab-Collector-Key':KEY},timeout=30,trust_env=False) as client:
                claim=(await client.post('/api/bookmakers/refresh/claim',json={})).json()['job']
                assert claim and claim['event']['source_event_id']=='123',claim
                result=await refresh_job(source,client,claim)
                assert result=={'source_event_id':'123','market_state':'ok','statistics_state':'ok'},result
                await front.locator("#event-refresh-status").filter(has_text='Статистика обновлена').wait_for(timeout=15000)
                assert 'Рынки обновлены' in await front.locator('#event-refresh-status').inner_text()
                saved=repo.event_markets('123'); assert saved['markets'][0]['outcomes'][0]['odds']==3.9
                old=repo.statistics('123'); assert old['state']=='ok'
                assert not await front.locator('#event-refresh').is_disabled()
                blocked[0]=True
                for run in range(2):
                    with repo.connection() as db:
                        db.execute('UPDATE event_refresh_jobs SET requested_at=? WHERE event_id=?',((datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat(timespec='microseconds'),'123'))
                    await front.locator('#event-refresh').click()
                    await front.locator("#event-refresh-status").filter(has_text='очереди').wait_for()
                    claim=(await client.post('/api/bookmakers/refresh/claim',json={})).json()['job']
                    count=len(stat_requests)
                    result=await refresh_job(source,client,claim)
                    assert result['market_state']=='ok' and result['statistics_state']=='blocked',result
                    if run: assert len(stat_requests)==count,stat_requests
                    else: assert len(stat_requests)>count,stat_requests
                    await front.locator("#event-refresh-status").filter(has_text='Источник ограничил доступ к статистике').wait_for(timeout=15000)
                    retained=repo.statistics('123')
                    assert retained['sections']==old['sections'] and retained['observed_at']==old['observed_at'] and retained['latest_attempt']['state']=='blocked'
                other=document(PROJECTIONS[0]); other.update(source_event_id='124',url='https://winline.ru/stavki/sport/futbol/rossiya/rpl/124',home='Спартак М',away='Акрон Тольятти',statistics_url='https://winline.ru/stats/match/72334422')
                repo.import_event_markets(other)
                await front.goto(base+'/#match/124')
                await front.locator('#event-refresh').wait_for(state='visible')
                await front.locator('#event-refresh').click()
                await front.locator("#event-refresh-status").filter(has_text='очереди').wait_for()
                claim=(await client.post('/api/bookmakers/refresh/claim',json={})).json()['job']
                count=len(stat_requests)
                result=await refresh_job(source,client,claim)
                assert result['market_state']=='ok' and result['statistics_state']=='blocked' and len(stat_requests)==count,result
                await front.locator("#event-refresh-status").filter(has_text='Источник ограничил доступ к статистике').wait_for(timeout=15000)
                assert not errors,errors
            await source.close()
        finally: await browser.close()
    await runner.cleanup()
    print('PASS: match button → queue → real browser, feed and stats widget → receiver → UI; blocked retries preserve old statistics and obey backoff')


def main():
    with tempfile.TemporaryDirectory(prefix='event-refresh-browser-') as folder:
        repo=Repository(Path(folder)/'matchlab.sqlite3'); repo.initialize()
        match=document(PROJECTIONS[0]); match.update(source_event_id='123',url='https://winline.ru/stavki/sport/futbol/rossiya/rpl/123',home='Спартак М',away='Акрон Тольятти',statistics_url='https://winline.ru/stats/match/72334410')
        repo.import_event_markets(match)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,QWEN_BASE_URL='',QWEN_MODEL='',BOOKMAKER_INGEST_TOKEN=KEY)
        server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            base=f'http://127.0.0.1:{port}'; opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                try: opener.open(base+'/api/bookmakers/events',timeout=1).close(); break
                except OSError: time.sleep(.1)
            else: raise AssertionError('API did not start')
            asyncio.run(exercise(base,repo))
        finally: server.terminate(); server.wait(timeout=20)


if __name__=='__main__': main()
