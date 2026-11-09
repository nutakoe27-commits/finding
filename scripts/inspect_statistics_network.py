#!/usr/bin/env python3
"""Inspect anonymous match-widget network metadata without printing response values."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

MAX_BODY = 1024 * 1024
MAX_RESPONSES = 80
PATH_WORDS = frozenset('winline www ru en match matches stats statistics sport sports api v1 v2 v3 graphql data events event results result scores score teams team players player standings table season seasons competitions competition leagues league fixtures fixture live report overview h2h form head-to-head'.split())
JSON_KEYS = frozenset('doc _doc _maxage code data result results response payload content props pageProps dehydratedState queries items edges node nodes page pageInfo total count status error errors message type id event eventId match matchId matches events fixture fixtures sport competition league season teams team participants home away homeTeam awayTeam players player score scores statistics stats standings table rows columns name title sections period periods rounds date time kickoffAt played won lost form games sets goals points'.split())
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
    remaining = [30]
    def describe(obj, depth=0):
        remaining[0]-=1
        if isinstance(obj, dict):
            names = [key for key in obj if isinstance(key, str) and key in JSON_KEYS and not SENSITIVE.search(key)]
            result = {'type': 'object', 'keys': sorted(names)[:30], 'other_keys': len(obj)-len(names)}
            if depth < 5 and remaining[0] > 0:
                result['children'] = {key: describe(obj[key], depth+1) for key in sorted(names)[:8] if isinstance(obj[key], (dict, list)) and remaining[0] > 0}
            return result
        if isinstance(obj, list):
            result = {'type': 'array', 'count': len(obj)}
            if obj and depth < 5 and remaining[0] > 0: result['first'] = describe(obj[0], depth+1)
            return result
        return {'type': 'null' if obj is None else 'boolean' if isinstance(obj, bool) else 'number' if isinstance(obj, (float, int)) else 'string' if isinstance(obj, str) else 'other'}
    return describe(value)


def recent_matches(value):
    if not isinstance(value, dict) or not isinstance(value.get('doc'), list): return None
    for item in value['doc']:
        data=item.get('data') if isinstance(item,dict) else None
        if isinstance(data,dict) and isinstance(data.get('team'),dict) and isinstance(data.get('matches'),list):
            return data
    return None


def participant_key(value):
    if not isinstance(value,str): return ''
    return ''.join(char for char in unicodedata.normalize('NFKC',value).casefold() if char.isalnum())


def public_label(value, depth=0):
    """Only short public match/team labels; never URLs, addresses or secrets."""
    if isinstance(value,str):
        value=' '.join(value.split())
        if (0<len(value)<=100 and not SENSITIVE.search(value) and
            not re.search(r'https?://|www\.|@|[?=&]|\d{13,}',value,re.I) and
            all(char.isprintable() for char in value)):
            return value
    if isinstance(value,dict) and depth<2:
        return {key:label for key in ('ru','en','name','short','full','default')
                if (label:=public_label(value.get(key),depth+1)) is not None}
    return None


def passive_details(documents, header, expected_id):
    lines=[]
    for line in header.splitlines():
        value=public_label(line)
        if value: lines.append(value)
        if len(lines)>=20: break
    matches=[]; teams=[]; header_key=participant_key(header)
    for _,data in documents:
        match=data.get('match') if isinstance(data.get('match'),dict) else data
        members=match.get('teams') if isinstance(match,dict) else None
        if isinstance(members,dict) and isinstance(members.get('home'),dict) and isinstance(members.get('away'),dict):
            home,away=members['home'],members['away']
            names=[public_label(home.get('name')),public_label(away.get('name'))]
            identifiers=[]
            for key in ('id','_id','matchId','match_id','eventId','event_id'):
                value=match.get(key)
                if isinstance(value,(str,int)) and str(value).rsplit(':',1)[-1]==expected_id: identifiers.append(key)
            matches.append({'home_name':names[0],'away_name':names[1],
                            'home_name_type':type(home.get('name')).__name__,
                            'away_name_type':type(away.get('name')).__name__,
                            'home_name_in_header':bool(participant_key(home.get('name')) and participant_key(home.get('name')) in header_key),
                            'away_name_in_header':bool(participant_key(away.get('name')) and participant_key(away.get('name')) in header_key),
                            'matching_id_fields':identifiers})
        team=data.get('team')
        if isinstance(team,dict) and isinstance(data.get('matches'),list):
            teams.append({'name':public_label(team.get('name')),'name_type':type(team.get('name')).__name__,
                          'match_count':len(data['matches'])})
    return {'header_lines':lines,'match_candidates':matches[:5],'team_match_lists':teams[:6]}


def passive_summary(documents, header):
    """Count already received public documents, binding team results to the displayed match."""
    header_key=participant_key(header)
    target=None
    for elapsed,data in documents:
        match=data.get('match') if isinstance(data.get('match'),dict) else data
        teams=match.get('teams') if isinstance(match,dict) else None
        if not isinstance(teams,dict): continue
        home,away=teams.get('home'),teams.get('away')
        if not isinstance(home,dict) or not isinstance(away,dict): continue
        home_key,away_key=participant_key(home.get('name')),participant_key(away.get('name'))
        if min(len(home_key),len(away_key))<3 or home_key==away_key: continue
        if home_key in header_key and away_key in header_key:
            target=(home_key,away_key,elapsed)
            break
    result={'documents':len(documents),'match_bound_to_header':bool(target),
            'recent_match_counts':{},'team_stats_found':{},'standings_candidates':0,
            'player_list_candidates':0,'seconds_until_matched_team_lists':None}
    if not target: return result
    home_key,away_key,match_at=target
    timestamps=[match_at]
    for elapsed,data in documents:
        team=data.get('team')
        team_key=participant_key(team.get('name')) if isinstance(team,dict) else ''
        side='home' if team_key==home_key else 'away' if team_key==away_key else None
        if side and isinstance(data.get('matches'),list):
            if side not in result['recent_match_counts']:
                result['recent_match_counts'][side]=len(data['matches'])
                timestamps.append(elapsed)
        if side and isinstance(data.get('stats'),dict): result['team_stats_found'][side]=True
        if isinstance(data.get('season'),dict) and isinstance(data.get('teams'),list): result['standings_candidates']+=1
        if isinstance(data.get('players'),list) and isinstance(data.get('season'),dict): result['player_list_candidates']+=1
    if set(result['recent_match_counts'])=={'home','away'}:
        result['seconds_until_matched_team_lists']=round(max(timestamps),2)
    return result


def compare_response(value, status: int, original: dict) -> dict:
    if isinstance(value,dict) and isinstance(value.get('doc'),list):
        for item in value['doc']:
            if not isinstance(item,dict): continue
            data=item.get('data') if isinstance(item.get('data'),dict) else {}
            if item.get('event')=='exception' or data.get('_doc')=='exception':
                result={'state':'provider_error','http_status':status}
                code=data.get('code')
                if type(code) is int and 400<=code<=599: result['provider_code']=code
                return result
    result=recent_matches(value)
    if result is None: return {'state':'different_response','http_status':status,'schema':shape(value)}
    return {'state':'ok','http_status':status,'match_count':len(result['matches']),
            'same_team':result['team']==original['team'],
            'same_matches':result['matches']==original['matches']}


def direct_request(url: str, original: dict) -> dict:
    """One plain GET to the observed provider URL; don't print URL, headers or body."""
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname!='sh.fn.yunai.tech' or parsed.username or parsed.password:
        return {'state':'invalid_source'}
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl): return None
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        request=urllib.request.Request(url,headers={'Accept':'application/json'},method='GET')
        with opener.open(request,timeout=15) as response:
            status=response.status
            body=response.read(MAX_BODY+1)
        if len(body)>MAX_BODY: return {'state':'too_large','http_status':status}
        return compare_response(json.loads(body),status,original)
    except urllib.error.HTTPError as error:
        return {'state':'http_error','http_status':error.code}
    except (ValueError,UnicodeError):
        return {'state':'invalid_json'}
    except Exception as error:
        return {'state':'request_error','error_type':type(error).__name__}


async def session_request(context, url: str, original: dict) -> dict:
    """Try the same observed GET using only the temporary browser's session."""
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname!='sh.fn.yunai.tech' or parsed.username or parsed.password:
        return {'state':'invalid_source'}
    response=None
    try:
        response=await context.request.get(url,max_redirects=0,timeout=15000)
        if response.status in (401,403,429): return {'state':'http_error','http_status':response.status}
        length=response.headers.get('content-length','')
        if length.isdecimal() and int(length)>MAX_BODY: return {'state':'too_large','http_status':response.status}
        body=await response.body()
        if len(body)>MAX_BODY: return {'state':'too_large','http_status':response.status}
        return compare_response(json.loads(body),response.status,original)
    except (ValueError,UnicodeError):
        return {'state':'invalid_json'}
    except Exception as error:
        return {'state':'request_error','error_type':type(error).__name__}
    finally:
        if response is not None: await response.dispose()


async def inspect_page(page, url: str, seconds: int, *, provider_only=False, test_direct=False, test_passive=False, show_labels=False) -> dict:
    responses = []
    sockets = []
    started=time.monotonic()
    def on_response(response):
        safe = endpoint(response.url)
        host = urlsplit(response.url).hostname
        if safe and (not provider_only or host == 'sh.fn.yunai.tech') and response.request.resource_type in ('fetch', 'xhr', 'document') and len(responses) < MAX_RESPONSES:
            responses.append((response, safe, time.monotonic()-started))
    def on_socket(socket):
        safe = endpoint(socket.url)
        if safe and safe not in sockets and len(sockets) < 20: sockets.append(safe)
    page.on('response', on_response)
    page.on('websocket', on_socket)
    document = await page.goto(url, wait_until='domcontentloaded', timeout=45000)
    await asyncio.sleep(seconds)
    frames = []
    header=''
    expected_id = urlsplit(url).path.strip('/').split('/')[-1]
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
            frame_url=urlsplit(frame.url)
            if test_passive and re.fullmatch(rf'/winline/(?:ru|en)/match/{re.escape(expected_id)}/?',frame_url.path):
                header=await frame.locator('[data-testid="match-header"]').inner_text(timeout=3000)
        except Exception as error:
            item['error'] = type(error).__name__
        frames.append(item)
    entries = []
    candidate = None
    passive_documents=[]
    for response, safe, elapsed in responses:
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
                        value=json.loads(body)
                        item['shape'] = shape(value)
                        if test_passive and response.status==200 and urlsplit(response.url).hostname=='sh.fn.yunai.tech':
                            frame=urlsplit(response.request.frame.url)
                            expected_path=rf'/winline/(?:ru|en)/match/{re.escape(expected_id)}/?'
                            matched=frame.scheme=='https' and frame.hostname in ('statshub.yunai.tech','statshub.sportradar.com') and re.fullmatch(expected_path,frame.path)
                            if matched and isinstance(value,dict) and isinstance(value.get('doc'),list):
                                for entry in value['doc'][:20]:
                                    data=entry.get('data') if isinstance(entry,dict) else None
                                    if isinstance(data,dict) and entry.get('event')!='exception' and data.get('_doc')!='exception':
                                        passive_documents.append((elapsed,data))
                        if test_direct and candidate is None and response.status==200 and response.request.method=='GET' and urlsplit(response.url).hostname=='sh.fn.yunai.tech':
                            frame=urlsplit(response.request.frame.url)
                            expected_path=rf'/winline/(?:ru|en)/match/{re.escape(expected_id)}/?'
                            matched=frame.scheme=='https' and frame.hostname in ('statshub.yunai.tech','statshub.sportradar.com') and re.fullmatch(expected_path,frame.path)
                            recent=recent_matches(value)
                            if matched and recent is not None:
                                candidate=(response.url,recent)
                    else:
                        item['shape'] = 'skipped_large_size'
        except Exception as error:
            item['inspection_error'] = type(error).__name__
        entries.append(item)
    if provider_only:
        grouped = {}
        for item in entries:
            identity = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if identity not in grouped: grouped[identity] = {**item, 'count': 0}
            grouped[identity]['count'] += 1
        entries = list(grouped.values())[:30]
    result={'page_status': document.status if document else None, 'frames': frames,
            'responses': entries, 'websockets': sockets, 'responses_limit': MAX_RESPONSES}
    if test_passive:
        result['passive_test']=passive_summary(passive_documents,header)
        if show_labels: result['passive_details']=passive_details(passive_documents,header,expected_id)
    if test_direct:
        if candidate is None:
            result['direct_test']={'state':'candidate_not_found'}
        else:
            direct=await asyncio.to_thread(direct_request,*candidate)
            result['direct_test']={'category':'recent_matches','browser_match_count':len(candidate[1]['matches']),
                                   **direct}
            denied=direct.get('http_status') in (401,403,429) or direct.get('provider_code') in (401,403,429)
            result['session_test']={'state':'skipped_source_denied'} if denied else await session_request(page.context,*candidate)
    return result


async def run(url: str, seconds: int, *, provider_only=False, test_direct=False, test_passive=False, show_labels=False) -> dict:
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
            return await inspect_page(page, url, seconds, provider_only=provider_only, test_direct=test_direct, test_passive=test_passive, show_labels=show_labels)
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Public Winline statistics link')
    parser.add_argument('--seconds', type=int, default=20)
    parser.add_argument('--provider-only', action='store_true', help='Summarize provider responses only')
    parser.add_argument('--test-direct', action='store_true', help='Repeat one observed public request without browser session')
    parser.add_argument('--test-passive', action='store_true', help='Measure already received data only; no replay requests')
    parser.add_argument('--show-match-labels', action='store_true', help='Show public match header and team labels')
    args = parser.parse_args()
    if not widget_url(args.url): parser.error('Нужна ссылка https://winline.ru/stats/match/ID без параметров.')
    if not 1 <= args.seconds <= 40: parser.error('Ожидание должно быть от 1 до 40 секунд.')
    if args.test_direct and args.test_passive: parser.error('Выберите только один режим проверки.')
    if args.show_match_labels and not args.test_passive: parser.error('Для имён команд укажите --test-passive.')
    os.umask(0o077)
    try:
        result=asyncio.run(run(args.url, args.seconds, provider_only=args.provider_only, test_direct=args.test_direct, test_passive=args.test_passive, show_labels=args.show_match_labels))
        if args.test_direct: result={key:result[key] for key in ('page_status','frames','direct_test','session_test') if key in result}
        if args.test_passive: result={key:result[key] for key in ('page_status','frames','passive_test','passive_details') if key in result}
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    except Exception as error:
        print(json.dumps({'error': type(error).__name__}), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
