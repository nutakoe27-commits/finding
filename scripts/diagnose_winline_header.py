#!/usr/bin/env python3
"""Inspect the public match header only; do not read forms or send data."""
import argparse
import asyncio
import json
import os
from urllib.parse import urlsplit,urlunsplit

from matchlab.winline_browser import event_id_from_url


HEADER_SCRIPT=r'''() => {
  const root=document.querySelector('.ww-markets');
  if (!root) return {found:false,children:[],candidates:[]};
  const text=element=>{
    let value=element.innerText || '';
    for (const form of element.querySelectorAll('form')) if (form.innerText) value=value.split(form.innerText).join('');
    return value.trim().slice(0,250);
  };
  const describe=element=>({tag:element.tagName.toLowerCase(),class:(element.getAttribute('class') || '').slice(0,200),
    text:text(element),alt:(element.getAttribute('alt') || '').slice(0,150),
    aria:(element.getAttribute('aria-label') || '').slice(0,150),testid:(element.getAttribute('data-testid') || '').slice(0,150)});
  const allowed=element=>!element.closest('form,ww-feature-marketbook-dsk,script,style') && element.getClientRects().length;
  const selectors='.ww-markets__stickybar,[class*="marketbook-header"],[class*="competitor"],[class*="team"],[data-testid],h1,h2';
  return {found:true,children:[...root.children].filter(allowed).slice(0,4).map(describe),
    candidates:[...root.querySelectorAll(selectors)].filter(allowed).slice(0,15).map(element=>({
      ...describe(element),children:[...element.children].filter(allowed).slice(0,4).map(describe),
      images:[...element.querySelectorAll('img[alt]')].filter(allowed).slice(0,2).map(describe)
    }))};
}'''


async def run(url):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        environment={key:value for key,value in os.environ.items() if key not in ('BOOKMAKER_INGEST_TOKEN','QWEN_API_KEY')}
        browser=await playwright.chromium.launch(headless=True,args=['--disable-gpu'],env=environment)
        try:
            context=await browser.new_context(locale='ru-RU',timezone_id='Europe/Moscow')
            async def resources(route):
                if route.request.resource_type in ('image','media','font'): await route.abort()
                else: await route.continue_()
            await context.route('**/*',resources)
            page=await context.new_page()
            result={'requested_event_id':event_id_from_url(url)}
            try:
                response=await page.goto(url,wait_until='domcontentloaded',timeout=45000)
                result['status']=response.status if response else None
                await page.wait_for_selector('ww-feature-marketbook-dsk button.market-header',timeout=45000)
                await asyncio.sleep(2)
            except Exception as error:
                result['error']=type(error).__name__
            parsed=urlsplit(page.url)
            result['url']=urlunsplit((parsed.scheme,parsed.hostname or '',parsed.path,'',''))
            result['resolved_event_id']=event_id_from_url(result['url'])
            result['same_event']=result['resolved_event_id']==result['requested_event_id']
            result['header']=await page.evaluate(HEADER_SCRIPT)
            print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
        finally:
            await browser.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    args=parser.parse_args()
    parsed=urlsplit(args.url)
    if not event_id_from_url(args.url) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('Нужен публичный URL спортивного события Winline без параметров.')
    os.umask(0o077)
    asyncio.run(run(args.url))


if __name__=='__main__': main()
