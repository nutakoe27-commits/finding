"""Only the new model admin view: auth, charts, mobile layout and logout cleanup."""
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from matchlab import accounts
from matchlab.storage import Repository
from test_participant_model_admin import fixture_report

PASSWORD='model-browser-fixture-long-password'

async def exercise(base):
    from playwright.async_api import async_playwright, expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for theme,width in [('light',1440),('dark',1440),('light',390),('dark',390),('light',320)]:
                context=await browser.new_context(viewport={'width':width,'height':1000},locale='ru-RU',color_scheme=theme,reduced_motion='reduce')
                page=await context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                response=await context.request.post(base+'/api/auth/login',data={'username':'model_admin','password':PASSWORD})
                assert response.status==200,await response.text()
                await page.goto(base+'/#admin');await page.evaluate('document.documentElement.dataset.theme='+repr(theme))
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await page.locator('[data-admin-tab="model"]').click()
                await expect(page.locator('#am-metrics .ar-metric')).to_have_count(4)
                await expect(page.locator('#am-matches tbody tr')).to_have_count(1)
                await expect(page.locator('#am-chart .am-bin')).to_have_count(2)
                await expect(page.locator('#am-variants .ar-metric')).to_have_count(4)
                await expect(page.locator('#am-matches')).to_contain_text('Ставим')
                await expect(page.locator('#am-version')).to_contain_text('Без коэффициентов в модели.')
                await expect(page.locator('#am-matches')).to_contain_text('70%')
                await expect(page.locator('#am-matches')).to_contain_text('20%')
                await expect(page.locator('#am-matches')).to_contain_text('П1 1,80 · Х 3,50 · П2 4,00')
                await expect(page.locator('#am-version')).to_contain_text('1\u00a0030')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width,'overflow')
                await page.evaluate('window.scrollTo(0,0)')
                await page.screenshot(path=f'/workspace/outputs/admin-model-{theme}-{width}.png',full_page=True)
                await page.locator('[data-admin-tab="overview"]').click()
                await page.locator('[data-admin-tab="model"]').click()
                await expect(page.locator('#am-matches tbody tr')).to_have_count(1)
                await page.evaluate("Account.api('/api/auth/logout',{method:'POST',body:'{}'}).then(()=>Account.refresh())")
                await expect(page.locator('#am-matches')).to_be_empty()
                assert (await context.request.get(base+'/api/admin/participant-model')).status==401
                assert not errors,errors
                await context.close()
        finally:await browser.close()
    print('PASS: model charts, exact unnormalised probabilities, 320/390/1440px light/dark, tab reload and logout cleanup')


def main():
    Path('/workspace/outputs').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='participant-model-browser-') as folder:
        root=Path(folder);repo=Repository(root/'matchlab.sqlite3');repo.initialize()
        uid=accounts.create_user(repo,'model_admin',PASSWORD,'admin')
        with repo.connection() as db:accounts.save_agreements(db,uid)
        report=root/'model-report.json';data=fixture_report()
        data.update(feature_profile='statistics_only.v1',decision_policy={'min_probability':.55,'min_expected_return':.05},unsupported_sports=['tennis'],
            variant_metrics={'statistics':{'selected':2,'evaluated':1,'hits':1,'accuracy':1},'statistics_price':{'bets':1,'settled':1,'won':1,'lost':0,'skipped':1,'pending':0,'profit_units':.8,'roi':.8,'accuracy':1,'coverage':.5}})
        data['matches'][0]['variants']={'statistics':{'side':'home','probability':.7},'statistics_price':{'side':'home','decision':'bet','reason':'paper_rule_passed'}}
        report.write_text(json.dumps(data))
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,DATABASE_URL='',PUBLIC_ORIGIN=base,QWEN_BASE_URL='',QWEN_MODEL='',BOOKMAKER_INGEST_TOKEN='',PARTICIPANT_MODEL_REPORT=str(report))
        with open('/tmp/participant-model-browser.log','w') as log:
            server=subprocess.Popen([sys.executable,'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:opener.open(base+'/api/auth/me',timeout=1).close();break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('Server did not start')
                asyncio.run(exercise(base))
            finally:server.terminate();server.wait(timeout=20)

if __name__=='__main__':main()
