"""Admin outcome charts against real saved decisions and a local archive fixture."""
import asyncio
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
from matchlab import accounts
from matchlab.storage import Repository
from outcome_helpers import seed_outcomes

PASSWORD='admin-outcomes-browser-fixture-123'


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for theme,width in [('light',1440),('dark',1440),('light',390),('dark',390),('light',320)]:
                context=await browser.new_context(viewport={'width':width,'height':1050},color_scheme=theme,locale='ru-RU',reduced_motion='reduce')
                page=await context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                response=await context.request.post(base+'/api/auth/login',data={'username':'outcomes_admin','password':PASSWORD})
                assert response.status==200,await response.text()
                await page.goto(base+'/#admin')
                await page.evaluate('document.documentElement.dataset.theme='+repr(theme))
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await page.locator('[data-admin-tab="results"]').click()
                await expect(page.locator('#ar-metrics .ar-metric')).to_have_count(4)
                await expect(page.locator('#ar-metrics .ar-metric strong').first).to_have_text('50%')
                await expect(page.locator('#ar-matches tbody tr')).to_have_count(6)
                await expect(page.locator('#ar-matches')).to_contain_text('Не зашло — отказ оправдан')
                await expect(page.locator('#ar-matches')).to_contain_text('Пропущена победа')
                await expect(page.locator('#ar-message')).to_contain_text('автоматическое обновление')
                assert await page.locator('#ar-accuracy-chart svg').count()==1
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width,'page overflow')
                svg=page.locator('#ar-accuracy-chart svg');await svg.focus()
                before=await page.locator('#ar-accuracy-chart .ar-chart-readout').inner_text();await page.keyboard.press('ArrowLeft')
                assert before!=await page.locator('#ar-accuracy-chart .ar-chart-readout').inner_text()
                await page.locator('#ar-state').select_option('bet')
                await expect(page.locator('#ar-matches tbody tr')).to_have_count(2)
                await expect(page.locator('#ar-metrics .ar-metric strong').first).to_have_text('50%')
                await page.locator('#ar-state').select_option('all')
                await expect(page.locator('#ar-matches tbody tr')).to_have_count(6)
                await page.locator('#ar-matches .ar-row-details summary').first.click()
                await expect(page.locator('#ar-matches .ar-row-details').first).to_contain_text('Причина решения по данным')
                await page.locator('#ar-days').focus()
                await page.evaluate('window.scrollTo(0,0)')
                await page.screenshot(path=f'/workspace/outputs/admin-results-{theme}-{width}.png',full_page=True)
                await page.locator('[data-admin-tab="tokens"]').click()
                await expect(page.locator('#tk-metrics .ar-metric')).to_have_count(4)
                await expect(page.locator('#tk-metrics .ar-metric strong').first).to_have_text('10\u00a0500')
                await expect(page.locator('#tk-requests tbody tr')).to_have_count(3)
                await expect(page.locator('#tk-coverage')).to_be_visible()
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width,'tokens overflow')
                await page.locator('#tk-days').focus();await page.evaluate('window.scrollTo(0,0)')
                await page.screenshot(path=f'/workspace/outputs/admin-tokens-{theme}-{width}.png',full_page=True)
                await page.locator('[data-admin-tab="results"]').click()
                await expect(page.locator('#ar-metrics .ar-metric')).to_have_count(4)
                await page.locator('#ar-days').select_option('7')
                await expect(page.locator('#ar-message')).to_contain_text('автоматическое обновление')
                await page.locator('#ar-source').select_option('trials')
                await expect(page.locator('#ar-diagnostic')).to_be_visible()
                await expect(page.locator('#ar-message')).to_contain_text('Архивный тест')
                await page.locator('#ar-source').select_option('site')
                await expect(page.locator('#ar-diagnostic')).to_be_hidden()
                await expect(page.locator('#ar-matches tbody tr')).to_have_count(6)
                await page.evaluate("Account.api('/api/auth/logout',{method:'POST',body:'{}'}).then(()=>Account.refresh())")
                await expect(page.locator('#ar-metrics')).to_be_empty()
                assert (await context.request.get(base+'/api/admin/verdict-outcomes')).status==401
                assert not errors,errors
                await context.close()
            reader=await browser.new_context()
            await reader.request.post(base+'/api/auth/login',data={'username':'outcomes_reader','password':PASSWORD})
            assert (await reader.request.get(base+'/api/admin/verdict-outcomes')).status==403
            await reader.close()
        finally:await browser.close()
    print('PASS: admin-only charts, real archive settlement, source separation, filters, keyboard chart inspection, 320/390/1440px themes and logout cleanup')


def main():
    Path('/workspace/outputs').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='matchlab-outcomes-') as folder:
        repo=Repository(Path(folder)/'matchlab.sqlite3');repo.initialize();seed_outcomes(repo)
        for name,role in [('outcomes_admin','admin'),('outcomes_reader','user')]:
            uid=accounts.create_user(repo,name,PASSWORD,role)
            with repo.connection() as db:accounts.save_agreements(db,uid)
        from datetime import datetime,timezone
        from matchlab.token_usage import capture_usage,begin_call
        import json
        with capture_usage() as captured:
            call=begin_call(1024);call.finish({'prompt_tokens':10000,'completion_tokens':500},'stop')
        with repo.connection() as db:
            uid=db.execute("SELECT id FROM users WHERE username='outcomes_admin'").fetchone()[0]
            for key,details in [('tokens',{'model_calls':1,'token_usage':captured.summary()}),('old',None),('cache',{'cache_hit':True,'model_calls':0})]:
                db.execute('INSERT INTO ai_usage(id,user_id,at,kind,target,status,details) VALUES (?,?,?,?,?,?,?)',
                    (key,uid,datetime.now(timezone.utc).isoformat(),'event','/api/bookmakers/events/123/explain','success',json.dumps(details) if details else None))
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,PUBLIC_ORIGIN=base,QWEN_BASE_URL='',QWEN_MODEL='',BOOKMAKER_INGEST_TOKEN='')
        with open('/tmp/verdict-outcomes-browser-server.log','w') as log:
            server=subprocess.Popen([sys.executable,'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:opener.open(base+'/api/auth/me',timeout=1).close();break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('App did not start; see /tmp/verdict-outcomes-browser-server.log')
                asyncio.run(exercise(base,repo))
            finally:server.terminate();server.wait(timeout=20)


if __name__=='__main__':main()
