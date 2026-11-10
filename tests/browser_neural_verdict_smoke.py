"""Real authenticated jobs and verdict UI against a deterministic local API fixture."""
import asyncio
from copy import deepcopy
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
from test_product import event
from verdict_helpers import statistics,reply
from matchlab.storage import Repository


class ModelFixture(BaseHTTPRequestHandler):
    packets=[]
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.path=='/v1/chat/completions'
        assert body['chat_template_kwargs']['enable_thinking'] is False
        packet=json.loads(body['messages'][1]['content']);self.packets.append(packet)
        content=json.dumps(reply(packet),ensure_ascii=False)
        encoded=json.dumps({'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':content}}]}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(encoded)


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    async def deliver():
        while True:
            job=repo.claim_statistics_request()
            if job:repo.finish_statistics_request(job['job_id'],job['lease_id'],'ok')
            await asyncio.sleep(.1)
    delivery=asyncio.create_task(deliver())
    try:
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
            try:
                for theme,width in [('dark',1440),('light',390),('dark',320)]:
                    context=await browser.new_context(viewport={'width':width,'height':1050},color_scheme=theme,locale='ru-RU',reduced_motion='reduce')
                    page=await context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    await page.goto(base+'/#match/123')
                    await page.locator('#age-accept').click();await page.locator('#privacy-reject').click()
                    await signed_in_browser(page,base);await page.reload()
                    await expect(page.locator('#explain-button')).to_have_text('Получить вердикт ИИ')
                    await page.locator('#explain-button').click()
                    await expect(page.locator('#explanation')).to_be_visible(timeout=30000)
                    await expect(page.locator('#explanation .verdict-choice')).to_have_text('Рассмотреть победу команды «Спартак»')
                    await expect(page.locator('#explanation .verdict-preference')).to_have_text('Спартак')
                    assert await page.locator('#explanation .report-facts li').count()==2
                    assert await page.locator('#explanation .verdict-counterargument').is_visible()
                    assert await page.locator('#explanation .verdict-market_context').is_visible()
                    assert await page.locator('#explanation .verdict-reason').is_visible()
                    await expect(page.locator('#explanation .verdict-stake')).to_contain_text('0,25%')
                    assert await page.locator('#explanation .report-chance,#explanation .report-distribution').count()==0
                    assert '%' not in await page.locator('#explanation .report-facts').inner_text()
                    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width)
                    assert await page.locator('#explanation .verdict-preference').evaluate('n=>parseFloat(getComputedStyle(n).fontSize)')>=20
                    if width<1100:assert await page.locator('#explanation .verdict-preference').evaluate('n=>n.getBoundingClientRect().top')>=100
                    await page.screenshot(path=f'/tmp/neural-verdict-{theme}-{width}.png')
                    analysis=await page.evaluate('state.aiAnalysis')
                    assert analysis['analysis_type']=='neural_verdict'
                    assert analysis['context']['neural_input']['quote']['odds']['home']==2.5
                    assert 'probability_assessment' not in analysis['context']
                    await page.goto(base+'/#analysis/'+analysis['id'])
                    await expect(page.locator('#saved-analysis-text .verdict-choice')).to_contain_text('Спартак')
                    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                    # Guarded and deliberate skips have separate labels, and no stake percentage.
                    for engine in ('qwen','model_error','timing_guard'):
                        verdict=deepcopy(analysis['context']['neural_verdict'])
                        verdict.update(decision='skip',selection=None,stake_percent=0,engine=engine)
                        await page.evaluate('v=>renderAnalysis(document.getElementById("saved-analysis-text"),"",null,{reason:"quote_expired"},v)',verdict)
                        await expect(page.locator('#saved-analysis-text .verdict-choice')).to_have_text('Пропустить матч')
                        assert await page.locator('#saved-analysis-text .verdict-stake').count()==0
                        await expect(page.locator('#saved-analysis-text .verdict-preference')).to_have_text('Спартак')
                    unsafe=deepcopy(verdict);unsafe['comparison']['home']['text']='<img src=x onerror=window.verdictInjected=1>'
                    await page.evaluate('v=>renderAnalysis(document.getElementById("saved-analysis-text"),"",null,null,v)',unsafe)
                    assert await page.locator('#saved-analysis-text img').count()==0
                    assert not await page.evaluate('Boolean(window.verdictInjected)')
                    legacy=deepcopy(analysis['context']['neural_verdict'])
                    for key in ('preference','preference_label','comparison','counterargument','market_context','reason','line_odds'):legacy.pop(key,None)
                    legacy['version']='neural-verdict-v2';legacy['arguments']=legacy['arguments'][:2]
                    await page.evaluate('v=>renderAnalysis(document.getElementById("saved-analysis-text"),"",null,null,v)',legacy)
                    await expect(page.locator('#saved-analysis-text .verdict-choice')).to_contain_text('Спартак')
                    assert await page.locator('#saved-analysis-text .verdict-preference').count()==0
                    assert await page.locator('#saved-analysis-text .report-facts li').count()==2
                    assert not errors,errors
                    await context.close()
            finally:await browser.close()
    finally:
        delivery.cancel();await asyncio.gather(delivery,return_exceptions=True)
    assert len(ModelFixture.packets)==3,len(ModelFixture.packets)
    assert all(p['policy']['scope']=='team_winner_90m' for p in ModelFixture.packets)
    print('PASS: authenticated jobs, raw prices, bounded stakes, saved verdicts, skip labels, mobile themes and safe rendering (local model fixture)')


def main():
    model=ThreadingHTTPServer(('127.0.0.1',0),ModelFixture);Thread(target=model.serve_forever,daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='matchlab-verdict-ui-') as folder:
            repo=Repository(Path(folder)/'matchlab.sqlite3');repo.initialize()
            value=event();value['statistics_url']='https://winline.ru/stats/match/72334410'
            repo.import_event_markets(value);repo.import_statistics(statistics())
            with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
            base=f'http://127.0.0.1:{port}'
            env=dict(os.environ,MATCHLAB_DATA_DIR=folder,PUBLIC_ORIGIN=base,EVENT_ANALYSIS_MODE='neural-verdict',
                VERDICT_MAX_STAKE_PCT='0.5',QWEN_BASE_URL=f'http://127.0.0.1:{model.server_port}/v1',QWEN_MODEL='verdict-fixture',
                QWEN_API_KEY='',BOOKMAKER_INGEST_TOKEN='')
            with open('/tmp/neural-verdict-browser-server.log','w') as log:
                server=subprocess.Popen([sys.executable,'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
                try:
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    for _ in range(100):
                        try:opener.open(base+'/api/bookmakers/events',timeout=1).close();break
                        except OSError:time.sleep(.1)
                    else:raise AssertionError('App did not start; see /tmp/neural-verdict-browser-server.log')
                    asyncio.run(exercise(base,repo))
                finally:server.terminate();server.wait(timeout=20)
    finally:model.shutdown();model.server_close()


if __name__=='__main__':main()
