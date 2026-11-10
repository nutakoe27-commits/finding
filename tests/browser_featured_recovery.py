"""Only the edited carousel, recovery result, no-evidence and live workflows."""
import asyncio
from collections import Counter
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import time
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from account_helpers import signed_in_browser
from matchlab.storage import Repository
from test_product import event
from verdict_helpers import reply,statistics


class Model(BaseHTTPRequestHandler):
    calls=Counter()
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.path=='/v1/chat/completions'
        packet=json.loads(body['messages'][1]['content']);key=packet['event']['source_event_id'];self.calls[key]+=1
        assert key not in ('203','299'),'unsupported event reached the model'
        value=reply(packet)
        if key=='202' or (key=='201' and self.calls[key]<3):value['comparison']['home']['text']+=' Забито 9999 голов.'
        response={'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(value,ensure_ascii=False)}}],
            'usage':{'prompt_tokens':1000,'completion_tokens':100,'total_tokens':1100}}
        data=json.dumps(response).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    async def deliver():
        while True:
            job=repo.claim_statistics_request()
            if job:repo.finish_statistics_request(job['job_id'],job['lease_id'],'ok')
            await asyncio.sleep(.05)
    delivery=asyncio.create_task(deliver())
    try:
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
            try:
                for theme,width in [('light',1440),('dark',390),('light',320)]:
                    Model.calls.clear();context=await browser.new_context(viewport={'width':width,'height':1050},color_scheme=theme,reduced_motion='reduce',locale='ru-RU')
                    page=await context.new_page();page.set_default_timeout(15000);errors=[];posts=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.on('request',lambda r:posts.append(r.url) if r.method=='POST' and r.url.endswith('/statistics/request') else None)
                    await page.goto(base+'/#matches')
                    if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                    if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                    await page.evaluate('document.documentElement.dataset.theme='+repr(theme))
                    await expect(page.locator('#featured-events')).to_be_hidden()
                    await page.locator('.match-card[data-event-id="201"] .match-ai-button').click();await expect(page.locator('#auth-dialog')).to_be_visible()
                    await page.locator('#auth-close').click()
                    await signed_in_browser(page,base);await page.evaluate('Account.refresh()')
                    await page.locator('.match-card[data-event-id="201"] .match-ai-button').click()
                    await expect(page.locator('#explanation .verdict-preference')).to_have_text('Спартак',timeout=30000)
                    first=await page.evaluate('state.aiAnalysis');assert first['context']['ai_generation']['model_calls']==3
                    assert first['context']['ai_generation']['token_usage']['total_tokens']==3300
                    await page.screenshot(path=f'/workspace/outputs/product-ai-result-{theme}-{width}.png',full_page=True)
                    await page.goto(base+'/#match/202');await page.wait_for_function('()=>state.marketDocument?.source_event_id==="202"');await expect(page.locator('#explain-button')).to_be_enabled();await page.locator('#explain-button').click()
                    await expect(page.locator('#explanation .verdict-preference')).to_have_text('Никто',timeout=30000)
                    await expect(page.locator('#explanation .verdict-choice')).to_have_text('Пропустить матч')
                    assert '9999' not in await page.locator('#explanation').inner_text()
                    assert await page.locator('#ai-request-error').is_hidden()
                    await page.goto(base+'/#match/203');await page.wait_for_function('()=>state.marketDocument?.source_event_id==="203"');await expect(page.locator('#explain-button')).to_be_enabled();await page.locator('#explain-button').click()
                    await page.wait_for_function('()=>state.aiAnalysis?.event?.source_event_id==="203"')
                    await expect(page.locator('#explanation .verdict-preference')).to_have_text('Никто',timeout=30000)
                    guarded=await page.evaluate('state.aiAnalysis');assert guarded['context']['ai_generation']['model_calls']==0
                    await page.goto(base+'/#match/299');await page.wait_for_function('()=>state.marketDocument?.source_event_id==="299"');await expect(page.locator('#event-ai-support')).to_be_visible()
                    old_posts=len(posts);await page.locator('#explain-button').click()
                    await page.wait_for_function('()=>state.aiAnalysis?.event?.source_event_id==="299"')
                    await expect(page.locator('#explanation .verdict-preference')).to_have_text('Никто',timeout=15000)
                    live=await page.evaluate('state.aiAnalysis');assert live['context']['ai_generation']['reason']=='live_not_supported'
                    assert len(posts)==old_posts,'live unnecessarily requested statistics'
                    assert Model.calls['201']==3 and Model.calls['202']==3 and not Model.calls['299']
                    await page.goto(base+'/#matches');await expect(page.locator('#featured-events')).to_be_hidden()
                    await page.evaluate('window.scrollTo(0,0)')
                    await page.screenshot(path=f'/workspace/outputs/featured-events-{theme}-{width}.png',full_page=True)
                    await page.locator('[data-phase="live"]').click();await expect(page.locator('.match-card')).to_have_count(1)
                    await expect(page.locator('.match-card')).to_contain_text('LIVE')
                    assert not errors,errors
                    await context.close()
            finally:await browser.close()
    finally:delivery.cancel();await asyncio.gather(delivery,return_exceptions=True)
    print('PASS: legacy catalogue/auth/mobile; bounded 3-attempt regeneration; explicit nobody/skip; live without statistics requests or model calls')


def main():
    Path('/workspace/outputs').mkdir(exist_ok=True)
    model=ThreadingHTTPServer(('127.0.0.1',0),Model);Thread(target=model.serve_forever,daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='featured-recovery-') as folder:
            repo=Repository(Path(folder)/'matchlab.sqlite3');repo.initialize()
            for key in ('201','202','203','204','205','206','299'):
                value=event(key,phase='live' if key=='299' else 'prematch');value['statistics_url']='https://winline.ru/stats/match/72334410'
                repo.import_event_markets(value)
                if key not in ('203','299'):repo.import_statistics(statistics(identifier=key))
            with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
            base=f'http://127.0.0.1:{port}'
            env=dict(os.environ,MATCHLAB_DATA_DIR=folder,PUBLIC_ORIGIN=base,QWEN_BASE_URL=f'http://127.0.0.1:{model.server_port}/v1',QWEN_MODEL='fixture',QWEN_API_KEY='',EVENT_ANALYSIS_MODE='neural-verdict',BOOKMAKER_INGEST_TOKEN='')
            with open('/tmp/featured-recovery-server.log','w') as log:
                server=subprocess.Popen([sys.executable,'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
                try:
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    for _ in range(100):
                        try:opener.open(base+'/api/bookmakers/events',timeout=1).close();break
                        except OSError:time.sleep(.1)
                    else:raise AssertionError('App did not start')
                    asyncio.run(exercise(base,repo))
                finally:server.terminate();server.wait(timeout=20)
    finally:model.shutdown();model.server_close()


if __name__=='__main__':main()
