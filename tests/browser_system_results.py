"""Local-only regressions for navigation outside the homepage and system results."""
import json
import shutil
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import browser_verdict_outcomes as fixture
from outcome_helpers import forecast
from matchlab.winline_markets import ensure_event_page,EventNavigationError


async def navigation(browser):
    context=await browser.new_context()
    canonical='https://winline.ru/stavki/sport/futbol/17089331'
    alias='https://winline.ru/stavki/event/17089331'
    html='''<a href="/stavki">Линия</a><main></main><script>
      addEventListener('popstate',()=>{
        if(location.pathname==='/stavki/sport/futbol/17089331')
          document.querySelector('main').innerHTML='<ww-feature-marketbook-dsk><button class="market-header">Исход</button></ww-feature-marketbook-dsk>';
      });</script>'''
    await context.route('https://winline.ru/**',lambda route:route.fulfill(status=200,content_type='text/html',body=html))
    try:
        page=await context.new_page()
        event={'source_event_id':'17089331','sport':'football'}
        for start in ('https://winline.ru/stavki',alias):
            await page.goto(start)
            await ensure_event_page(page,alias,event)
            assert page.url==canonical
            assert await page.locator('ww-feature-marketbook-dsk button.market-header').is_visible()
        # Keep the source's own delayed route resolution when no catalogue
        # metadata is available, rather than guessing a sport.
        await page.goto(alias)
        await page.evaluate("url=>setTimeout(()=>{history.replaceState({},'',url);dispatchEvent(new PopStateEvent('popstate'));},100)",canonical)
        await ensure_event_page(page,alias)
        assert page.url==canonical
        await page.goto('https://winline.ru/stavki/sport/xokkej/17089331')
        try:await ensure_event_page(page,alias,event)
        except EventNavigationError as error:assert error.code=='sport_identity_mismatch'
        else:raise AssertionError('Another sport was accepted')
        await page.goto('https://winline.ru/stavki')
        await page.set_content('<a href="/stavki">Линия</a><p>Verify you are human</p>')
        try:await ensure_event_page(page,alias,event)
        except EventNavigationError as error:assert error.code=='access_check'
        else:raise AssertionError('An access check was bypassed')
        assert page.url=='https://winline.ru/stavki'
    finally:await context.close()


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    now=datetime.now(timezone.utc)
    human=forecast(repo,uuid4().hex,kickoff=now+timedelta(days=3),at=now-timedelta(seconds=30),score=None)
    system=forecast(repo,uuid4().hex,event=human['event'],at=now-timedelta(seconds=20),score=None)
    no_data=forecast(repo,uuid4().hex,kickoff=now+timedelta(days=4),at=now-timedelta(seconds=20),score=None,decision='skip',preference=None,engine='data_guard')
    with repo.connection() as db:
        for item in (system,no_data):
            item['context']['analysis_origin']='system'
            db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(item),item['id']))
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            await navigation(browser)
            for width in (1440,390):
                context=await browser.new_context(viewport={'width':width,'height':1000},locale='ru-RU',reduced_motion='reduce')
                page=await context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                response=await context.request.post(base+'/api/auth/login',data={'username':'outcomes_admin','password':fixture.PASSWORD})
                assert response.status==200
                await page.goto(base+'/#admin')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await page.locator('[data-admin-tab="results"]').click()
                await expect(page.locator('#ar-matches tbody tr')).to_have_count(8)
                await page.locator('#ar-origin').select_option('system')
                await expect(page.locator('#ar-matches tbody tr')).to_have_count(1)
                await expect(page.locator('#ar-matches')).to_contain_text('Системный анализ')
                await expect(page.locator('#ar-matches')).to_contain_text('Ожидаем начало матча')
                await expect(page.locator('#ar-system-activity')).to_contain_text('сохранено 2; ИИ ответил 1; без подтверждённого прогноза 1')
                await expect(page.locator('#ar-metrics .ar-metric strong').first).to_have_text('—')
                data=await (await context.request.get(base+'/api/admin/verdict-outcomes?origin=system')).json()
                assert data['metrics']['settled']==0 and data['awaiting_start']==1
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
                await page.screenshot(path=f'/workspace/outputs/system-results-{width}.png',full_page=True)
                await page.locator('#ar-origin').select_option('human')
                await expect(page.locator('#ar-matches tbody tr')).to_have_count(7)
                await expect(page.locator('#ar-system-activity')).to_be_hidden()
                await page.evaluate("Account.api('/api/auth/logout',{method:'POST',body:'{}'}).then(()=>Account.refresh())")
                await expect(page.locator('#ar-matches')).to_be_empty()
                await expect(page.locator('#ar-system-activity')).to_be_empty()
                assert not errors,errors
                await context.close()
        finally:await browser.close()
    print('PASS: canonical navigation outside homepage, identity and access checks; system/human filters, future pending results, no-data activity, 1440/390px, logout cleanup')


if __name__=='__main__':
    fixture.exercise=exercise
    fixture.main()
