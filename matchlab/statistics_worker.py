"""Lightweight browser collector for public team statistics on an accessible device."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import time
from urllib.parse import urlsplit

import httpx

from .statistics import collect_statistics
from .worker_protocol import market_feed
from .winline_markets import ensure_event_page, read_marketbook, discover_statistics_link, market_document, primary_quote
from .winline_browser import event_id_from_url

LOG=logging.getLogger(__name__)


def validate_config(config):
    if not isinstance(config,dict): raise ValueError('Нужна настройка сборщика статистики.')
    url=urlsplit(config.get('api_url',''))
    key=config.get('collector_key','')
    if config.get('format')!='matchlab.statistics-client.v1' or url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost','::1') or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
        raise ValueError('Нужен локальный API через существующий SSH-туннель.')
    if not isinstance(key,str) or not 32<=len(key)<=500 or any(ord(char)<32 for char in key):
        raise ValueError('Ключ сборщика отсутствует в локальной настройке.')
    return config


def read_config(path):
    if path.is_symlink(): raise ValueError('Файл настройки не должен быть ссылкой.')
    if path.stat().st_mode&0o077: raise ValueError('Права настройки должны быть 600: chmod 600 '+str(path))
    return validate_config(json.loads(path.read_text()))


def runtime_config(args):
    if args.api_url:
        return validate_config({'format':'matchlab.statistics-client.v1','api_url':args.api_url,
                                'collector_key':os.environ.get('BOOKMAKER_INGEST_TOKEN','')})
    return read_config(args.config or Path('.local/statistics-client.json'))


def report(args,value):
    value={'at':datetime.now(timezone.utc).isoformat(),**value}
    if args.status_file:
        path=args.status_file
        path.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile('w',dir=path.parent,delete=False,encoding='utf-8') as output:
            temporary=Path(output.name)
            json.dump(value,output,ensure_ascii=False)
        try: temporary.replace(path)
        finally: temporary.unlink(missing_ok=True)
    print(json.dumps(value,ensure_ascii=False),flush=True)


def receiver_failure(error,operation,latest_attempt,failures):
    """Safe diagnostics without exception text, request headers or response bodies."""
    delay=min(60,5*2**max(0,min(failures-1,4)))
    value={'state':'receiver_unavailable','latest_attempt':latest_attempt,
           'operation':operation,'error_type':type(error).__name__,
           'retry_in_seconds':delay,
           'hint':'Проверьте локальный SSH-туннель, обновление приложения на VPS и настройку сборщика.'}
    if isinstance(error,httpx.HTTPStatusError):value['http_status']=error.response.status_code
    return value


async def wait_or_stop(stopped,seconds):
    try:await asyncio.wait_for(stopped.wait(),timeout=seconds)
    except asyncio.TimeoutError:pass


def feed_supported(payload):
    capabilities=payload.get('capabilities') if isinstance(payload,dict) else None
    return isinstance(capabilities,list) and 'statistics_feed_v1' in capabilities


async def ingest_statistics(client,snapshot):
    response=await client.post('/api/bookmakers/statistics/ingest',json=snapshot)
    if response.status_code==422 and snapshot.get('structured_feed') and snapshot.get('capture_kind')!='fast':
        # Preserve the detailed DOM capture if a source feed fails validation.
        LOG.warning('Structured statistics rejected; delivering the detailed source sections')
        fallback={key:value for key,value in snapshot.items() if key!='structured_feed'}
        response=await client.post('/api/bookmakers/statistics/ingest',json=fallback)
    response.raise_for_status()
    return response.json()


async def refresh_market(context,client,event):
    """Use the same public, feed-verified extraction as the regular crawler."""
    page=await context.new_page()
    feeds=[]
    def opened(socket):
        if not market_feed('winline',socket.url): return
        feed={'connected':True,'last_frame':0.0}; feeds.append(feed)
        socket.on('framereceived',lambda data: feed.update(last_frame=time.monotonic()))
        socket.on('close',lambda *args: feed.update(connected=False))
        socket.on('socketerror',lambda *args: feed.update(connected=False))
    page.on('websocket',opened)
    stage='navigation'
    try:
        response=await page.goto(event['url'],wait_until='domcontentloaded',timeout=45000)
        if response and response.status in (401,403,429): return 'blocked'
        await ensure_event_page(page,event['url'],event)
        stage='markets'
        snapshot=await read_marketbook(page)
        if not any(feed['connected'] and 0 <= time.monotonic()-feed['last_frame'] <= 180 for feed in feeds):
            LOG.warning('Event %s refresh: no current market feed',event['source_event_id'])
            return 'unavailable'
        captured_url,captured_at=page.url,datetime.now(timezone.utc).isoformat()
        if event_id_from_url(captured_url)!=event['source_event_id']: return 'unavailable'
        try: snapshot['statistics_links']=await discover_statistics_link(page,snapshot)
        except Exception: pass
        stage='receiver'
        document=market_document(snapshot,captured_url,captured_at,event)
        response=await client.post('/api/bookmakers/events/ingest',json=document)
        response.raise_for_status(); reply=response.json()
        if not reply.get('updated'): return 'partial' if reply.get('reason')=='incomplete_snapshot' else 'unavailable'
        quote=primary_quote(document)
        if quote:
            response=await client.post('/api/bookmakers/ingest',json=quote)
            response.raise_for_status()
        return 'ok' if document['coverage']['complete'] else 'partial'
    except Exception as error:
        LOG.warning('Event %s refresh failed at %s (%s)',event.get('source_event_id'),stage,type(error).__name__)
        try:
            body=(await page.locator('body').inner_text())[:1000]
            if any(marker in body.lower() for marker in ('access denied','verify you are human','проверка безопасности')): return 'blocked'
        except Exception: pass
        return 'unavailable'
    finally:
        await page.close()


async def refresh_job(context,client,job):
    event=job['event']; market_state='unavailable'; statistics_state='unavailable'
    try:
        market_state=await asyncio.wait_for(refresh_market(context,client,event),timeout=115)
    except Exception as error:
        LOG.warning('Event %s market refresh did not complete (%s)',event.get('source_event_id'),type(error).__name__)
    try:
        response=await client.get('/api/bookmakers/events/'+event['source_event_id']+'/statistics')
        response.raise_for_status(); detail=response.json()
        url=detail.get('statistics_url') if detail.get('link_status')=='found' else None
        if not url:
            statistics_state='missing_link'
        else:
            previous=(detail.get('statistics') or {}).get('latest_attempt') or {}
            blocked_until=None
            if detail.get('source_blocked_until'):
                try: blocked_until=datetime.fromisoformat(detail['source_blocked_until'])
                except ValueError: pass
            elif previous.get('state')=='blocked' and previous.get('at'):
                try: blocked_until=datetime.fromisoformat(previous['at'])+timedelta(hours=1)
                except ValueError: pass
            if blocked_until and blocked_until>datetime.now(timezone.utc):
                statistics_state='blocked'
            else:
                page=await context.new_page()
                try:
                    async def publish_fast(value):
                        await ingest_statistics(client,value)
                    enabled=feed_supported(detail)
                    snapshot=await collect_statistics(page,{**event,'statistics_url':url},on_fast=publish_fast if enabled else None,enable_feed=enabled)
                finally: await page.close()
                reply=await ingest_statistics(client,snapshot)
                statistics_state=snapshot['state'] if reply.get('updated') else 'unavailable'
    except Exception as error:
        LOG.warning('Event %s statistics refresh did not complete (%s)',event.get('source_event_id'),type(error).__name__)
        statistics_state='unavailable'
    response=await client.post('/api/bookmakers/refresh/finish',json={'job_id':job['job_id'],'lease_id':job['lease_id'],
                                'market_state':market_state,'statistics_state':statistics_state})
    response.raise_for_status()
    return {'source_event_id':event['source_event_id'],'market_state':market_state,'statistics_state':statistics_state}


async def statistics_request_job(context,client,job):
    """Fetch statistics only for an explicitly requested match."""
    event=job['event']; state='unavailable'
    async def fetch():
        nonlocal state
        prefix='/api/archive/events/' if event.get('source_kind')=='stream' else '/api/bookmakers/events/'
        endpoint=prefix+event['source_event_id']+'/statistics'
        response=await client.get(endpoint); response.raise_for_status(); detail=response.json()
        blocked_until=detail.get('source_blocked_until')
        if blocked_until:
            try:
                if datetime.fromisoformat(blocked_until)>datetime.now(timezone.utc):
                    state='blocked'; return
            except (ValueError,TypeError): pass
        try:
            await asyncio.wait_for(refresh_market(context,client,event),timeout=115)
        except Exception as error:
            LOG.warning('Popular markets for event %s were not refreshed (%s)',event['source_event_id'],type(error).__name__)
        url=detail.get('statistics_url') if detail.get('link_status')=='found' else None
        if not url:
            # A stream event may not yet have an observed statistics link.
            # Open only this event and use the normal verified market capture to find it.
            response=await client.get(endpoint); response.raise_for_status(); detail=response.json()
            url=detail.get('statistics_url') if detail.get('link_status')=='found' else None
        if not url:
            state='missing_link'; return
        page=await context.new_page()
        try:
            async def publish_fast(value):
                await ingest_statistics(client,value)
            enabled=feed_supported(detail)
            snapshot=await collect_statistics(page,{**event,'statistics_url':url},on_fast=publish_fast if enabled else None,enable_feed=enabled)
        finally:
            await page.close()
        reply=await ingest_statistics(client,snapshot)
        state=snapshot['state'] if reply.get('updated') else 'unavailable'
    try:
        await asyncio.wait_for(fetch(),timeout=240)
    except Exception as error:
        LOG.warning('Requested statistics for event %s did not complete (%s)',event.get('source_event_id'),type(error).__name__)
    response=await client.post('/api/bookmakers/refresh/finish',json={
        'kind':'statistics','job_id':job['job_id'],'lease_id':job['lease_id'],'statistics_state':state})
    response.raise_for_status()
    return {'source_event_id':event['source_event_id'],'statistics_state':state}


async def run(args):
    from playwright.async_api import async_playwright
    config=runtime_config(args)
    stopped=asyncio.Event()
    active_browser=[None]
    loop=asyncio.get_running_loop()
    def stop():
        stopped.set()
        if active_browser[0] and active_browser[0].is_connected():
            loop.create_task(active_browser[0].close())
    for code in (signal.SIGINT,signal.SIGTERM):
        try: loop.add_signal_handler(code,stop)
        except (NotImplementedError,RuntimeError): pass
    async with httpx.AsyncClient(base_url=config['api_url'].rstrip('/'),headers={'X-Matchlab-Collector-Key':config['collector_key']},timeout=30,trust_env=False) as client:
        async with async_playwright() as playwright:
            browser=await playwright.chromium.launch(headless=args.headless,args=['--disable-gpu'],env={key:value for key,value in os.environ.items() if key not in ('BOOKMAKER_INGEST_TOKEN','QWEN_API_KEY')})
            active_browser[0]=browser
            try:
                context=await browser.new_context(locale='ru-RU',timezone_id='Europe/Moscow')
                async def resources(route):
                    if route.request.resource_type in ('image','media','font'): await route.abort()
                    else: await route.continue_()
                await context.route('**/*',resources)
                latest_attempt=None
                last_idle=0.0
                receiver_failures=0
                while not stopped.is_set():
                    operation='statistics_request_claim'
                    try:
                        response=await client.post('/api/bookmakers/refresh/claim',json={'kind':'statistics'})
                        response.raise_for_status(); job=response.json().get('job')
                        if job:
                            operation='statistics_request_delivery'
                            result=await statistics_request_job(context,client,job)
                            latest_attempt=result; receiver_failures=0
                            report(args,{'state':'requested','mode':'on_demand','request':result})
                            if args.once: return 0 if result['statistics_state']=='ok' else 2
                            continue
                        operation='refresh_claim'
                        response=await client.post('/api/bookmakers/refresh/claim',json={})
                        response.raise_for_status(); job=response.json().get('job')
                        if job:
                            operation='refresh_delivery'
                            result=await refresh_job(context,client,job)
                            receiver_failures=0
                            report(args,{'state':'refresh','mode':'on_demand','refresh':result})
                            if args.once: return 0 if result['statistics_state']=='ok' else 2
                            continue
                    except (httpx.HTTPError,ValueError,KeyError) as error:
                        receiver_failures+=1
                        failure=receiver_failure(error,operation,latest_attempt,receiver_failures)
                        report(args,failure)
                        if args.once: return 2
                        # Claim polling runs before next_regular, so a failed
                        # receiver must pause here instead of immediately looping.
                        await wait_or_stop(stopped,failure['retry_in_seconds'])
                        continue
                    else:
                        receiver_failures=0
                        if not browser.is_connected():
                            report(args,{'state':'browser_closed','mode':'on_demand'}); return 2
                        if time.monotonic()-last_idle>=60:
                            report(args,{'state':'idle','mode':'on_demand','latest_attempt':latest_attempt})
                            last_idle=time.monotonic()
                    if args.once:return 0
                    if stopped.is_set(): break
                    await wait_or_stop(stopped,getattr(args,'poll_interval',2))
            finally:
                await browser.close()
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    connection=parser.add_mutually_exclusive_group()
    connection.add_argument('--config',type=Path)
    connection.add_argument('--api-url',help='Loopback HTTP API; key is read from BOOKMAKER_INGEST_TOKEN')
    parser.add_argument('--status-file',type=Path)
    parser.add_argument('--headless',action='store_true',help='Optional; first verify source access in a visible browser')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--interval',type=int,default=60)
    parser.add_argument('--batch-size',type=int,default=5)
    parser.add_argument('--poll-interval',type=int,default=2)
    args=parser.parse_args()
    if not 10<=args.interval<=3600 or not 1<=args.batch_size<=20 or not 1<=args.poll_interval<=10: parser.error('Некорректные лимиты сборщика.')
    os.umask(0o077)
    try: raise SystemExit(asyncio.run(run(args)))
    except (ValueError,OSError,json.JSONDecodeError): parser.exit(1,'Не удалось прочитать локальную настройку статистики. Содержимое ключа не выводится.\n')


if __name__=='__main__': main()
