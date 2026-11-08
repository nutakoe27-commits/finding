"""Synthetic DOM using real public labels/prices; optional real Scrapy run."""
import argparse
import asyncio
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
import socket
import subprocess
import tempfile
import time
from threading import Thread
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from matchlab.winline_markets import MARKETS_SCRIPT, MARKET_CONTEXT_SCRIPT, ensure_event_page, market_document, primary_quote, read_marketbook
from scripts.diagnose_winline_header import HEADER_SCRIPT

PROJECTION = json.loads((Path(__file__).parent / 'fixtures/winline_market_projection.json').read_text())
GOAL_VARIANTS = json.loads((Path(__file__).parent / 'fixtures/winline_goal_market_variants.json').read_text())


def fixture_html(*, collapsed=False, unsupported=False, virtual=False):
    chunks = ['''<!doctype html><meta charset="utf-8"><style>ww-feature-marketbook-dsk{display:block}.market__row-btn span{display:block}</style>
      <div class="marketbook-header--prematch"><div class="prematch__competitor-left"><h1>Сандерленд</h1>Н В П</div>
      <div class="prematch-info">Завтра<br>21:00</div><div class="prematch__competitor-right"><h1>Челси</h1>В П Н</div></div>
      <ww-feature-marketbook-dsk><div class="marketbook__filter"><div class="ww-tabs__item" onclick="this.classList.add('ww-tabs__item--active')">Все</div></div>''']
    for index, section in enumerate(PROJECTION['sections']):
        chunks.append('<h2>' + escape(section['group']) + '</h2><section class="market">')
        chunks.append('<button class="market-header" onclick="this.parentElement.querySelector(\'.market__table\').hidden=false">' + escape(section['name']) + '</button>')
        chunks.append('<div class="market__table"' + (' hidden' if collapsed and index == 0 else '') + '>')
        for row in section['rows']:
            chunks.append('<div class="source-row"><span>' + escape(row['context']) + '</span>')
            for name, price in row['outcomes']:
                label = '' if unsupported and name == 'П1' and row['context'] == 'осн.время' else '<span>' + escape(name) + '</span>'
                chunks.append('<button class="market__row-btn" onclick="window.betClicks++">' + label + '<span>' + str(price) + '</span></button>')
            chunks.append('</div>')
        chunks.append('</div></section>')
    chunks.append('</ww-feature-marketbook-dsk><script>window.betClicks=0;</script>')
    return ''.join(chunks)


def live_fixture_html():
    # Header structure and team order observed on the public live match page.
    source = fixture_html()
    start = source.index('<div class="marketbook-header--prematch">')
    end = source.index('<ww-feature-marketbook-dsk>', start)
    live_header = '''<div class="ww-markets"><div class="ww-markets__stickybar">
      <ww-feature-marketbook-stickybar-dsk><div class="marketbook-header marketbook-header--live">
      <div class="marketbook-header__top"><div class="competitors__wrapper"><div class="competitors__info">
      <div class="competitors__logos"><div class="logo-wrapper"></div><div class="logo-wrapper"></div></div>
      <div class="competitors__names competitors-desc"><div class="competitors-desc__block"></div>
      <div class="competitors-desc__name">Витория Гимарайнш</div><div class="competitors-desc__block"></div>
      <div class="competitors-desc__block"></div><div class="competitors-desc__name">Порту</div>
      <div class="competitors-desc__block"></div></div></div></div>
      <div class="score-block__wrapper">1Т 20'<br>0<br>1</div></div></div></ww-feature-marketbook-stickybar-dsk>
      </div></div>'''
    return source[:start] + live_header + source[end:]


async def browser_test(executable):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=executable)
        try:
            page = await browser.new_page(locale='ru-RU', timezone_id='Europe/Moscow')
            await page.set_content('<div class="ww-markets"><div class="ww-markets__stickybar"><div class="marketbook-header--live"><div class="team-home">Команда А</div><div class="team-away">Команда Б</div><form>PRIVATE_FORM<input value="secret"></form></div></div><ww-feature-marketbook-dsk><h2>Рынок скрыт из диагностики</h2></ww-feature-marketbook-dsk></div>')
            header=await page.evaluate(HEADER_SCRIPT)
            assert header['found'] and any('Команда А' in item['text'] for item in header['candidates'])
            assert 'PRIVATE_FORM' not in json.dumps(header,ensure_ascii=False) and 'Рынок скрыт' not in json.dumps(header,ensure_ascii=False)
            await page.set_content(live_fixture_html())
            live_url='https://winline.ru/stavki/sport/futbol/portugaliya/premijer_liga/17068673'
            live_snapshot=await read_marketbook(page,maximum_passes=10)
            live=market_document(live_snapshot,live_url,datetime.now(timezone.utc).isoformat())
            assert (live['home'],live['away'],live['phase'],live['event_kind']) == ('Витория Гимарайнш','Порту','live','match'),live
            assert live['coverage']['complete'] and live['coverage']['captured_outcomes']==13
            assert live['kickoff_at'] is None and primary_quote(live) is None
            await page.locator('.ww-markets').evaluate("el=>el.insertAdjacentHTML('beforeend','<div class=marketbook-header--prematch><div class=prematch__competitor-left>Устаревшие</div><div class=prematch__competitor-right>Данные</div></div>')")
            mixed=await page.evaluate(MARKETS_SCRIPT)
            assert (mixed['home'],mixed['away'],mixed['phase']) == ('Витория Гимарайнш','Порту','live')
            await page.locator('.competitors__names').evaluate("el=>el.insertAdjacentHTML('beforeend','<div class=competitors-desc__name>Третья команда</div>')")
            ambiguous_live=await page.evaluate(MARKETS_SCRIPT)
            assert not ambiguous_live['home'] and not ambiguous_live['away'] and ambiguous_live['phase']=='live'
            async def event_fixture(route):
                await route.fulfill(status=200,content_type='text/html',body=fixture_html())
            await page.route('https://winline.ru/**',event_fixture)
            alias='https://winline.ru/stavki/event/17006967'
            await page.goto(alias)
            await page.evaluate("setTimeout(() => history.replaceState({}, '', '/stavki/sport/futbol/angliya/apl/17006967'), 500)")
            await ensure_event_page(page,alias)
            assert '/sport/futbol/' in page.url
            await page.unroute('https://winline.ru/**',event_fixture)
            await page.set_content(fixture_html(collapsed=True))
            snapshot = await read_marketbook(page, maximum_passes=10)
            document = market_document(snapshot, PROJECTION['url'], datetime.now(timezone.utc).isoformat())
            assert document['coverage']['complete'], document['coverage']
            assert document['coverage']['captured_outcomes'] == 13
            full = next(m for m in document['markets'] if m['name'] == 'Исход 1X2' and m['period'] == 'осн.время')
            half = next(m for m in document['markets'] if m['name'] == 'Исход 1X2' and m['period'] == '1 тайм')
            assert full['outcomes'][0]['odds'] == 3.9 and half['outcomes'][0]['odds'] == 4.3
            assert primary_quote(document)['snapshots'][0]['odds']['home'] == 3.9
            assert await page.evaluate('window.betClicks') == 0
            await page.locator('.marketbook-header--prematch').evaluate("node=>node.insertAdjacentHTML('beforeend','<a href=\"https://winline.ru/stats/match/72334410\">Статистика</a>')")
            await page.locator('body').evaluate("node=>node.insertAdjacentHTML('beforeend','<a href=\"https://winline.ru/stats/match/999\">Чужая статистика вне события</a>')")
            linked=market_document(await read_marketbook(page,maximum_passes=10),PROJECTION['url'],datetime.now(timezone.utc).isoformat())
            assert linked['statistics_url']=='https://winline.ru/stats/match/72334410'
            await page.locator('ww-feature-marketbook-dsk').evaluate('''root => root.insertAdjacentHTML('beforeend', `<h2>Исход</h2><section class="market"><button class="market-header">Победит (осн.время)</button><div class="market__table"><h4>Сандерленд</h4><button class="market__row-btn"><span>Да</span><span>3.55</span></button><button class="market__row-btn"><span>Нет</span><span>1.25</span></button><h4>Челси</h4><button class="market__row-btn"><span>Да</span><span>1.83</span></button><button class="market__row-btn"><span>Нет</span><span>1.88</span></button></div></section>`)''')
            teams = market_document(await read_marketbook(page, maximum_passes=10), PROJECTION['url'], datetime.now(timezone.utc).isoformat())
            variants = [market for market in teams['markets'] if market['name'] == 'Победит (осн.время)']
            assert {market['parameter'] for market in variants} == {'Сандерленд', 'Челси'}
            assert teams['coverage']['complete'] and teams['coverage']['captured_outcomes'] == 17
            # Real public report: two distinct DOM blocks share a title and
            # two outcome labels, but differ in the rest of their selections.
            blocks='<h2>Тотал</h2>'
            for outcomes in GOAL_VARIANTS['variants']:
                blocks+='<ww-marketbook-market-dsk><div><button class="market-header">'+escape(GOAL_VARIANTS['market'])+'</button><div class="market__table"><div class="market__row">'
                for name,price in outcomes:
                    blocks+=f'<button class="market__row-btn"><span>{escape(name)}</span><span>{price}</span></button>'
                blocks+='</div></div></div></ww-marketbook-market-dsk>'
            await page.set_content(fixture_html()+f'<script>document.querySelector("ww-feature-marketbook-dsk").insertAdjacentHTML("beforeend",{json.dumps(blocks)});</script>')
            separate=market_document(await read_marketbook(page,maximum_passes=10),PROJECTION['url'],datetime.now(timezone.utc).isoformat())
            found=[market for market in separate['markets'] if market['name']==GOAL_VARIANTS['market']]
            assert separate['coverage']['complete'] and separate['coverage']['captured_outcomes']==21,separate['coverage']
            assert len(found)==2 and len({market['variant_id'] for market in found})==2,found
            assert all(market['source_market_id'] is None for market in found)
            observed=[[(outcome['name'],outcome['odds']) for outcome in market['outcomes']] for market in found]
            assert observed==[[tuple(outcome) for outcome in variant] for variant in GOAL_VARIANTS['variants']],observed
            keys={market['variant_id']:market['key'] for market in found}
            await page.locator('ww-marketbook-market-dsk').first.locator('.market__row-btn').first.locator('span').last.evaluate("node => node.textContent='3.80'")
            refreshed=market_document(await read_marketbook(page,maximum_passes=10),PROJECTION['url'],datetime.now(timezone.utc).isoformat())
            found_again=[market for market in refreshed['markets'] if market['name']==GOAL_VARIANTS['market']]
            assert {market['variant_id']:market['key'] for market in found_again}==keys
            assert found_again[0]['outcomes'][0]['odds']==3.8
            blank_attributes=blocks.replace('<h2>Тотал</h2>','<h2></h2>').replace('class="market__row-btn"','class="market__row-btn" data-id=""')
            await page.set_content(fixture_html()+f'<script>document.querySelector("ww-feature-marketbook-dsk").insertAdjacentHTML("beforeend",{json.dumps(blank_attributes)});</script>')
            blanks=market_document(await read_marketbook(page,maximum_passes=10),PROJECTION['url'],datetime.now(timezone.utc).isoformat())
            assert blanks['coverage']['complete'] and blanks['coverage']['captured_outcomes']==21,blanks['coverage']
            blank_variants=[market for market in blanks['markets'] if market['name']==GOAL_VARIANTS['market']]
            assert len(blank_variants)==2 and all(market['group'] is None for market in blank_variants)
            invalid_blocks=blocks.replace('<button class="market__row-btn"><span>4-6</span>','<button class="market__row-btn" data-id="'+('x'*101)+'"><span>4-6</span>')
            await page.set_content(fixture_html()+f'<script>document.querySelector("ww-feature-marketbook-dsk").insertAdjacentHTML("beforeend",{json.dumps(invalid_blocks)});</script>')
            invalid_snapshot=await read_marketbook(page,maximum_passes=10)
            invalid=market_document(invalid_snapshot,PROJECTION['url'],datetime.now(timezone.utc).isoformat())
            assert not invalid['coverage']['complete'] and invalid['coverage']['unparsed_outcomes']==4,invalid['coverage']
            assert invalid['coverage']['captured_outcomes']==17 and primary_quote(invalid)
            assert invalid_snapshot['unsupported'][0]['field']=='outcome.source_outcome_id'
            same_sets=blocks.replace('4-6','4-5').replace('7+','6+')
            await page.set_content(fixture_html()+f'<script>document.querySelector("ww-feature-marketbook-dsk").insertAdjacentHTML("beforeend",{json.dumps(same_sets)});</script>')
            same=market_document(await read_marketbook(page,maximum_passes=10),PROJECTION['url'],datetime.now(timezone.utc).isoformat())
            assert not same['coverage']['complete'] and same['coverage']['unparsed_outcomes']==8,same['coverage']
            assert not [market for market in same['markets'] if market['name']==GOAL_VARIANTS['market']]
            # Missing distinguishing context must not silently choose one of
            # two prices or discard unrelated, correctly parsed markets.
            await page.set_content(fixture_html()+'''<script>document.querySelector('ww-feature-marketbook-dsk').insertAdjacentHTML('beforeend',`<h2>Неоднозначная группа</h2><section><button class="market-header">Неоднозначный рынок</button><div class="market__table"><div><span>осн.время</span><button class="market__row-btn"><span>Да</span><span>2.0</span></button><button class="market__row-btn"><span>Нет</span><span>3.0</span></button></div><div><span>осн.время</span><button class="market__row-btn"><span>Да</span><span>4.0</span></button><button class="market__row-btn"><span>Нет</span><span>5.0</span></button></div></div></section>`)</script>''')
            ambiguous_snapshot=await read_marketbook(page,maximum_passes=10)
            ambiguous=market_document(ambiguous_snapshot,PROJECTION['url'],datetime.now(timezone.utc).isoformat())
            assert not ambiguous['coverage']['complete'],ambiguous['coverage']
            assert ambiguous['coverage']['captured_outcomes']==13 and ambiguous['coverage']['unparsed_outcomes']==4,ambiguous['coverage']
            assert all(market['name']!='Неоднозначный рынок' for market in ambiguous['markets'])
            assert primary_quote(ambiguous)['snapshots'][0]['odds']['home']==3.9
            assert ambiguous_snapshot['unsupported'][0]['reason']=='ambiguous_outcome_context'
            await page.locator('button.market-header').filter(has_text='Неоднозначный рынок').evaluate("header => header.parentElement.insertAdjacentHTML('beforeend','<form>PRIVATE_FORM_CONTENT<input value=private></form>')")
            structure=await page.evaluate(MARKET_CONTEXT_SCRIPT,{'names':['Неоднозначный рынок'],'labels':['Да','Нет']})
            assert structure and len(structure[0]['outcomes'])==4
            assert 'PRIVATE_FORM_CONTENT' not in json.dumps(structure,ensure_ascii=False)
            await page.set_content(fixture_html(unsupported=True))
            partial = market_document(await read_marketbook(page, maximum_passes=5), PROJECTION['url'], datetime.now(timezone.utc).isoformat())
            assert not partial['coverage']['complete']
            assert partial['coverage']['unparsed_outcomes'] == 1
            # A virtual table reuses the same title/button count for different
            # lines. Scrolling must merge all of them, not only the last slice.
            virtual = fixture_html() + '''<style>body{height:5000px}ww-feature-marketbook-dsk{position:fixed;top:70px;left:0;right:0;background:white}</style><script>
                const root=document.querySelector('ww-feature-marketbook-dsk');
                let last=-1;
                function draw(){const bucket=Math.min(4,Math.floor(window.scrollY/900));if(bucket===last)return;last=bucket;
                root.innerHTML='<div class="marketbook__filter"><div class="ww-tabs__item ww-tabs__item--active">Все</div></div><h2>Тотал</h2><section class="market"><button class="market-header">Тотал (осн.время)</button><div class="market__table"><div><span>осн.время</span><button class="market__row-btn"><span>б '+(bucket+0.5)+'</span><span>1.50</span></button><button class="market__row-btn"><span>м '+(bucket+0.5)+'</span><span>2.50</span></button></div></div></section>';}
                window.addEventListener('scroll',draw);draw();</script>'''
            await page.set_content(virtual)
            await page.evaluate('window.scrollTo(0,0)')
            merged = market_document(await read_marketbook(page, maximum_passes=40), PROJECTION['url'], datetime.now(timezone.utc).isoformat())
            assert merged['coverage']['complete'], merged['coverage']
            assert merged['coverage']['captured_outcomes'] == 10, merged
            virtual_variants=fixture_html()+'''<style>body{height:5000px}ww-feature-marketbook-dsk{position:fixed;top:70px;left:0;right:0;background:white}</style><script>
                const root=document.querySelector('ww-feature-marketbook-dsk'); let last=-1;
                function draw(){const bucket=Math.min(4,Math.floor(window.scrollY/900));if(bucket===last)return;last=bucket;
                  const market=(name,rows)=>'<section><button class="market-header">'+name+'</button><div class="market__table"><div><span>осн.время</span>'+rows.map(([label,price])=>'<button class="market__row-btn"><span>'+label+'</span><span>'+price+'</span></button>').join('')+'</div></div></section>';
                  const pairs=bucket===0 ? [[['A','2.0'],['B','3.0']],[['C','2.1'],['D','3.1']]] : [[['A','2.0'],['C','3.0']],[['B','2.1'],['D','3.1']]];
                  root.innerHTML='<div class="marketbook__filter"><div class="ww-tabs__item ww-tabs__item--active">Все</div></div><h2>Тотал</h2>'+
                    market('Исход 1X2',[['П1','2.0'],['X','3.0'],['П2','4.0']])+
                    market('Скрытый вариант',pairs[0])+market('Скрытый вариант',pairs[1]);}
                window.addEventListener('scroll',draw);draw();</script>'''
            await page.goto('about:blank')
            await page.set_content(virtual_variants)
            await page.evaluate('window.scrollTo(0,0)')
            virtual_result=market_document(await read_marketbook(page,maximum_passes=40),PROJECTION['url'],datetime.now(timezone.utc).isoformat())
            assert not virtual_result['coverage']['complete'] and virtual_result['coverage']['unparsed_outcomes']>0,virtual_result['coverage']
            assert not any(market.get('variant_id') for market in virtual_result['markets'])
        finally:
            await browser.close()
    print('PASS: All tab, collapsed groups, period/parameter separation, virtual-list union, no bet clicks, ambiguous variants excluded while valid markets survive, incomplete coverage stays explicit')


def scrapy_test(executable, publish=False):
    # Uses the real Scrapy download handler and pipeline, with a local HTTP
    # fixture. It never contacts Winline or a production receiver.
    from scrapy.crawler import CrawlerProcess
    from matchlab.scrapy_worker import WinlineSpider, settings, stamp
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass
        def do_GET(self):
            body = fixture_html().encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            api_server = None
            api_url = 'http://127.0.0.1:1'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if publish:
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    api_port = probe.getsockname()[1]
                api_url = f'http://127.0.0.1:{api_port}'
                root = Path(__file__).resolve().parent.parent
                os.environ['BOOKMAKER_INGEST_TOKEN'] = 'isolated-crawler-integration-test-key'
                env = dict(os.environ, MATCHLAB_DATA_DIR=str(Path(folder) / 'api-data'), QWEN_BASE_URL='', QWEN_MODEL='', QWEN_API_KEY='')
                api_server = subprocess.Popen([str(root / '.venv/bin/python'), '-m', 'uvicorn', 'matchlab.api:app', '--host', '127.0.0.1', '--port', str(api_port)],
                                              cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(100):
                    try:
                        opener.open(api_url + '/api/bookmakers/events', timeout=1).close()
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    api_server.terminate()
                    api_server.wait(timeout=10)
                    raise AssertionError('Test receiver did not start')
            class FixtureSpider(WinlineSpider):
                allowed_domains = ['127.0.0.1']
                async def start(self):
                    self.events.add('17006967')
                    yield self.request(f'http://127.0.0.1:{server.server_port}/fixture', self.fixture_event)
                async def fixture_event(self, response):
                    page = response.meta['playwright_page']
                    try:
                        item = market_document(await read_marketbook(page), PROJECTION['url'], stamp())
                    finally:
                        await page.close()
                    yield item
            args = argparse.Namespace(executable=executable, concurrency=1, cycle_timeout=30)
            config = settings(args)
            config['DOWNLOAD_HANDLERS']['http'] = config['DOWNLOAD_HANDLERS']['https']
            process = CrawlerProcess(config)
            crawler = process.create_crawler(FixtureSpider)
            try:
                process.crawl(crawler, state_dir=folder, api_url=api_url, no_publish=not publish, check=publish)
                process.start()
                stats = crawler.stats.get_stats()
                assert stats.get('markets/saved_events') == 1, stats
                assert stats.get('markets/complete_events') == 1, stats
                assert (Path(folder) / 'events/17006967.json.gz').exists()
                if publish:
                    assert stats.get('markets/published_complete_events') == 1, stats
                    assert stats.get('markets/primary_quotes') == 1, stats
                    asyncio.run(crawler.spider.publish_status('ok'))
                    with opener.open(api_url + '/api/bookmakers/events/17006967/markets') as response:
                        document = json.load(response)
                    assert document['coverage']['complete']
                    assert document['coverage']['captured_outcomes'] == 13
                    with opener.open(api_url + '/api/bookmakers') as response:
                        body = json.load(response)
                    assert body['quotes'][0]['odds'] == {'home': 3.9, 'draw': 3.75, 'away': 1.89}
                    assert body['status']['worker']['mode'] == 'event_markets'
            finally:
                if api_server:
                    api_server.terminate()
                    api_server.wait(timeout=20)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print('PASS: real Scrapy + scrapy-playwright + Chromium + validated gzip pipeline; all bookmaker responses are local fixtures')
    if publish:
        print('PASS: real authenticated receiver, compressed market storage, primary 1X2 projection and worker progress, using an isolated test database')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium', default=shutil.which('chromium'))
    parser.add_argument('--scrapy', action='store_true')
    parser.add_argument('--publish-test', action='store_true')
    args = parser.parse_args()
    if args.scrapy:
        scrapy_test(args.chromium, args.publish_test)
    else:
        asyncio.run(browser_test(args.chromium))
