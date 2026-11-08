"""Real preflight shutdown with a busy listing, blocked event DOM and HTTP receiver."""
import asyncio
from argparse import Namespace
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from crawler_multisport_smoke import NAV,PROJECTIONS,event_html
from matchlab.scrapy_worker import WinlineSpider,settings


def child(mode):
    from scrapy.crawler import CrawlerProcess
    with tempfile.TemporaryDirectory(prefix='crawler-stop-') as folder:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        api_url=f'http://127.0.0.1:{port}'
        token='crawler-shutdown-test-key-1234567890'
        os.environ['BOOKMAKER_INGEST_TOKEN']=token
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,QWEN_BASE_URL='',QWEN_MODEL='',QWEN_API_KEY='')
        server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for _ in range(100):
                try: opener.open(api_url+'/api/bookmakers/events',timeout=1).close(); break
                except OSError: time.sleep(.1)
            else: raise AssertionError('Receiver did not start')
            item=next(item for item in PROJECTIONS if item['sport']=='hockey')
            class BusySpider(WinlineSpider):
                async def prepare_page(self,page,request):
                    await super().prepare_page(page,request)
                    # Feed freshness has separate transport tests; no external
                    # bookmaker calls are made by this shutdown acceptance test.
                    request.meta['market_feed']=[{'connected':True,'last_frame':time.monotonic()}]
                    async def fixture(route):
                        good=route.request.url.endswith('/'+item['id'])
                        body=event_html(item) if good else NAV+'<style>body{min-height:1000000px}</style>'
                        await route.fulfill(status=200,content_type='text/html',body='<meta charset="utf-8">'+body)
                    await page.route('**/*',fixture)

                async def start(self):
                    yield self.request('https://winline.ru/stavki',self.parse_listing)
                    yield self.request('https://winline.ru/stavki/sport/futbol/test/league/99999',self.parse_event)
                    if mode=='verified':
                        yield self.request(f'https://winline.ru/stavki/sport/{item["slug"]}/test/league/{item["id"]}',self.parse_event)

            args=Namespace(executable=shutil.which('chromium'),concurrency=3,cycle_timeout=3 if mode=='deadline' else 12)
            config=settings(args); config['DOWNLOAD_DELAY']=0
            process=CrawlerProcess(config)
            crawler=process.create_crawler(BusySpider)
            process.crawl(crawler,state_dir=Path(folder)/'cache',api_url=api_url,check=True,max_scrolls=200)
            started=time.monotonic(); process.start(); elapsed=time.monotonic()-started
            stats=crawler.stats.get_stats()
            assert elapsed<10,(elapsed,stats)
            assert stats.get('markets/failed_events',0)==0,stats
            if mode=='verified':
                assert stats['finish_reason']=='verified_complete_event',stats
                assert stats['markets/published_complete_events']>=1,stats
                with opener.open(api_url+'/api/bookmakers/events?include_started=true') as response:
                    assert json.load(response)['total']>=1
            else:
                assert stats['finish_reason']=='cycle_timeout',stats
                assert stats.get('markets/published_complete_events',0)==0,stats
            print('PASS:',mode,'closed busy listing and event callbacks in',round(elapsed,2),'seconds',flush=True)
        finally:
            server.terminate(); server.wait(timeout=10)


if __name__=='__main__':
    if len(sys.argv)==2:
        child(sys.argv[1])
    else:
        for mode in ('verified','deadline'):
            process=subprocess.Popen([sys.executable,__file__,mode],start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            try: output,_=process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL); output,_=process.communicate()
                raise AssertionError('Shutdown exceeded test deadline: '+output[-3000:])
            assert process.returncode==0,output[-5000:]
            print(output.strip())
