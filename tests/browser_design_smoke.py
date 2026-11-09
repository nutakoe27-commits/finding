"""Theme, onboarding and match navigation against a local API on real Chromium."""
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


CONTRAST = r'''selector => {
  const element=document.querySelector(selector);
  const rgb=value=>value.match(/[\d.]+/g).map(Number);
  const foreground=rgb(getComputedStyle(element).color);
  let parent=element,background;
  while(parent){ const color=rgb(getComputedStyle(parent).backgroundColor); if(color.length===3 || color[3]>0.99){background=color;break;} parent=parent.parentElement; }
  const luminance=values=>values.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
  const a=luminance(foreground),b=luminance(background);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
}'''


async def exercise(base):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for theme in ('light','dark'):
                for width in (320,390,768,1024,1150,1440):
                    context=await browser.new_context(viewport={'width':width,'height':900},color_scheme=theme,locale='ru-RU',reduced_motion='reduce')
                    page=await context.new_page(); errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    await page.goto(base+'/#matches'); await page.locator('.match-card').first.wait_for()
                    await page.locator('#age-accept').click(); await page.locator('#privacy-reject').click()
                    assert await page.title()=='prognozich ai — спортивная аналитика'
                    assert await page.locator('.brand').get_attribute('aria-label')=='prognozich ai, матчи'
                    assert (await page.locator('.brand').inner_text()).replace('\n','').replace(' ','')=='prognozichai'
                    assert await page.locator('.brand-mark').evaluate('img=>img.complete && img.naturalWidth>0')
                    assert await page.locator('a[href="#calculator"]:visible, a[href="#status"]:visible').count()==0
                    assert 'matchlab' not in (await page.locator('body').inner_text()).lower()
                    assert await page.locator('html').get_attribute('data-theme')==theme
                    assert await page.locator('#quick-guide').is_visible()
                    assert await page.locator('#guide-toggle').get_attribute('aria-expanded')=='true'
                    assert await page.locator('#theme-toggle').get_attribute('aria-pressed')==str(theme=='dark').lower()
                    assert not await page.evaluate('document.documentElement.scrollWidth>window.innerWidth'),(theme,width,'catalog overflow')
                    for selector in ('body','.nav-link.active','h1','.guide-steps strong','.match-card-title','.match-ai-button','.odds-tile strong'):
                        ratio=await page.evaluate(CONTRAST,selector)
                        assert ratio>=4.5,(theme,width,selector,ratio)
                    if width>650:
                        rail=await page.locator('.sidebar').bounding_box(); toggle=await page.locator('#theme-toggle').bounding_box()
                        assert toggle['x']+toggle['width']<=rail['x']+rail['width'],(width,rail,toggle)
                    await page.locator('.match-card-title').first.click()
                    await page.locator('#event-header').wait_for(state='visible')
                    assert await page.locator('#ai-desk').is_visible()
                    assert 'личном аккаунте' in await page.locator('.ai-visibility').inner_text()
                    if await page.locator('#tab-analysis').is_visible(): await page.locator('#tab-analysis').click()
                    await page.locator('#event-show-markets').click()
                    assert await page.locator('#tab-markets').get_attribute('aria-selected')=='true'
                    assert await page.locator('#market-detail').is_visible()
                    assert not await page.evaluate('document.documentElement.scrollWidth>window.innerWidth'),(theme,width,'detail overflow')
                    await page.goto(base+'/#status'); await page.locator('#view-status').wait_for(state='visible')
                    assert not await page.evaluate('document.documentElement.scrollWidth>window.innerWidth'),(theme,width,'status overflow')
                    assert not errors,(theme,width,errors)
                    await context.close()

            context=await browser.new_context(viewport={'width':390,'height':900},color_scheme='light')
            page=await context.new_page(); await page.goto(base+'/#matches'); await page.locator('.match-card').first.wait_for()
            await page.locator('#age-accept').click(); await page.locator('#privacy-reject').click()
            await page.locator('#guide-close').click()
            assert await page.locator('#quick-guide').is_hidden()
            assert await page.locator('#guide-toggle').get_attribute('aria-expanded')=='false'
            await page.locator('#theme-toggle').click()
            assert await page.locator('html').get_attribute('data-theme')=='dark'
            assert await page.evaluate("localStorage.getItem('matchlab:theme:v1')")=='dark'
            await page.reload(); await page.locator('.match-card').first.wait_for()
            assert await page.locator('html').get_attribute('data-theme')=='dark' and await page.locator('#quick-guide').is_hidden()
            await page.emulate_media(color_scheme='light')
            assert await page.locator('html').get_attribute('data-theme')=='dark'
            await page.locator('#guide-toggle').click(); assert await page.locator('#quick-guide').is_visible()
            await page.locator('#match-search').fill('нет такой команды')
            await page.locator('#market-list-empty').wait_for(state='visible')
            assert await page.locator('#clear-active-filters').is_visible()
            await page.locator('#clear-active-filters').click(); await page.locator('.match-card').first.wait_for()
            assert await page.locator('#match-search').input_value()==''
            second=await context.new_page(); await second.goto(base+'/#matches'); await second.locator('.match-card').first.wait_for()
            await second.locator('#theme-toggle').click()
            await page.wait_for_function("() => document.documentElement.dataset.theme==='light'")
            assert await page.locator('#theme-toggle').get_attribute('aria-pressed')=='false'
            await context.close()

            context=await browser.new_context(viewport={'width':390,'height':900},color_scheme='dark')
            page=await context.new_page(); await page.goto(base+'/#matches'); await page.locator('.match-card').first.wait_for()
            await page.locator('#age-accept').click(); await page.locator('#privacy-reject').click()
            await page.emulate_media(color_scheme='light')
            await page.wait_for_function("() => document.documentElement.dataset.theme==='light'")
            await context.close()

            context=await browser.new_context(viewport={'width':390,'height':900},color_scheme='light')
            await context.add_init_script("Storage.prototype.getItem=()=>{throw Error('storage blocked')}; Storage.prototype.setItem=()=>{throw Error('storage blocked')}")
            page=await context.new_page(); errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
            await page.goto(base+'/#matches'); await page.locator('.match-card').first.wait_for()
            await page.locator('#age-accept').click(); await page.locator('#privacy-reject').click()
            assert await page.locator('html').get_attribute('data-theme')=='light'
            await page.locator('#theme-toggle').click(); assert await page.locator('html').get_attribute('data-theme')=='dark'
            await page.locator('#guide-close').click(); assert await page.locator('#quick-guide').is_hidden()
            assert not errors,errors
            await context.close()
        finally: await browser.close()
    print('PASS: system and saved themes, contrast, cross-tab updates, onboarding, reset filters and responsive views')


def main():
    with tempfile.TemporaryDirectory(prefix='matchlab-design-') as folder:
        repo=Repository(Path(folder)/'matchlab.sqlite3'); repo.initialize()
        repo.import_event_markets(make_event(17006967,'Сандерленд'))
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,QWEN_BASE_URL='',QWEN_MODEL='',BOOKMAKER_INGEST_TOKEN='')
        server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            base=f'http://127.0.0.1:{port}'; opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                try: opener.open(base+'/api/bookmakers/events',timeout=1).close(); break
                except OSError: time.sleep(.1)
            else: raise AssertionError('API did not start')
            asyncio.run(exercise(base))
        finally: server.terminate(); server.wait(timeout=20)


if __name__=='__main__': main()
