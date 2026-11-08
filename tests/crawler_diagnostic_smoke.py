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
from browser_market_smoke import live_fixture_html
from matchlab.scrapy_worker import WinlineSpider,settings


def main():
    from scrapy.crawler import CrawlerProcess
    no_publish='--no-publish' in sys.argv[1:]
    invalid_label='--invalid-label' in sys.argv[1:]
    live='--live' in sys.argv[1:]
    variants='--variants' in sys.argv[1:] or invalid_label
    item=next(item for item in PROJECTIONS if item['sport']=='football')
    extra='''<section><button class="market-header">Неоднозначный рынок</button><div class="market__table"><div><span>осн.время</span><button class="market__row-btn"><span>Да</span><span>2.0</span></button><button class="market__row-btn"><span>Нет</span><span>3.0</span></button></div><div><span>осн.время</span><button class="market__row-btn"><span>Да</span><span>4.0</span></button><button class="market__row-btn"><span>Нет</span><span>5.0</span></button></div></div><form>PRIVATE_FORM_CONTENT<input value="secret"></form></section>'''
    real=json.loads((ROOT/'tests/fixtures/winline_goal_market_variants.json').read_text())
    if variants:
        extra=''
        for selections in real['variants']:
            extra+='<ww-marketbook-market-dsk><div><button class="market-header">'+real['market']+'</button><div class="market__table"><div class="market__row">'
            for name,price in selections:
                extra+=f'<button class="market__row-btn"><span>{name}</span><span>{price}</span></button>'
            extra+='</div></div></div></ww-marketbook-market-dsk>'
        if invalid_label:
            extra=extra.replace('<button class="market__row-btn"><span>4-6</span>','<button class="market__row-btn" data-id="'+('x'*101)+'"><span>4-6</span>')
    html=(live_fixture_html() if live else event_html(item)).replace('</ww-feature-marketbook-dsk>',extra+'</ww-feature-marketbook-dsk>')
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
            process.crawl(crawler,state_dir=cache,api_url=api_url,seed=url,no_publish=no_publish)
            process.start()
            if not variants or invalid_label:
                data=json.loads((cache/'market-diagnostic.json').read_text())
                assert data['url']==url and data['unparsed_outcomes']==4,data
                if invalid_label:
                    assert data['examples'][0]['reason']=='invalid_source_label' and data['examples'][0]['field']=='outcome.source_outcome_id',data
                else:
                    assert data['examples'][0]['reason']=='ambiguous_outcome_context',data
                    assert data['market_structure'] and len(data['market_structure'][0]['outcomes'])==4,data
                assert 'PRIVATE_FORM_CONTENT' not in json.dumps(data,ensure_ascii=False)
                assert token not in json.dumps(data,ensure_ascii=False)
            if no_publish:
                with opener.open(api_url+'/api/bookmakers/events') as response:
                    assert json.load(response)['total']==0
                print('PASS: isolated seed probe writes diagnostics locally without publishing any event')
            else:
                with opener.open(api_url+'/api/bookmakers/events/'+item['id']+'/markets') as response:
                    document=json.load(response)
                if live:
                    assert (document['home'],document['away'],document['phase'],document['event_kind']) == ('Витория Гимарайнш','Порту','live','match'),document
                    assert document['kickoff_at'] is None
                if variants and not invalid_label:
                    found=[market for market in document['markets'] if market['name']==real['market']]
                    assert document['coverage']['complete'] and len(found)==2,document['coverage']
                    assert len({market['variant_id'] for market in found})==2
                    assert [[(outcome['name'],outcome['odds']) for outcome in market['outcomes']] for market in found]==[[tuple(outcome) for outcome in variant] for variant in real['variants']]
                    print('PASS: real browser → crawler → authenticated receiver preserves both observed football market variants as a complete snapshot')
                else:
                    assert not document['coverage']['complete'] and document['coverage']['unparsed_outcomes']==4
                    if invalid_label:
                        assert len([market for market in document['markets'] if market['name']==real['market']])==1
                    print('PASS: real browser → crawler → private bounded diagnostic plus partial event delivery; no form or key content')
        finally:
            server.terminate(); server.wait(timeout=10)


if __name__=='__main__': main()
