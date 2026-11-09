#!/usr/bin/env python3
"""Inspect anonymous match-widget network metadata without printing response values."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

MAX_BODY = 1024 * 1024
MAX_RESPONSES = 80
PATH_WORDS = frozenset('winline www ru en match matches stats statistics sport sports api v1 v2 v3 graphql data events event results result scores score teams team players player standings table season seasons competitions competition leagues league fixtures fixture live report overview h2h form head-to-head'.split())
JSON_KEYS = frozenset('data result results response payload content props pageProps dehydratedState queries items edges node nodes page pageInfo total count status error errors message type id event eventId match matchId matches events fixture fixtures sport competition league season teams team participants home away homeTeam awayTeam players player score scores statistics stats standings table rows columns name title sections period periods rounds date time kickoffAt played won lost form games sets goals points'.split())
SENSITIVE = re.compile(r'token|secret|auth|password|passwd|cookie|session|email|phone|address|key|signature', re.I)


def widget_url(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(parsed.scheme == 'https' and parsed.hostname in ('winline.ru', 'www.winline.ru')
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                and re.fullmatch(r'/stats/match/\d{1,20}/?', parsed.path))


def endpoint(value: str) -> str | None:
    """Keep known public route words, redact all other path components and all query data."""
    parsed = urlsplit(value)
    host = parsed.hostname or ''
    allowed = host in ('winline.ru', 'www.winline.ru') or host.endswith('.yunai.tech') or host.endswith('.sportradar.com')
    if not allowed or parsed.scheme not in ('https', 'wss') or parsed.username or parsed.password:
        return None
    parts = []
    for segment in parsed.path.split('/'):
        if not segment: continue
        if segment.isdecimal(): parts.append('{id}')
        elif segment.lower() in PATH_WORDS: parts.append(segment.lower())
        else: parts.append('{other}')
    return host + '/' + '/'.join(parts[:20])


def shape(value) -> dict:
    """Report only a bounded whitelist of schema keys and structural types, never values."""
    def describe(obj, depth=0):
        if isinstance(obj, dict):
            names = [key for key in obj if isinstance(key, str) and key in JSON_KEYS and not SENSITIVE.search(key)]
            result = {'type': 'object', 'keys': sorted(names)[:30], 'other_keys': len(obj)-len(names)}
            if depth < 1:
                result['children'] = {key: describe(obj[key], depth+1) for key in sorted(names)[:8] if isinstance(obj[key], (dict, list))}
            return result
        if isinstance(obj, list):
            result = {'type': 'array', 'count': len(obj)}
            if obj and depth < 2: result['first'] = describe(obj[0], depth+1)
            return result
        return {'type': 'null' if obj is None else 'boolean' if isinstance(obj, bool) else 'number' if isinstance(obj, (float, int)) else 'string' if isinstance(obj, str) else 'other'}
    return describe(value)


async def inspect_page(page, url: str, seconds: int) -> dict:
    responses = []
    sockets = []
    def on_response(response):
        safe = endpoint(response.url)
        if safe and response.request.resource_type in ('fetch', 'xhr', 'document') and len(responses) < MAX_RESPONSES:
            responses.append((response, safe))
    def on_socket(socket):
        safe = endpoint(socket.url)
        if safe and safe not in sockets and len(sockets) < 20: sockets.append(safe)
    page.on('response', on_response)
    page.on('websocket', on_socket)
    document = await page.goto(url, wait_until='domcontentloaded', timeout=45000)
    await asyncio.sleep(seconds)
    frames = []
    for frame in page.frames:
        host = urlsplit(frame.url).hostname or ''
        if host not in ('statshub.yunai.tech', 'statshub.sportradar.com'):
            continue
        item = {'endpoint': endpoint(frame.url)}
        try:
            info = await asyncio.wait_for(frame.evaluate('''() => ({
                header: !!document.querySelector('[data-testid="match-header"]'),
                headings: [...document.querySelectorAll('h3')].filter(e => e.getClientRects().length).length,
                next_data: !!window.__NEXT_DATA__, nuxt_data: !!window.__NUXT__,
                json_scripts: [...document.querySelectorAll('script[type="application/json"]')].length,
                next_stream_scripts: [...document.querySelectorAll('script:not([src])')].filter(s => (s.textContent || '').includes('__next_f.push')).length,
                next_keys: window.__NEXT_DATA__ && typeof window.__NEXT_DATA__ === 'object' ? Object.keys(window.__NEXT_DATA__).slice(0,40) : []
            })'''), timeout=5)
            keys = [key for key in info.pop('next_keys') if key in JSON_KEYS and not SENSITIVE.search(key)]
            item.update(info, next_data_keys=keys)
        except Exception as error:
            item['error'] = type(error).__name__
        frames.append(item)
    entries = []
    for response, safe in responses:
        item = {'endpoint': safe, 'status': response.status, 'resource_type': response.request.resource_type}
        try:
            content_type = (await response.header_value('content-type') or '').split(';', 1)[0].lower()
            item['content_type'] = content_type if re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+', content_type) else 'unknown'
            length = await response.header_value('content-length')
            if 'json' in content_type and response.request.resource_type in ('fetch', 'xhr'):
                if not length or not length.isdecimal() or int(length) > MAX_BODY:
                    item['shape'] = 'skipped_unknown_or_large_size'
                else:
                    body = await asyncio.wait_for(response.body(), timeout=3)
                    if len(body) <= MAX_BODY:
                        item['shape'] = shape(json.loads(body))
                    else:
                        item['shape'] = 'skipped_large_size'
        except Exception as error:
            item['inspection_error'] = type(error).__name__
        entries.append(item)
    return {'page_status': document.status if document else None, 'frames': frames,
            'responses': entries, 'websockets': sockets, 'responses_limit': MAX_RESPONSES}


async def run(url: str, seconds: int) -> dict:
    # Never pass local service secrets to a browser launched for diagnostics.
    environment = {key: value for key, value in os.environ.items() if key not in ('BOOKMAKER_INGEST_TOKEN', 'QWEN_API_KEY')}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False, args=['--disable-gpu'], env=environment)
        try:
            context = await browser.new_context(locale='ru-RU', timezone_id='Europe/Moscow')
            async def resources(route):
                if route.request.resource_type in ('image', 'media', 'font'): await route.abort()
                else: await route.continue_()
            await context.route('**/*', resources)
            page = await context.new_page()
            return await inspect_page(page, url, seconds)
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Public Winline statistics link')
    parser.add_argument('--seconds', type=int, default=20)
    args = parser.parse_args()
    if not widget_url(args.url): parser.error('Нужна ссылка https://winline.ru/stats/match/ID без параметров.')
    if not 1 <= args.seconds <= 40: parser.error('Ожидание должно быть от 1 до 40 секунд.')
    os.umask(0o077)
    try:
        print(json.dumps(asyncio.run(run(args.url, args.seconds)), ensure_ascii=False, indent=2), flush=True)
    except Exception as error:
        print(json.dumps({'error': type(error).__name__}), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
