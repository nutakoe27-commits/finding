"""Real Scrapy, Chromium, DOM parser and authenticated receiver; offline sports fixtures."""
import asyncio
from datetime import datetime, timezone
from html import escape
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
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from sports_fixtures import PROJECTIONS
from matchlab.scrapy_worker import WinlineSpider, init_page, settings
from matchlab.sports import sport_from_url

SEEN=[]
NAV='<a href="/stavki">Линия</a>'+''.join(f'<a href="/stavki/sport/{item["slug"]}/">{item["label"]}</a>' for item in PROJECTIONS)+'<a href="/stavki/sport/kibersport/">Киберспорт</a>'


def event_html(item):
    participants = (f'<div class="prematch__competitor-left"><h1>{escape(item["home"])}</h1></div><div class="prematch__competitor-right"><h1>{escape(item["away"])}</h1></div>' if item.get('home') else f'<h1>{escape(item["title"])}</h1>')
    result= NAV + '<div class="marketbook-header--prematch">'+participants+'<div class="prematch-info">Завтра<br>21:00</div></div>'
    result+='<ww-feature-marketbook-dsk style="display:block"><div class="marketbook__filter"><div class="ww-tabs__item ww-tabs__item--active">Все</div></div>'
    for market in item['markets']:
        result+='<section><button class="market-header">'+escape(market['name'])+'</button><div class="market__table"><div><span>'+escape(market['period'])+'</span>'
        for name,price in market['outcomes']:
            result+=f'<button class="market__row-btn" onclick="window.betClicks++"><span>{escape(name)}</span><span>{price}</span></button>'
        result+='</div></div></section>'
    return result+'</ww-feature-marketbook-dsk><style>.market__row-btn span{display:block}</style><script>window.betClicks=0;</script>'


async def local_init(page,request):
    await init_page(page,request)
    # Transport is unchanged by the multisport feature and is tested separately
    # in browser_capture_smoke.py. Supply only its fresh-state contract here;
    # the inventory, canonical routing, DOM parser and receiver are all real.
    request.meta['market_feed']=[{'connected':True,'last_frame':time.monotonic()}]
    async def fixture(route):
        parsed=urlsplit(route.request.url)
        assert parsed.hostname=='winline.ru', parsed.hostname
        SEEN.append(parsed.path)
        assert 'kibersport' not in parsed.path
        item=next((item for item in PROJECTIONS if parsed.path.rstrip('/').endswith('/'+item['id'])),None)
        if item:
            body=event_html(item)
            if '/event/' in parsed.path:
                # The public Angular application resolves its short alias to
                # the canonical sport route. Simulate that in-page transition
                # so redirect hops cannot escape the offline route fixture.
                canonical=f'/stavki/sport/{item["slug"]}/test/league/{item["id"]}'
                body+='<script>history.replaceState({},"",'+json.dumps(canonical)+');</script>'
        else:
            body=NAV
            sport=sport_from_url(route.request.url)
            for current in PROJECTIONS:
                if sport and current['sport']!=sport: continue
                title = f'<div>{escape(current["home"])}</div><div>{escape(current["away"])}</div>' if current.get('home') else escape(current['title'])
                body+=f'<section class="block-tournament"><h2>{escape(current["competition"])}</h2><a href="/stavki/event/{current["id"]}">{title}</a></section>'
        await route.fulfill(status=200,content_type='text/html',body='<!doctype html><meta charset="utf-8">'+body)
    await page.route('**/*',fixture)


class LocalSportsSpider(WinlineSpider):
    def request(self,*args,**kwargs):
        request=super().request(*args,**kwargs)
        request.meta['playwright_page_init_callback']=local_init
        return request

    async def failed(self,failure):
        print('Offline fixture navigation:',failure.getErrorMessage()[:600])
        await super().failed(failure)


def main():
    from argparse import Namespace
    from scrapy.crawler import CrawlerProcess
    with tempfile.TemporaryDirectory(prefix='multisport-crawler-') as folder:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        api_url=f'http://127.0.0.1:{port}'
        token='offline-multisport-test-key-1234567890'
        os.environ['BOOKMAKER_INGEST_TOKEN']=token
        env=dict(os.environ,MATCHLAB_DATA_DIR=str(Path(folder)/'receiver'),QWEN_BASE_URL='',QWEN_MODEL='',QWEN_API_KEY='')
        server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for _ in range(100):
                try:
                    opener.open(api_url+'/api/bookmakers/events',timeout=1).close(); break
                except OSError: time.sleep(.1)
            else: raise AssertionError('Test receiver did not start')
            args=Namespace(executable=shutil.which('chromium'),concurrency=3,cycle_timeout=60)
            config=settings(args)
            config['DOWNLOAD_DELAY']=0
            process=CrawlerProcess(config)
            crawler=process.create_crawler(LocalSportsSpider)
            process.crawl(crawler,state_dir=Path(folder)/'cache',api_url=api_url,max_scrolls=8,sports='all')
            process.start()
            stats=crawler.stats.get_stats()
            assert stats.get('markets/failed_events',0)==0, stats
            assert stats.get('markets/published_complete_events')==4, stats
            assert set(crawler.spider.sport_counts)=={item['sport'] for item in PROJECTIONS}
            with opener.open(api_url+'/api/bookmakers/events?include_started=true') as response:
                records=json.load(response)['events']
            assert len(records)==4, records
            tennis=next(event for event in records if event['sport']=='tennis')
            assert [outcome['name'] for outcome in tennis['headline']['outcomes']]==['П1','П2']
            race=next(event for event in records if event['sport']=='motorsport')
            assert race['title']=='Тестовая гонка' and race['home'] is None and race['away'] is None
            assert race['headline']['outcome_count']==4
            with opener.open(api_url+'/api/bookmakers') as response:
                quotes=json.load(response)['quotes']
            assert len(quotes)==1 and quotes[0]['sport']=='football', quotes
            for projection in PROJECTIONS:
                assert f'/stavki/sport/{projection["slug"]}/' in SEEN
            assert not any('kibersport' in path for path in SEEN)
            print('PASS: actual Scrapy discovery → public canonical routing → DOM markets → authenticated receiver for football, hockey, tennis and multi-participant racing; no esports requests; football quotes remain isolated')
        finally:
            server.terminate(); server.wait(timeout=20)


if __name__=='__main__': main()
