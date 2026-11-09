"""Product acceptance: real API/SQLite and a local AI HTTP fixture, no external calls."""
import asyncio
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from account_helpers import signed_in_browser
from matchlab.markets import FORMAT
from matchlab.storage import Repository
from matchlab.winline_markets import primary_quote
from sports_fixtures import PROJECTIONS, document as sport_document

REQUESTS = []
GOAL_VARIANTS=json.loads((ROOT/'tests/fixtures/winline_goal_market_variants.json').read_text())


class AIHandler(BaseHTTPRequestHandler):
    def log_message(self, *unused):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        context = json.loads(body['messages'][1]['content'])
        REQUESTS.append(context)
        time.sleep(0.3)
        text = 'Qwen: **Вывод:** разбор выбранного рынка.\n\n* **Рынок:** это оценка рынка, а не независимая вероятность. <img src=x onerror="window.analysisInjected=1">'
        encoded = json.dumps({'choices':[{'message':{'content':text},'finish_reason':'stop'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


async def run(url):
    from playwright.async_api import async_playwright, expect
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=shutil.which('chromium'))
        try:
            for width, height in ((1440, 1000), (390, 844)):
                page = await browser.new_page(viewport={'width':width,'height':height}, locale='ru-RU')
                await signed_in_browser(page,url)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(url)
                await page.locator('#age-accept').click(); await page.locator('#privacy-reject').click()
                await page.locator('.match-card').first.wait_for()
                await page.locator('#market-refresh').click()
                await page.locator('#toast').filter(has_text='Список обновлён из сохранённых данных').wait_for()
                async def reject_refresh(route):
                    await route.fulfill(status=503,json={'detail':'Приёмник временно недоступен'})
                await page.route('**/api/bookmakers/events?*',reject_refresh)
                await page.locator('#market-refresh').click()
                await page.locator('#market-list-error').filter(has_text='Приёмник временно недоступен').wait_for()
                assert not await page.locator('#toast').is_visible()
                await page.unroute('**/api/bookmakers/events?*',reject_refresh)
                async def proxy_failure(route):
                    await route.fulfill(status=502,content_type='text/html',body='<h1>Bad Gateway</h1>')
                await page.route('**/api/bookmakers/events?*',proxy_failure)
                await page.locator('#market-refresh').click()
                await page.locator('#market-list-error').filter(has_text='HTTP 502').wait_for()
                assert not await page.locator('#toast').is_visible()
                await page.unroute('**/api/bookmakers/events?*',proxy_failure)
                await page.locator('#market-refresh').click()
                await page.locator('#toast').filter(has_text='Список обновлён').wait_for()
                assert not await page.locator('#view-status').is_visible()
                assert not await page.locator('#manual-selection').is_visible()
                assert not await page.locator('#market-next').is_disabled()
                # Search must find an event outside the first page, including
                # Cyrillic case folding; server filtering precedes pagination.
                await page.locator('#match-search').fill('УНИКАЛЬНАЯ')
                await page.locator('.match-card[data-event-id="17007032"]').wait_for()
                assert await page.locator('.match-card').count() == 1
                await page.locator('#match-search').fill('Сандерленд')
                card = page.locator('.match-card[data-event-id="17006967"]')
                await card.wait_for()
                await card.locator('.favorite-button').click()
                await page.reload()
                await page.locator('.nav-link[href="#favorites"]').click()
                await page.locator('.match-card[data-event-id="17006967"]').wait_for()
                await expect(page.locator(".match-card")).to_have_count(1)
                # A source price selects the exact outcome for the AI desk.
                await page.locator('.match-card .odds-tile').first.click()
                await page.locator('#ai-outcome-label').wait_for(state='visible')
                assert 'П1' in await page.locator('#ai-outcome-label').inner_text()
                assert '3.9' in await page.locator('#ai-outcome-label').inner_text()
                assert await page.locator('#market-detail-title').inner_text() == 'Сандерленд — Челси'
                await page.locator('#event-refresh').click()
                await page.locator('#event-refresh-status').filter(has_text='Сборщик ещё не настроен').wait_for()
                assert not await page.locator('#event-refresh').is_disabled()
                await page.locator('#market-groups summary').first.click()
                outcomes = page.locator('.market-outcomes').first
                assert await outcomes.inner_text() == 'П1\n3.9\nX\n3.75\nП2\n1.89'
                await page.locator('#market-search').fill('Несуществующий рынок')
                assert await page.locator('#market-groups details').count() == 0
                await page.locator('#market-search').fill('1X2')
                assert await page.locator('#market-groups details').count() == 1
                await page.locator('#explain-button').click()
                await page.locator('#explanation').wait_for(state='visible')
                text = await page.locator('#explanation').inner_text()
                assert 'оценка рынка' in text and 'qwen' not in text.lower()
                assert '**' not in text and await page.locator('#explanation strong').count() == 2
                assert await page.locator('#explanation li').count() == 1
                assert not await page.evaluate('Boolean(window.analysisInjected || window.marketInjected)')
                assert REQUESTS[-1]['selection']['outcome_key']
                assert REQUESTS[-1]['snapshot']['observed_at']
                assert not await page.evaluate('document.documentElement.scrollWidth > window.innerWidth'), f'Overflow at {width}'
                await page.screenshot(path=f'/tmp/matchlab-ui-match-{width}.png')
                # An answer delayed in transit must not appear under a new
                # match after the user has navigated away.
                captured, release = asyncio.Event(), asyncio.Event()
                async def delayed_answer(route):
                    response = await route.fetch()
                    captured.set()
                    await release.wait()
                    await route.fulfill(response=response)
                await page.route('**/api/bookmakers/events/17006967/explain', delayed_answer)
                await page.locator('#explain-button').click()
                await asyncio.wait_for(captured.wait(), timeout=10)
                await page.evaluate("location.hash = '#match/17007000'")
                await expect(page.locator("#market-detail-title")).to_have_text("Команда 0 — Челси")
                release.set()
                await expect(page.locator("#explain-button")).to_be_enabled()
                assert not await page.locator('#explanation').is_visible(), 'Old analysis appeared for another match'
                await page.unroute('**/api/bookmakers/events/17006967/explain', delayed_answer)
                variant_groups=page.locator('.market-group').filter(has_text=GOAL_VARIANTS['market'])
                assert await variant_groups.count()==2
                assert '4-5' in await variant_groups.nth(0).inner_text() and '4-6' in await variant_groups.nth(1).inner_text()
                await variant_groups.nth(1).locator('summary').click()
                await variant_groups.nth(1).locator('.market-outcome').filter(has_text='0-1').click()
                assert (await page.locator('#ai-outcome-label').inner_text())=='0-1 · 3.7'
                assert '4-6' in await page.locator('#ai-market-label').inner_text()
                await page.locator('#ai-notes-details summary').click()
                await page.locator('#notes').fill(f'Проверка выбранного варианта {width}')
                await page.locator('#ai-notes-details summary').click()
                await page.locator('#explain-button').click()
                await page.locator('#explanation').wait_for(state='visible')
                selected=next(item for item in REQUESTS[-1]['markets'] if item['key']==REQUESTS[-1]['selection']['market_key'])
                assert selected['variant_id']=='b'*32,(selected,REQUESTS[-1]['selection'])
                await page.locator('.nav-link[href="#history"]').click()
                await page.locator('.history-card').first.wait_for()
                await page.locator('#history-refresh').click()
                await page.locator('#toast').filter(has_text='История обновлена').wait_for()
                assert '**' not in await page.locator('.history-card').first.locator('.history-excerpt').inner_text()
                await page.evaluate("location.hash = '#status'")
                await page.locator('#view-status').wait_for()
                await page.locator('#status-refresh').click()
                await page.locator('#toast').filter(has_text='Состояние сервиса обновлено').wait_for()
                await page.locator('.nav-link[href="#history"]').click()
                await page.locator('.history-card').first.wait_for()
                await page.locator('.history-card').filter(has_text='Сандерленд').first.locator('a').click()
                await page.locator('#saved-analysis').wait_for(state='visible')
                assert 'оценка рынка' in await page.locator('#saved-analysis-text').inner_text()
                assert '**' not in await page.locator('#saved-analysis-text').inner_text()
                assert await page.locator('#saved-analysis-text strong').count() == 2
                assert 'Qwen' not in await page.locator('body').inner_text()
                await page.locator('#saved-analysis-open').click()
                await page.locator('#market-detail-title').wait_for(state='visible')
                # Keyboard tabs and back navigation remain usable.
                await page.locator('#tab-markets').focus()
                await page.keyboard.press('ArrowRight')
                assert await page.locator('#tab-analysis').get_attribute('aria-selected') == 'true' if await page.locator('#tab-analysis').is_visible() else await page.locator('#tab-markets').get_attribute('aria-selected') == 'true'
                await page.locator('#match-back').click()
                await page.locator('.match-card').first.wait_for()
                await page.screenshot(path=f'/tmp/matchlab-ui-line-{width}.png')
                # Sports share the flow while preserving their own outcomes.
                await page.locator('.nav-link[href="#matches"]').click()
                await page.locator('#sport-filter').select_option('tennis')
                tennis=page.locator('.match-card[data-event-id="91003"]')
                await tennis.wait_for()
                assert await tennis.locator('.odds-tile').count()==2
                assert await tennis.locator('.odds-tile span').all_text_contents()==['П1','П2']
                await tennis.locator('.odds-tile').last.click()
                await page.locator('#ai-outcome-label').wait_for(state='visible')
                assert 'П2' in await page.locator('#ai-outcome-label').inner_text()
                assert not await page.locator('#tab-analysis').is_visible()
                await page.locator('#ai-notes-details summary').click()
                await page.locator('#notes').fill(f'Проверка тенниса {width}')
                await page.locator('#explain-button').click()
                await page.locator('#explanation').wait_for(state='visible')
                assert REQUESTS[-1]['event']['sport']=='tennis'
                assert REQUESTS[-1]['statistical_model'] is None
                assert not await page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                await page.screenshot(path=f'/tmp/matchlab-sports-tennis-{width}.png')
                await page.locator('.nav-link[href="#matches"]').click()
                await page.locator('#sport-filter').select_option('hockey')
                hockey=page.locator('.match-card[data-event-id="91002"]')
                await hockey.wait_for()
                assert await hockey.locator('.odds-tile span').all_text_contents()==['П1','X','П2']
                assert 'осн.время' in await hockey.locator('.headline-caption').inner_text()
                await page.locator('#sport-filter').select_option('motorsport')
                race=page.locator('.match-card[data-event-id="91004"]')
                await race.wait_for()
                assert '4 исходов' in await race.locator('.headline-caption').inner_text()
                await race.locator('.match-card-title').click()
                await expect(page.locator("#market-detail-title")).to_have_text("Тестовая гонка")
                await page.locator('#market-groups summary').first.click()
                await page.locator('.market-outcome').last.click()
                assert 'Пилот D' in await page.locator('#ai-outcome-label').inner_text()
                assert not await page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                assert not errors, errors
                await page.close()
        finally:
            await browser.close()
    print('PASS: real catalogue search/pagination, favorites across reload, desktop/mobile line, selected odds → AI HTTP request → saved history, safe text, generic model naming, keyboard tabs and no overflow')


def make_event(event_id, home, *, competition='Англия. Премьер-лига', phase='prematch', start=1):
    now = datetime.now(timezone.utc)
    return {'format':FORMAT,'source':'winline','sport':'football','source_event_id':str(event_id),
        'url':f'https://winline.ru/stavki/sport/futbol/angliya/apl/{event_id}',
        'home':home,'away':'Челси','competition':competition,'phase':phase,
        'observed_at':now.isoformat(),'kickoff_at':(now+timedelta(days=start)).isoformat(),
        'parser_version':'isolated-ui-fixture','markets':[
            {'name':'Исход 1X2','period':'осн.время','group':'Исход','outcomes':[{'name':'П1','odds':3.9},{'name':'X','odds':3.75},{'name':'П2','odds':1.89}]},
            {'name':'Тотал <img src=x onerror="window.marketInjected=1">','period':'осн.время','group':'Тотал','outcomes':[{'name':'б 2.5','odds':1.78},{'name':'м 2.5','odds':2.02}]}],
        'coverage':{'expected_outcomes':5,'all_tab_selected':True,'stable_passes':2,'hidden_groups':0,'scroll_exhausted':True}}


def main():
    ai = ThreadingHTTPServer(('127.0.0.1',0),AIHandler)
    Thread(target=ai.serve_forever,daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='matchlab-market-ui-') as folder:
            directory = Path(folder)
            if (ROOT / '.local/matchlab.sqlite3').exists():
                shutil.copy2(ROOT / '.local/matchlab.sqlite3',directory / 'matchlab.sqlite3')
            repo = Repository(directory / 'matchlab.sqlite3'); repo.initialize()
            first=make_event(17006967,'Сандерленд');first['statistics_url']='https://winline.ru/stats/match/72334410'
            repo.import_event_markets(first)
            document = repo.event_markets('17006967')
            repo.import_bookmakers(primary_quote(document))
            for index in range(33):
                event=make_event(17007000+index,'Уникальная Команда' if index == 32 else f'Команда {index}',competition='Россия. Премьер-лига',start=2)
                if index==0:
                    event['statistics_url']='https://winline.ru/stats/match/72334411'
                    for variant_id,variant in zip(('a'*32,'b'*32),GOAL_VARIANTS['variants']):
                        event['markets'].append({'name':GOAL_VARIANTS['market'],'group':'Тотал','period':'осн.время','variant_id':variant_id,
                            'outcomes':[{'name':name,'odds':price} for name,price in variant]})
                    event['coverage']['expected_outcomes']+=8
                repo.import_event_markets(event)
            repo.import_event_markets(make_event(18000000,'Наполи',competition='Италия. Серия А',phase='live',start=-1))
            for projection in PROJECTIONS[1:]:
                item=sport_document(projection)
                if item['source_event_id']=='91003': item['statistics_url']='https://winline.ru/stats/match/72334412'
                repo.import_event_markets(item)
            # The model is deliberately skipped without verified context. Seed
            # linked source snapshots so this test exercises the model path.
            for event_id in ('17006967','17007000','91003'):
                item=repo.event_markets(event_id);stamp=datetime.now(timezone.utc).isoformat()
                stats_id=item['statistics_url'].rsplit('/',1)[-1]
                repo.import_statistics({'format':'matchlab.event-statistics.v1','source_event_id':event_id,
                    'event_url':item['url'],'statistics_url':item['statistics_url'],'provider_url':'https://statshub.yunai.tech/winline/ru/match/'+stats_id,
                    'state':'ok','attempted_at':stamp,'observed_at':stamp,'header':item['home']+' — '+item['away'],
                    'match_phase':'prematch','sections':[{'title':'Последние матчи','text':'Подтверждённые данные тестового матча.'}]})
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0)); port = probe.getsockname()[1]
            env = dict(os.environ,MATCHLAB_DATA_DIR=folder,PUBLIC_ORIGIN=f'http://127.0.0.1:{port}',QWEN_BASE_URL=f'http://127.0.0.1:{ai.server_port}/v1',QWEN_MODEL='local-test-model',QWEN_API_KEY='',BOOKMAKER_INGEST_TOKEN='')
            server = subprocess.Popen([str(ROOT / '.venv/bin/python'),'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                base = f'http://127.0.0.1:{port}'
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:
                        opener.open(base + '/api/bookmakers/events',timeout=1).close(); break
                    except OSError: time.sleep(.1)
                else: raise AssertionError('Fixture API did not start')
                asyncio.run(run(base))
            finally:
                server.terminate(); server.wait(timeout=20)
    finally:
        ai.shutdown(); ai.server_close()


if __name__ == '__main__':
    main()
