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
from matchlab.winline_markets import market_document, primary_quote, read_marketbook

PROJECTION = json.loads((Path(__file__).parent / 'fixtures/winline_market_projection.json').read_text())


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


async def browser_test(executable):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=executable)
        try:
            page = await browser.new_page(locale='ru-RU', timezone_id='Europe/Moscow')
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
        finally:
            await browser.close()
    print('PASS: All tab, collapsed groups, every outcome accounted, periods/parameters separated, virtual-list union, no price-button clicks, unsupported labels prevent completeness')


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
