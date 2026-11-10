"""Structured report layout, saved text, plain-text fallback and injection safety."""
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
from browser_market_ui import make_event
from matchlab.storage import Repository
from matchlab.chances import render_report
from reasoning_helpers import EXPLANATION

REPORT='''Победитель: Велес — 42,2% (по линии; Исход 1X2 (осн.время)).

Победные рынки (оценка): 1 тайм обе забьют · Нет — 79,8% по линии; 2 тайм обе забьют · Нет — 74,5% по линии.

Аргументы: Статистика сезона: победы — Велес 8, Камаз 3; Статистика сезона: забитые мячи — Велес 21, Камаз 20; Статистика сезона: голов пропущено — Велес 20, Камаз 28.

Ограничения: Недостаточно подходящей истории для независимого расчёта; проценты рассчитаны по линии. Для части рынков вероятность не рассчитана: условия или модель не поддерживаются. Источник передал неполный набор рынков. Все сохранённые рынки не поместились в контекст нейросети; показан расчётный отчёт.'''


async def exercise(base):
    from playwright.async_api import async_playwright, expect
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for theme,width in [('dark',1440),('light',390),('dark',320)]:
                page=await browser.new_page(viewport={'width':width,'height':1050},color_scheme=theme,locale='ru-RU')
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto(base+'/#match/17006967')
                await page.locator('#age-accept').click();await page.locator('#privacy-reject').click()
                await page.locator('#event-header').wait_for(state='visible')
                for text in [REPORT,REPORT.replace('\n\n','\n'),REPORT.replace('\n',' ')]:
                    await page.evaluate('text=>{renderAnalysis(document.getElementById("explanation"),text);document.getElementById("explanation").hidden=false;}',text)
                    await expect(page.locator('#explanation .report-chance')).to_have_text('42,2%')
                    await expect(page.locator('#explanation .report-badge')).to_have_text('По линии')
                    assert await page.locator('#explanation .report-markets li').count()==2
                    assert await page.locator('#explanation .report-facts li').count()==3
                    assert await page.locator('#explanation .report-probability').all_inner_texts()==['79,8%','74,5%']
                    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width)
                assert await page.locator('.report-chance').evaluate('node=>parseFloat(getComputedStyle(node).fontSize)')>=24
                await page.locator('#explanation').screenshot(path=f'/tmp/report-{theme}-{width}.png')
                assert await page.locator('#explanation .report-limitations p').is_visible()
                await page.locator('#explanation .report-limitations summary').click()
                assert not await page.locator('#explanation .report-limitations p').is_visible()
                await page.evaluate('text=>renderAnalysis(document.getElementById("saved-analysis-text"),text)',REPORT)
                assert await page.locator('#saved-analysis-text .report-section').count()==3
                unsafe=REPORT.replace('Велес — 42,2%','<img src=x onerror=window.reportInjected=1> — 42,2%')
                await page.evaluate('text=>renderAnalysis(document.getElementById("explanation"),text)',unsafe)
                assert await page.locator('#explanation img,#explanation svg').count()==0
                assert not await page.evaluate('Boolean(window.reportInjected)')
                await page.evaluate('text=>renderAnalysis(document.getElementById("explanation"),text)',REPORT.split('\n\nОграничения:')[0])
                assert await page.locator('#explanation .report-limitations').count()==0
                detailed=render_report({'winner':{'participant':'Таиланд (до23)','probability':.4316,'basis':'history','market':'Исход 1X2','period':'осн.время'},
                    'markets':[],'facts':[],'limitations':['Малая выборка: оценка предварительная.']},
                    arguments=['Средние показатели сезона: забито за матч 0.33. '+EXPLANATION*2,
                               'Средние показатели сезона: пропущено за матч 2.5. '+EXPLANATION*2])
                await page.evaluate('text=>renderAnalysis(document.getElementById("explanation"),text)',detailed)
                assert await page.locator('#explanation .report-facts li').count()==2
                assert EXPLANATION*2 in await page.locator('#explanation .report-facts li').first.inner_text()
                await expect(page.locator('#explanation .report-chance')).to_have_text('43,2%')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width,'detailed arguments')
                await page.locator('#explanation').screenshot(path=f'/tmp/report-detailed-{theme}-{width}.png')
                await page.evaluate('()=>renderAnalysis(document.getElementById("explanation"),"# Старый разбор\\n\\n**Вывод:** сохранённый текст.\\n\\n- Первый факт")')
                assert not await page.locator('#explanation').evaluate('node=>node.classList.contains("structured-report")')
                assert await page.locator('#explanation h3').inner_text()=='Старый разбор'
                assert await page.locator('#explanation li').count()==1
                assert not errors,errors
                await page.close()
        finally:await browser.close()
    print('PASS: report hierarchy, probability emphasis, lists, limitations, saved reports, themes, mobile, legacy text and safe HTML handling')


def main():
    with tempfile.TemporaryDirectory(prefix='matchlab-report-') as folder:
        repo=Repository(Path(folder)/'matchlab.sqlite3');repo.initialize()
        repo.import_event_markets(make_event(17006967,'Сандерленд'))
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,PUBLIC_ORIGIN=f'http://127.0.0.1:{port}',QWEN_BASE_URL='',QWEN_MODEL='',BOOKMAKER_INGEST_TOKEN='')
        server=subprocess.Popen([sys.executable,'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            base=f'http://127.0.0.1:{port}';opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                try:opener.open(base+'/api/bookmakers/events',timeout=1).close();break
                except OSError:time.sleep(.1)
            else:raise AssertionError('App did not start')
            asyncio.run(exercise(base))
        finally:server.terminate();server.wait(timeout=20)


if __name__=='__main__':main()
