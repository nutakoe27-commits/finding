"""Real crawler writes source context for ambiguous prices without exposing forms."""
from argparse import Namespace
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

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from crawler_multisport_smoke import PROJECTIONS,event_html
from matchlab.scrapy_worker import WinlineSpider,settings


def main():
    from scrapy.crawler import CrawlerProcess
    item=next(item for item in PROJECTIONS if item['sport']=='football')
    extra='''<section><button class="market-header">Неоднозначный рынок</button><div class="market__table"><div><span>осн.время</span><button class="market__row-btn"><span>Да</span><span>2.0</span></button><button class="market__row-btn"><span>Нет</span><span>3.0</span></button></div><div><span>осн.время</span><button class="market__row-btn"><span>Да</span><span>4.0</span></button><button class="market__row-btn"><span>Нет</span><span>5.0</span></button></div></div><form>PRIVATE_FORM_CONTENT<input value="secret"></form></section>'''
    html=event_html(item).replace('</ww-feature-marketbook-dsk>',extra+'</ww-feature-marketbook-dsk>')
    url=f'https://winline.ru/stavki/sport/{item["slug"]}/test/league/{item["id"]}'
    with tempfile.TemporaryDirectory(prefix='crawler-diagnostic-') as folder:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        api_url=f'http://127.0.0.1:{port}'
        token='diagnostic-crawler-test-key-1234567890'
        os.environ['BOOKMAKER_INGEST_TOKEN']=token
        env=dict(os.environ,MATCHLAB_DATA_DIR=folder,QWEN_BASE_URL='',QWEN_MODEL='',QWEN_API_KEY='')
        server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','matchlab.api:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for _ in range(100):
                try: opener.open(api_url+'/api/bookmakers/events',timeout=1).close(); break
                except OSError: time.sleep(.1)
            else: raise AssertionError('Receiver did not start')
            class FixtureSpider(WinlineSpider):
                async def prepare_page(self,page,request):
                    await super().prepare_page(page,request)
                    request.meta['market_feed']=[{'connected':True,'last_frame':time.monotonic()}]
                    async def fixture(route):
                        assert route.request.url==url,route.request.url
                        await route.fulfill(status=200,content_type='text/html',body='<meta charset="utf-8">'+html)
                    await page.route('**/*',fixture)
            args=Namespace(executable=shutil.which('chromium'),concurrency=1,cycle_timeout=30)
            config=settings(args); config['DOWNLOAD_DELAY']=0
            process=CrawlerProcess(config); crawler=process.create_crawler(FixtureSpider)
            cache=Path(folder)/'cache'
            process.crawl(crawler,state_dir=cache,api_url=api_url,seed=url)
            process.start()
            data=json.loads((cache/'market-diagnostic.json').read_text())
            assert data['url']==url and data['unparsed_outcomes']==4,data
            assert data['examples'][0]['reason']=='ambiguous_outcome_context',data
            assert data['market_structure'] and len(data['market_structure'][0]['outcomes'])==4,data
            assert 'PRIVATE_FORM_CONTENT' not in json.dumps(data,ensure_ascii=False)
            assert token not in json.dumps(data,ensure_ascii=False)
            with opener.open(api_url+'/api/bookmakers/events/'+item['id']+'/markets') as response:
                document=json.load(response)
            assert not document['coverage']['complete'] and document['coverage']['unparsed_outcomes']==4
            print('PASS: real browser → crawler → private bounded diagnostic plus partial event delivery; no form or key content')
        finally:
            server.terminate(); server.wait(timeout=10)


if __name__=='__main__': main()
