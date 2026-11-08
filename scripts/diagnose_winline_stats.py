#!/usr/bin/env python3
"""Read-only anonymous statistics-page diagnostic; never changes running services."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

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
        browser=await playwright.chromium.launch(headless=True,args=['--disable-gpu'],env={key:value for key,value in os.environ.items() if key not in ('BOOKMAKER_INGEST_TOKEN','QWEN_API_KEY')})
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
            print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
        finally:
            await browser.close()


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='https://winline.ru/stats/match/72334410')
    parser.add_argument('--seconds',type=int,default=10)
    parser.add_argument('--output-dir',type=Path,default=Path('/var/lib/matchlab-crawler/diagnostics'))
    args=parser.parse_args()
    parsed=urlsplit(args.url)
    if parsed.scheme!='https' or parsed.hostname not in ('winline.ru','www.winline.ru') or parsed.username or parsed.password or not re.fullmatch(r'/stats/match/\d{1,20}/?',parsed.path) or parsed.query or parsed.fragment:
        parser.error('Нужен публичный адрес https://winline.ru/stats/match/ID без параметров.')
    if not 1<=args.seconds<=60: parser.error('Ожидание — от 1 до 60 секунд.')
    asyncio.run(run(args))


if __name__=='__main__': main()
