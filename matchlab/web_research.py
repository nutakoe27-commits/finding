"""Bounded, optional Wigolo MCP research for a single match.

Search evidence is third-party text, never instructions or verified match data.
Only the configured loopback MCP service is contacted by this process.
"""
import argparse
import asyncio
import ipaddress
import json
import logging
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

_cache = OrderedDict()
_lock = threading.Lock()
_MAX_CACHE = 128
_log = logging.getLogger(__name__)


def _clean(value, limit):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()[:limit]


def _public_link(value):
    value = _clean(value, 1500)
    try:
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            return ""
        host = url.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        if url.port not in (None, 80, 443):
            return ""
        return value
    except ValueError:
        return ""


def _endpoint(value):
    url = urlsplit(value)
    if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1") or
            url.username or url.password or url.query or url.fragment or url.path != "/mcp" or
            not url.port):
        raise ValueError("WIGOLO_MCP_URL must be an HTTP loopback /mcp endpoint with an explicit port")
    return value


def _queries(event):
    home = _clean(event.get("home"), 100)
    away = _clean(event.get("away"), 100)
    sport = _clean(event.get("sport_label") or event.get("sport"), 60)
    competition = _clean(event.get("competition"), 100)
    if not home or not away or home.casefold() == away.casefold() or event.get("event_kind") == "outright":
        return []
    # Only source match metadata, never private notes, account data or credentials.
    return [f'{home} {away} {competition} {sport} новости матч',
            f'{home} {competition} {sport} новости травмы состав',
            f'{away} {competition} {sport} новости травмы состав']


async def _collect_batches(queries, search_one, deadline=18):
    """Keep successful results if another source times out."""
    tasks = [asyncio.create_task(search_one(query)) for query in queries]
    try:
        _, pending = await asyncio.wait(tasks, timeout=deadline)
        for task in pending:
            task.cancel()
        batches = await asyncio.gather(*tasks, return_exceptions=True)
        if all(isinstance(batch, BaseException) for batch in batches):
            if pending:
                raise TimeoutError('search deadline reached')
            raise next(batch for batch in batches if isinstance(batch, Exception))
        return batches
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


def _error_code(error):
    """Return an operator-safe category; never expose exception contents."""
    seen, queue = set(), [error]
    while queue:
        current = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, FileNotFoundError):
            return 'token_missing'
        if isinstance(current, PermissionError):
            return 'token_unreadable'
        if isinstance(current, httpx.HTTPStatusError) and current.response.status_code in (401, 403):
            return 'authentication_failed'
        if isinstance(current, (TimeoutError, httpx.TimeoutException)):
            return 'timeout'
        if isinstance(current, (httpx.ConnectError, ConnectionRefusedError)):
            return 'connection_failed'
        if isinstance(current, BaseExceptionGroup):
            queue.extend(current.exceptions)
        if current.__cause__ is not None:
            queue.append(current.__cause__)
        if current.__context__ is not None:
            queue.append(current.__context__)
    return 'provider_or_configuration_error'


def _extract(payload):
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise TypeError("unexpected search response")
    sources, seen = [], set()
    evidence = payload.get("evidence", [])
    evidence = evidence if isinstance(evidence, list) else []
    excerpts = {item.get("url"): item.get("excerpt") for item in evidence[:30] if isinstance(item, dict)}
    for row in rows[:30]:
        if not isinstance(row, dict):
            continue
        url = _public_link(row.get("url"))
        snippet = _clean(excerpts.get(row.get("url")) or row.get("excerpt") or row.get("snippet"), 1200)
        title = _clean(row.get("title"), 240)
        if not url or not snippet or url in seen:
            continue
        seen.add(url)
        freshness = row.get("freshness_signal")
        freshness = freshness if isinstance(freshness, dict) else {}
        published = _clean(freshness.get("published_date") or row.get("published_date"), 40)
        sources.append({"id": len(sources) + 1, "title": title or url, "url": url,
                        "excerpt": snippet, "published_at": published or None,
                        "date_inferred": bool(freshness.get("inferred"))})
        if len(sources) >= 8:
            break
    return sources


def _relevant_sources(sources, event):
    """Reject clearly unrelated hits without asserting that retained hits are true."""
    def normalize(value):
        return unicodedata.normalize('NFKC', unquote(_clean(value, 4000))).casefold()

    def matches(word, source_words):
        # Short prefixes (e.g. "Саут" in "Саутгемптон") are not team matches.
        return word in source_words or (len(word) >= 6 and any(
            candidate.startswith(word) and len(candidate) - len(word) <= 2 for candidate in source_words
        ))

    ignored = {'команда', 'football', 'футбол', 'фк', 'club', 'клуб', 'team', 'люб', 'любители',
               'россия', 'россии', 'russia'}
    participants = []
    for name in (event.get('home'), event.get('away')):
        full = normalize(name)
        words = {word for word in re.findall(r'[^\W_]+', re.sub(r'\([^)]*\)', ' ', full))
                 if len(word) >= 4 and word not in ignored}
        if words <= {'юнайтед', 'united', 'сити', 'city'}:
            words = set()
        qualified = any(marker in full for marker in ('люб.', 'любитель', 'amateur'))
        participants.append((words, qualified))
    competition = normalize(event.get('competition'))
    tournament_words = {word for word in re.findall(r'[^\W_]+', competition)
                        if len(word) >= 5 and word not in ignored}
    # Abbreviations are useful for identifying amateur competitions.
    if 'лфл' in competition:
        tournament_words.add('лфл')
    accepted = []
    for source in sources:
        text = normalize(' '.join(str(source.get(field) or '') for field in ('title', 'excerpt', 'url')))
        source_words = set(re.findall(r'[^\W_]+', text))
        for words, qualified in participants:
            if not words or not all(matches(word, source_words) for word in words):
                continue
            if qualified and not (any(marker in text for marker in ('люб.', 'любитель', 'amateur'))
                                  or any(word in text for word in tournament_words)):
                continue
            accepted.append({**source, 'id': len(accepted) + 1})
            break
    return accepted


async def _search(url, token_file, queries):
    # Lazy import keeps the site usable when the optional feature is disabled.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    token = Path(token_file).read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'[0-9a-f]{64}', token):
        raise ValueError('invalid MCP token')
    async with asyncio.timeout(25):
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), trust_env=False, follow_redirects=False,  # noqa: SIM117
                                     headers={'Authorization': f'Bearer {token}'}) as client:
            async with streamable_http_client(_endpoint(url), http_client=client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as session:
                    await session.initialize()
                    async def one(query):
                        result = await session.call_tool("search", {
                            "query": query, "category": "news", "time_range": "week",
                            "max_results": 4, "max_fetches": 0, "search_depth": "fast",
                            "max_tokens_out": 3000, "force_refresh": True,
                        })
                        if result.isError:
                            raise ValueError("search tool returned an error")
                        payload = result.structuredContent
                        if not isinstance(payload, dict):
                            parts = [item.text for item in result.content if getattr(item, "type", None) == "text"]
                            if not parts or sum(map(len, parts)) > 1_000_000:
                                raise ValueError("invalid search response")
                            payload = json.loads("\n".join(parts))
                        if payload.get('error'):
                            raise ValueError('search provider error')
                        return _extract(payload), bool(payload.get('warning') or payload.get('engine_warnings'))

                    batches = await _collect_batches(queries, one)
                    combined, seen = [], set()
                    partial = any(isinstance(batch, BaseException) or batch[1] for batch in batches)
                    for index in range(4):
                        for batch in batches:
                            if isinstance(batch, BaseException) or len(batch[0]) <= index:
                                continue
                            item = batch[0][index]
                            if item['url'] in seen:
                                continue
                            seen.add(item['url'])
                            item['id'] = len(combined) + 1
                            combined.append(item)
                            if len(combined) == 8:
                                return combined, partial
                    return combined, partial


def research(settings, event):
    if not settings.wigolo_mcp_url:
        return {"state": "disabled", "sources": []}
    queries = _queries(event)
    if not queries:
        return {"state": "not_applicable", "sources": []}
    key = (settings.wigolo_mcp_url, settings.wigolo_mcp_token_file, *queries)
    now = time.monotonic()
    with _lock:
        saved = _cache.get(key)
        if saved and saved[0] > now:
            _cache.move_to_end(key)
            return deepcopy(saved[1])
    try:
        sources, partial = asyncio.run(_search(settings.wigolo_mcp_url, settings.wigolo_mcp_token_file, queries))
        sources = _relevant_sources(sources, event)
        value = {"state": "ready" if sources else "empty", "searched_at": datetime.now(UTC).isoformat(),
                 "partial": partial, "sources": sources}
        ttl = 300 if sources else 60
    except Exception as exc:  # noqa: BLE001 - MCP and network failures must not block a match analysis.
        # Do not propagate service messages or third-party content into logs/API errors.
        code = _error_code(exc)
        _log.warning('Wigolo MCP unavailable: %s', code)
        value = {"state": "unavailable", "searched_at": datetime.now(UTC).isoformat(), "sources": [], "error_code": code}
        ttl = 30
    with _lock:
        _cache[key] = (time.monotonic() + ttl, deepcopy(value))
        _cache.move_to_end(key)
        while len(_cache) > _MAX_CACHE:
            _cache.popitem(last=False)
    return value


def main():
    import sys

    from .config import Settings
    parser = argparse.ArgumentParser(description='Проверить поиск Wigolo через MCP без вывода токена.')
    parser.add_argument('--home', required=True)
    parser.add_argument('--away', required=True)
    parser.add_argument('--sport', default='Футбол')
    parser.add_argument('--competition', default='')
    args = parser.parse_args()
    result = research(Settings.from_env(), {'home': args.home, 'away': args.away,
                     'sport_label': args.sport, 'competition': args.competition})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result['state'] != 'ready':
        sys.exit(1)


if __name__ == '__main__':
    main()
