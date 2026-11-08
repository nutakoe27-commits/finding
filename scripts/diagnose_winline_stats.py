#!/usr/bin/env python3
"""Read-only anonymous statistics-page diagnostic; never changes running services."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit, urljoin

SCRIPT=r'''() => {
    const safeText = element => {
        const clone = element.cloneNode(true);
        clone.querySelectorAll('form,input,textarea,script,style,noscript').forEach(node=>node.remove());
        clone.querySelectorAll('br').forEach(node=>node.replaceWith('\n'));
        clone.querySelectorAll('div,p,li,tr,h1,h2,h3,h4,section').forEach(node=>node.append('\n'));
        return (clone.textContent || '').replace(/[ \t]+/g,' ').replace(/\n{3,}/g,'\n\n').trim();
    };
    const visible = element => element.getClientRects().length && !element.closest('form');
    const headings = [...document.querySelectorAll('h1,h2,h3,h4,[role="heading"]')].filter(visible).slice(0,50).map(node=>({tag:node.tagName.toLowerCase(),class:node.className,text:safeText(node).slice(0,300)}));
    const tables = [...document.querySelectorAll('table,[role="table"],[role="grid"]')].filter(visible).slice(0,12).map(node=>({tag:node.tagName.toLowerCase(),class:node.className,text:safeText(node).slice(0,12000),rows:[...node.querySelectorAll('tr,[role="row"]')].slice(0,35).map(row=>[...row.querySelectorAll('th,td,[role="cell"],[role="columnheader"]')].map(cell=>safeText(cell).slice(0,200)))}));
    const nodes = [...document.querySelectorAll('[class*="stat"],[class*="match"],[class*="team"],[class*="head"], [data-testid]')].filter(visible).slice(0,150).map(node=>({tag:node.tagName.toLowerCase(),class:typeof node.className==='string'?node.className:'',testid:node.getAttribute('data-testid'),text:safeText(node).slice(0,700)}));
    const links=[...document.querySelectorAll('a[href]')].filter(visible).map(node=>({url:node.href,text:safeText(node).slice(0,160)})).filter(link=>/\/stats\/match\/|\/(?:stavki|live)\/(?:sport|event)\//.test(link.url)).slice(0,50);
    return {title:document.title,headings,tables,nodes,links,text:safeText(document.body).slice(0,40000)};
}'''


def public_url(value):
    parsed=urlsplit(value)
    path=parsed.path
    if re.search(r'auth|login|account|profile|cashier|token|signature|eyJ',path,re.I): path='/[private-path]'
    return f'{parsed.scheme}://{parsed.hostname or ""}{path}'


async def run(args):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        launch={'headless':not args.headed,'args':['--disable-gpu'],'env':{key:value for key,value in os.environ.items() if key not in ('BOOKMAKER_INGEST_TOKEN','QWEN_API_KEY')}}
        if args.channel: launch['channel']=args.channel
        browser=await playwright.chromium.launch(**launch)
        try:
            context=await browser.new_context(locale='ru-RU',timezone_id='Europe/Moscow')
            async def resources(route):
                if route.request.resource_type in ('image','media','font'): await route.abort()
                else: await route.continue_()
            await context.route('**/*',resources)
            page=await context.new_page()
            result={'requested_url':args.url,'observed_at':datetime.now(timezone.utc).isoformat(),'frames':[]}
            try:
                response=await page.goto(args.url,wait_until='domcontentloaded',timeout=45000)
                result['status']=response.status if response else None
                await asyncio.sleep(args.seconds)
                result['url']=public_url(page.url)
                for frame in page.frames[:8]:
                    try:
                        data=await asyncio.wait_for(frame.evaluate(SCRIPT),timeout=5)
                        for link in data['links']: link['url']=public_url(link['url'])
                        result['frames'].append({'url':public_url(frame.url),**data})
                    except Exception as error:
                        result['frames'].append({'url':public_url(frame.url),'error':type(error).__name__})
            except Exception as error:
                result['error']=type(error).__name__
            args.output_dir.mkdir(parents=True,exist_ok=True)
            path=args.output_dir/('stats-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'.json')
            path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            summary={key:value for key,value in result.items() if key!='frames'}
            summary['frames']=[{'url':frame['url'],'title':frame.get('title'),'error':frame.get('error'),
                               'headings':frame.get('headings',[])[:15],'tables':len(frame.get('tables',[])),
                               'nodes':frame.get('nodes',[])[:8],'links':frame.get('links',[])[:8],
                               'text_sample':frame.get('text','')[:4500]} for frame in result['frames'][:4]]
            summary['saved_to']=str(path)
            if args.probe_line:
                sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
                from matchlab.winline_browser import select_football_line, open_event_link
                try:
                    market_page=await context.new_page()
                    await market_page.goto('https://winline.ru/stavki',wait_until='domcontentloaded',timeout=45000)
                    await select_football_line(market_page)
                    anchor=market_page.locator('.block-tournament a[href*="/event/"]').first
                    target=await anchor.get_attribute('href')
                    event_id=int(urlsplit(target).path.rstrip('/').split('/')[-1])
                    await open_event_link(market_page,event_id=event_id)
                    await market_page.wait_for_selector('ww-feature-marketbook-dsk',timeout=30000)
                    probe=await market_page.evaluate(r'''() => {
                        const root=document.querySelector('.ww-markets') || document.body;
                        return {titles:[...document.querySelectorAll('h1')].map(node=>node.innerText),
                            links:[...root.querySelectorAll('a[href*="/stats/"]')].map(node=>({url:node.href,title:node.title,text:node.innerText})),
                            controls:[...root.querySelectorAll('.special__btns-wrapper a,.special__btns-wrapper button,button[title],button[aria-label]')].filter(node=>!node.closest('form')).slice(0,25).map(node=>({tag:node.tagName.toLowerCase(),class:node.className,title:node.title,aria:node.getAttribute('aria-label'),href:node.getAttribute('href')}))};
                    }''')
                    for link in probe['links']: link['url']=public_url(link['url'])
                    for control in probe['controls']:
                        if control.get('href'): control['href']=public_url(urljoin(market_page.url,control['href']))
                    summary['market_probe']={'url':public_url(market_page.url),'source_event_id':str(event_id),**probe}
                except Exception as error:
                    summary['market_probe']={'error':type(error).__name__}
                result['market_probe']=summary['market_probe']
                path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
        finally:
            await browser.close()


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='https://winline.ru/stats/match/72334410')
    parser.add_argument('--seconds',type=int,default=10)
    parser.add_argument('--headed',action='store_true',help='Open a separate visible browser, without the normal user profile')
    parser.add_argument('--channel',choices=('chrome','msedge'),help='Use an already installed browser')
    parser.add_argument('--probe-line',action='store_true',help='Inspect statistics links on one current football event')
    parser.add_argument('--output-dir',type=Path,default=Path('/var/lib/matchlab-crawler/diagnostics'))
    args=parser.parse_args()
    parsed=urlsplit(args.url)
    if parsed.scheme!='https' or parsed.hostname not in ('winline.ru','www.winline.ru') or parsed.username or parsed.password or not re.fullmatch(r'/stats/match/\d{1,20}/?',parsed.path) or parsed.query or parsed.fragment:
        parser.error('Нужен публичный адрес https://winline.ru/stats/match/ID без параметров.')
    if not 1<=args.seconds<=60: parser.error('Ожидание — от 1 до 60 секунд.')
    asyncio.run(run(args))


if __name__=='__main__': main()
