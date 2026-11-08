"""Lightweight browser collector for public team statistics on an accessible device."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import time
from urllib.parse import urlsplit

import httpx

from .statistics import collect_statistics


def read_config(path):
    if path.is_symlink(): raise ValueError('Файл настройки не должен быть ссылкой.')
    config=json.loads(path.read_text())
    url=urlsplit(config.get('api_url',''))
    key=config.get('collector_key','')
    if config.get('format')!='matchlab.statistics-client.v1' or url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost','::1') or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
        raise ValueError('Нужен локальный API через существующий SSH-туннель.')
    if not isinstance(key,str) or not 32<=len(key)<=500 or any(ord(char)<32 for char in key):
        raise ValueError('Ключ сборщика отсутствует в локальной настройке.')
    if path.stat().st_mode&0o077: raise ValueError('Права настройки должны быть 600: chmod 600 '+str(path))
    return config


async def run(args):
    from playwright.async_api import async_playwright
    config=read_config(args.config)
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
                page=await context.new_page()
                while not stopped.is_set():
                    started=time.monotonic(); states={}; linked=0
                    try:
                        response=await client.get('/api/bookmakers/statistics/queue',params={'limit':args.batch_size})
                        response.raise_for_status(); queue=response.json(); linked=queue.get('linked_events',0)
                        for job in queue['jobs']:
                            if stopped.is_set(): break
                            if page.is_closed(): page=await context.new_page()
                            snapshot=await collect_statistics(page,job)
                            if stopped.is_set(): break
                            if not browser.is_connected():
                                print('Окно сборщика закрыто. Запустите сборщик снова.',flush=True)
                                return 2
                            response=await client.post('/api/bookmakers/statistics/ingest',json=snapshot)
                            response.raise_for_status()
                            states[snapshot['state']]=states.get(snapshot['state'],0)+1
                    except (httpx.HTTPError,ValueError,KeyError):
                        print(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'state':'receiver_unavailable','hint':'Проверьте локальный SSH-туннель, обновление приложения на VPS и локальный файл настройки.'},ensure_ascii=False),flush=True)
                        if args.once: return 2
                    print(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'state':'cycle_finished','linked_events':linked,'statistics':states,'cycle_seconds':round(time.monotonic()-started,2)},ensure_ascii=False),flush=True)
                    if args.once: return 0 if states.get('ok') else 2
                    remaining=max(0,args.interval-(time.monotonic()-started))
                    if remaining:
                        try: await asyncio.wait_for(stopped.wait(),timeout=remaining)
                        except asyncio.TimeoutError: pass
            finally:
                await browser.close()
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('.local/statistics-client.json'))
    parser.add_argument('--headless',action='store_true',help='Optional; first verify source access in a visible browser')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--interval',type=int,default=60)
    parser.add_argument('--batch-size',type=int,default=5)
    args=parser.parse_args()
    if not 10<=args.interval<=3600 or not 1<=args.batch_size<=20: parser.error('Некорректные лимиты сборщика.')
    os.umask(0o077)
    try: raise SystemExit(asyncio.run(run(args)))
    except (ValueError,OSError,json.JSONDecodeError): parser.exit(1,'Не удалось прочитать локальную настройку статистики. Содержимое ключа не выводится.\n')


if __name__=='__main__': main()
