"""Scrapy/Playwright sports inventory and per-event market collection."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import scrapy
from scrapy import signals
from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import IgnoreRequest
from scrapy_playwright.page import PageMethod

from .bookmakers import lines
from .capture import DOM_SCRIPT
from .markets import validate_event_markets
from .server_scraper import Publisher, local_api
from .winline_browser import LINE_URL, event_id_from_url, select_public_line
from .winline_markets import ensure_event_page, market_document, primary_quote, read_marketbook, discover_statistics_link
from .worker_protocol import market_feed
from .sports import ordinary_sport, sport_from_url, sport_label, EXCLUDED


LISTING_SCRIPT = r"""() => {
    const anchors = [...document.querySelectorAll('a[href*="/sport/"],a[href*="/event/"]')]
        .filter(el => el.getClientRects().length && !el.closest('form'));
    return {truncated: anchors.length > 15000, nodes: anchors.slice(0,15000).map(el => {
        const tournament = el.closest('.block-tournament');
        const heading = tournament && (tournament.querySelector('h1,h2,h3,.tournament__title,.block-tournament__title') || tournament);
        return {attrs:{href:el.href},text:(el.innerText || '').trim().slice(0,1200),
                competition: heading ? (heading.innerText || '').split('\n').map(s=>s.trim()).find(Boolean) : null};
    })};
}"""


def selected_sports(value: str) -> set[str] | None:
    if value == 'all':
        return None
    keys = value.split(',')
    if not keys or any(not ordinary_sport(key) for key in keys):
        raise ValueError('Укажите all или виды спорта через запятую: football,hockey,tennis,basketball')
    return set(keys)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def abort_heavy(request):
    return request.resource_type in ('image', 'media', 'font')


async def init_page(page, request):
    state = request.meta.setdefault('market_feed', [])

    def opened(socket):
        if not market_feed('winline', socket.url):
            return
        feed = {'connected': True, 'last_frame': 0.0}
        state.append(feed)
        socket.on('framereceived', lambda data: feed.update(last_frame=time.monotonic()))
        socket.on('close', lambda *args: feed.update(connected=False))
        socket.on('socketerror', lambda *args: feed.update(connected=False))

    page.on('websocket', opened)

def listing_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.hostname not in ('winline.ru', 'www.winline.ru') or event_id_from_url(value):
        return None
    if not sport_from_url(value):
        return None
    # Route graph comes from real anchors; never enumerate guessed event IDs.
    return urlunsplit(('https', 'winline.ru', parsed.path, '', ''))


def event_candidates(payload: dict) -> list[dict]:
    result = {}
    for node in payload.get('nodes', []):
        url = node.get('attrs', {}).get('href', '')
        event_id = event_id_from_url(url)
        teams = lines(node.get('text', ''))
        if event_id and teams and len(' '.join(teams)) <= 300:
            event = {'source_event_id': event_id, 'url': url.split('?', 1)[0]}
            if len(teams) == 2:
                event.update(home=teams[0], away=teams[1])
            else:
                event['title'] = ' '.join(teams)
            if node.get('competition'):
                event['competition'] = node['competition']
            result[event_id] = event
    return list(result.values())


class MarketPipeline:
    @classmethod
    def from_crawler(cls, crawler):
        pipeline = cls()
        pipeline.crawler = crawler
        return pipeline

    async def process_item(self, item):
        document = validate_event_markets(dict(item))
        spider = self.crawler.spider
        folder = spider.state_dir / 'events'
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (document['source_event_id'] + ('.json.gz' if document['coverage']['complete'] else '.partial.json.gz'))
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as output:
            temporary = Path(output.name)
            output.write(gzip.compress(json.dumps(document, ensure_ascii=False).encode()))
        try:
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        self.crawler.stats.inc_value('markets/saved_events')
        spider.sport_counts.setdefault(document['sport'], {'saved':0, 'complete':0, 'failed':0})['saved'] += 1
        if document['coverage']['complete']:
            self.crawler.stats.inc_value('markets/complete_events')
            spider.sport_counts[document['sport']]['complete'] += 1
        if spider.publisher:
            try:
                reply = await spider.publisher.request('POST', '/api/bookmakers/events/ingest', document)
                if reply.get('updated') is True:
                    self.crawler.stats.inc_value('markets/published_events')
                    if document['coverage']['complete']:
                        self.crawler.stats.inc_value('markets/published_complete_events')
                    quote = primary_quote(document)
                    if quote:
                        await spider.publisher.request('POST', '/api/bookmakers/ingest', quote)
                        self.crawler.stats.inc_value('markets/primary_quotes')
                else:
                    self.crawler.stats.inc_value('markets/receiver_preserved_previous')
            except ValueError:
                self.crawler.stats.inc_value('markets/publish_failed')
                spider.logger.error('Receiver rejected event %s; the validated document remains in the local cache', document['source_event_id'])
        if spider.check and document['coverage']['complete'] and (not spider.publisher or self.crawler.stats.get_value('markets/published_complete_events', 0)):
            spider.request_stop('verified_complete_event')
        if spider.stopping:
            return document
        if time.monotonic() - spider.last_status > 60:
            await spider.publish_status('partial')
        return document


class WinlineSpider(scrapy.Spider):
    name = 'winline_markets'
    allowed_domains = ['winline.ru']

    @classmethod
    def from_crawler(cls,crawler,*args,**kwargs):
        spider=super().from_crawler(crawler,*args,**kwargs)
        crawler.signals.connect(spider.collection_opened,signal=signals.spider_opened)
        crawler.signals.connect(spider.collection_closed,signal=signals.spider_closed)
        return spider

    def __init__(self, *, state_dir, api_url, no_publish=False, seed=None, max_events=5000,
                 max_listings=200, max_scrolls=200, check=False, interval=60, sports='all', **kwargs):
        super().__init__(**kwargs)
        self.state_dir = Path(state_dir)
        self.publisher = None if no_publish else Publisher(api_url, os.getenv('BOOKMAKER_INGEST_TOKEN', ''))
        self.seed = seed
        self.check = check
        self.interval = interval
        self.max_events, self.max_listings, self.max_scrolls = max_events, max_listings, max_scrolls
        self.events, self.listings = set(), set()
        self.last_status = 0.0
        self.sports = selected_sports(sports)
        self.sport_counts = {}
        self.sport_labels = {}
        self.sport_queued = {}
        self.stopping = False
        self.active_pages = set()
        self.close_task = None
        self.close_timer = None

    def collection_opened(self):
        timeout=self.crawler.settings.getfloat('MARKET_CYCLE_TIMEOUT',1800)
        self.close_timer=asyncio.get_running_loop().call_later(timeout,self.request_stop,'cycle_timeout')

    def collection_closed(self):
        if self.close_timer: self.close_timer.cancel()

    async def prepare_page(self, page, request):
        self.active_pages.add(page)
        def closed(): self.active_pages.discard(page)
        page.on('close',closed)
        if self.stopping:
            await page.close()
            raise IgnoreRequest('Collector is stopping')
        await init_page(page,request)

    def request_stop(self, reason):
        if self.stopping:
            return
        self.stopping = True
        # Run after the acknowledged item's pipeline has returned. Waiting
        # for the engine from inside that pipeline would wait for ourselves.
        self.close_task = asyncio.create_task(self.close_collection(reason))

    async def close_collection(self, reason):
        closing = asyncio.create_task(self.crawler.engine.close_spider_async(reason=reason,mode='fast'))
        # Fast engine shutdown drops downloads, but active browser callbacks
        # can still wait for navigation/DOM/scrolling. Close only our pages.
        await asyncio.gather(*(asyncio.wait_for(page.close(),timeout=5)
                               for page in tuple(self.active_pages) if not page.is_closed()),return_exceptions=True)
        await closing

    async def publish_status(self, state):
        self.last_status = time.monotonic()
        if not self.publisher:
            return
        stats = self.crawler.stats
        saved = stats.get_value('markets/published_events', 0)
        progress = {'discovered': len(self.events), 'saved': saved,
                    'skipped': stats.get_value('markets/skipped_other_sports', 0),
                    'failed': sum(stats.get_value('markets/' + key, 0) for key in ('failed_events', 'request_failures', 'publish_failed'))}
        source_state = 'starting' if state == 'starting' else 'ok' if saved else 'error'
        body = {'state': state, 'interval_seconds': self.interval, 'sources': {'winline': {'state': source_state, 'events': saved}},
                'mode': 'event_markets', 'progress': progress}
        try:
            await self.publisher.request('POST', '/api/bookmakers/worker-status', body)
        except ValueError:
            self.logger.error('Collector progress could not reach the local receiver')

    def request(self, url, callback, **kwargs):
        priority = kwargs.pop('priority', 0)
        return scrapy.Request(url, callback=callback, errback=self.failed,
                              priority=priority,
                              meta={'playwright': True, 'playwright_include_page': True,
                                    'playwright_page_init_callback': self.prepare_page, **kwargs})

    async def start(self):
        if self.publisher:
            try:
                capabilities = await self.publisher.request('GET', '/api/bookmakers/events?limit=1')
                if (self.sports is None or self.sports - {'football'}) and not isinstance(capabilities.get('sports'), list):
                    raise ValueError('Обновите приложение на VPS: приёмник ещё не поддерживает несколько видов спорта')
            except ValueError as exc:
                self.crawler.stats.inc_value('markets/receiver_unavailable')
                self.logger.error('Receiver: %s', str(exc))
                return
        await self.publish_status('starting')
        if self.seed:
            self.events.add(event_id_from_url(self.seed))
            yield self.request(self.seed, self.parse_event, event_metadata={})
            return
        self.listings.update((LINE_URL, 'https://winline.ru/'))
        yield self.request(LINE_URL, self.parse_listing,
                           playwright_page_methods=[PageMethod(select_public_line)], listing_depth=0)
        # Both entrypoints contribute actual current links. No sport URLs or
        # numeric event IDs are constructed speculatively.
        yield self.request('https://winline.ru/', self.parse_listing)

    async def parse_listing(self, response):
        page = response.meta['playwright_page']
        self.active_pages.add(page)
        candidates, links, stable, previous, bottom = {}, set(), 0, None, False
        try:
            if self.stopping: return
            selector = 'a[href*="/event/"],.block-tournament a[href]' if sport_from_url(response.request.url) else 'a[href*="/sport/"]'
            try:
                await page.wait_for_selector(selector, timeout=20000)
            except Exception:
                text = (await page.locator('body').inner_text())[:20000]
                if not re.search(r'нет событий|нет доступных|ничего не найдено|событий пока нет', text, re.I):
                    self.crawler.stats.inc_value('markets/listing_not_ready')
            for _ in range(self.max_scrolls):
                if self.stopping: return
                payload = await page.evaluate(LISTING_SCRIPT)
                if payload.get('truncated'):
                    self.crawler.stats.inc_value('markets/bounds_reached')
                for event in event_candidates(payload):
                    sport = sport_from_url(event['url']) or sport_from_url(response.request.url)
                    if sport and self.sports is not None and sport not in self.sports:
                        continue
                    if sport:
                        event.update(sport=sport, sport_label=self.sport_labels.get(sport, sport_label(sport)))
                    candidates[event['source_event_id']] = event
                for node in payload.get('nodes', []):
                    target = listing_url(node.get('attrs', {}).get('href', ''))
                    sport = sport_from_url(target) if target else None
                    if target and (self.sports is None or sport in self.sports):
                        if urlsplit(target).path.rstrip('/').count('/') == 3 and EXCLUDED.search(node.get('text','')):
                            continue
                        if urlsplit(target).path.rstrip('/').count('/') == 3:
                            self.sport_labels[sport] = sport_label(sport, node.get('text'))
                        links.add(target)
                count = len(candidates)
                stable = stable + 1 if count == previous else 0
                previous = count
                bottom = await page.evaluate("""() => {
                    const anchors = [...document.querySelectorAll('a[href*="/event/"],.block-tournament a[href*="/sport/"]')];
                    const last = anchors[anchors.length - 1];
                    for (let parent = last && last.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
                        if (parent.scrollHeight > parent.clientHeight && /auto|scroll/.test(getComputedStyle(parent).overflowY)) {
                            parent.scrollTop += Math.max(300, parent.clientHeight * 0.8);
                            return parent.scrollTop + parent.clientHeight >= parent.scrollHeight - 5;
                        }
                    }
                    window.scrollBy(0, Math.max(300, window.innerHeight * 0.8));
                    return window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 5;
                }""")
                if bottom and stable >= 3:
                    break
                await asyncio.sleep(0.3)
            else:
                self.crawler.stats.inc_value('markets/bounds_reached')
        except Exception:
            if not self.stopping: raise
        finally:
            self.active_pages.discard(page)
            await page.close()
        if self.stopping: return
        self.crawler.stats.inc_value('markets/listing_pages')
        def cache_time(item):
            cache = self.state_dir / 'events' / (item[0] + '.json.gz')
            return cache.stat().st_mtime if cache.exists() else 0
        for event_id, event in sorted(candidates.items(), key=cache_time):
            if self.stopping: return
            if event_id in self.events:
                continue
            if len(self.events) >= self.max_events:
                self.crawler.stats.inc_value('markets/bounds_reached')
                break
            self.events.add(event_id)
            self.crawler.stats.inc_value('markets/discovered_events')
            cache = self.state_dir / 'events' / (event_id + '.json.gz')
            age = max(1, int(time.time() - cache.stat().st_mtime)) if cache.exists() else 10000000
            sport = event.get('sport', 'unclassified')
            ordinal = self.sport_queued.get(sport, 0)
            self.sport_queued[sport] = ordinal + 1
            # The first events of every sport precede the second/third ones;
            # large football inventories cannot consume the whole queue first.
            priority = 10000000 - ordinal * 1000 + min(age, 499)
            yield self.request(event['url'], self.parse_event, event_metadata=event, priority=priority)
        for url in sorted(links):
            if self.stopping: return
            if url in self.listings:
                continue
            if len(self.listings) >= self.max_listings:
                self.crawler.stats.inc_value('markets/bounds_reached')
                break
            self.listings.add(url)
            depth = response.meta.get('listing_depth', 0) + 1
            root = urlsplit(url).path.rstrip('/').count('/') == 3
            priority = 20000000 if root else 5000000 - min(depth,1000) * 1000
            yield self.request(url, self.parse_listing, priority=priority if not self.check else 0, listing_depth=depth)

    async def parse_event(self, response):
        page = response.meta['playwright_page']
        self.active_pages.add(page)
        document = None
        try:
            if self.stopping: return
            if response.status in (401, 403, 429):
                raise ValueError('Source limited access')
            source_sport = sport_from_url(page.url)
            if '/sport/' in urlsplit(page.url).path and (not source_sport or (self.sports is not None and source_sport not in self.sports)):
                self.crawler.stats.inc_value('markets/skipped_other_sports')
                return
            await ensure_event_page(page, response.request.url, response.meta.get('event_metadata'))
            sport = sport_from_url(page.url)
            if not sport or (self.sports is not None and sport not in self.sports):
                self.crawler.stats.inc_value('markets/skipped_other_sports')
                return
            snapshot = await read_marketbook(page)
            if snapshot.get('unparsed_outcomes') or snapshot.get('hidden_groups'):
                self.logger.error('Event %s: unparsed_outcomes=%s hidden_groups=%s examples=%s',
                                  event_id_from_url(page.url), snapshot.get('unparsed_outcomes'), snapshot.get('hidden_groups'),
                                  json.dumps(snapshot.get('unsupported', [])[:3], ensure_ascii=False))
                # Keep one bounded public diagnostic, not browser state, forms
                # or a growing HTML archive. It can be inspected on the server.
                address=urlsplit(page.url)
                diagnostic={'at':stamp(),'url':urlunsplit((address.scheme,address.netloc,address.path,'','')),
                            **{key:snapshot.get(key) for key in ('home','away','title','expected_outcomes','unparsed_outcomes','hidden_groups')},
                            'examples':snapshot.get('unsupported',[])[:5]}
                self.state_dir.mkdir(parents=True,exist_ok=True)
                with tempfile.NamedTemporaryFile('w',dir=self.state_dir,delete=False,encoding='utf-8') as output:
                    temporary=Path(output.name); json.dump(diagnostic,output,ensure_ascii=False)
                try: temporary.replace(self.state_dir/'market-diagnostic.json')
                finally: temporary.unlink(missing_ok=True)
            feeds = response.meta.get('market_feed', [])
            if not any(feed['connected'] and 0 <= time.monotonic() - feed['last_frame'] <= 180 for feed in feeds):
                raise ValueError('No current public market feed')
            captured_url,captured_at=page.url,stamp()
            # Prices and their observation time are fixed before opening the
            # read-only statistics control, which may navigate this page.
            try: snapshot['statistics_links']=await discover_statistics_link(page,snapshot)
            except Exception: pass  # Optional statistics must not stop market delivery.
            document = market_document(snapshot, captured_url, captured_at, response.meta.get('event_metadata'))
        except Exception as exc:
            if self.stopping: return
            if '/sport/' in urlsplit(page.url).path and not sport_from_url(page.url):
                self.crawler.stats.inc_value('markets/skipped_other_sports')
                return
            self.crawler.stats.inc_value('markets/failed_events')
            sport = sport_from_url(page.url) or response.meta.get('event_metadata', {}).get('sport')
            if sport:
                self.sport_counts.setdefault(sport, {'saved':0,'complete':0,'failed':0})['failed'] += 1
            # Our validation errors contain fixed diagnostic messages, while
            # browser exceptions can include request details. Keep those out
            # of the journal and still explain a failed source check.
            reason = str(exc)[:200] if isinstance(exc, ValueError) else type(exc).__name__
            self.logger.error('Event %s could not be collected: %s', event_id_from_url(response.request.url), reason)
        finally:
            self.active_pages.discard(page)
            await page.close()
        if document and not self.stopping:
            yield document

    async def failed(self, failure):
        page = failure.request.meta.get('playwright_page')
        if page and not page.is_closed():
            await page.close()
        if self.stopping: return
        self.crawler.stats.inc_value('markets/request_failures')
        self.logger.error('Public page request failed (%s)', failure.type.__name__)


def settings(args):
    launch = {'headless': True, 'args': ['--disable-gpu'],
              'env': {key: value for key, value in os.environ.items() if key not in ('BOOKMAKER_INGEST_TOKEN', 'QWEN_API_KEY')}}
    if args.executable:
        launch['executable_path'] = args.executable
    return {'TWISTED_REACTOR': 'twisted.internet.asyncioreactor.AsyncioSelectorReactor',
            'DOWNLOAD_HANDLERS': {'https': 'scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler'},
            'PLAYWRIGHT_LAUNCH_OPTIONS': launch, 'PLAYWRIGHT_MAX_CONTEXTS': 1,
            'PLAYWRIGHT_ABORT_REQUEST': abort_heavy, 'PLAYWRIGHT_PROCESS_REQUEST_HEADERS': None,
            'PLAYWRIGHT_MAX_PAGES_PER_CONTEXT': args.concurrency,
            'PLAYWRIGHT_CONTEXTS': {'default': {'locale': 'ru-RU', 'timezone_id': 'Europe/Moscow'}},
            'CONCURRENT_REQUESTS': args.concurrency, 'CONCURRENT_REQUESTS_PER_DOMAIN': args.concurrency,
            'DOWNLOAD_DELAY': 0.5, 'RANDOMIZE_DOWNLOAD_DELAY': True, 'DOWNLOAD_TIMEOUT': 180,
            'RETRY_TIMES': 1, 'RETRY_HTTP_CODES': [500, 502, 503, 504],
            'ITEM_PIPELINES': {'matchlab.scrapy_worker.MarketPipeline': 100},
            'LOG_LEVEL': 'ERROR', 'LOGSTATS_INTERVAL': 0, 'TELNETCONSOLE_ENABLED': False,
            'REMOTE_CONTROL_ENABLED': False,
            'MEMUSAGE_ENABLED': False, 'CLOSESPIDER_TIMEOUT': 0,
            'MARKET_CYCLE_TIMEOUT': args.cycle_timeout}


def one_cycle(args) -> int:
    if args.seed and not event_id_from_url(args.seed):
        raise ValueError('Seed must be a current public event URL')
    if not args.no_publish and not os.getenv('BOOKMAKER_INGEST_TOKEN'):
        raise ValueError('BOOKMAKER_INGEST_TOKEN is required')
    process = CrawlerProcess(settings(args))
    crawler = process.create_crawler(WinlineSpider)
    started = time.monotonic()
    process.crawl(crawler, state_dir=args.state_dir, api_url=args.api_url, no_publish=args.no_publish,
                  seed=args.seed, max_events=args.max_events, max_listings=args.max_listings, max_scrolls=args.max_scrolls,
                  check=args.check, interval=args.interval, sports=args.sports)
    process.start()
    values = crawler.stats.get_stats()
    summary = {key.split('/', 1)[1]: value for key, value in values.items() if key.startswith('markets/')}
    summary.update(at=stamp(), cycle_seconds=round(time.monotonic() - started, 2), interval_seconds=args.interval,
                   finish_reason=values.get('finish_reason'), mode='event_markets')
    ready = bool(summary.get('complete_events')) if args.no_publish else bool(summary.get('published_complete_events'))
    summary['traversal_complete'] = (summary['finish_reason'] == 'finished' and not any(summary.get(key) for key in
                                     ('bounds_reached', 'request_failures', 'failed_events', 'publish_failed', 'listing_not_ready')))
    summary['sports'] = crawler.spider.sport_counts if crawler.spider else {}
    summary['incomplete_events'] = max(0, summary.get('saved_events', 0) - summary.get('complete_events', 0))
    summary['success'] = ready and (args.check or bool(args.seed) or (summary['traversal_complete'] and not summary['incomplete_events']))
    if crawler.spider and crawler.spider.publisher:
        state = 'stopped' if summary['finish_reason'] == 'shutdown' else 'ok' if summary['success'] else 'partial' if summary.get('published_events') else 'error'
        asyncio.run(crawler.spider.publish_status(state))
    args.state_dir.mkdir(parents=True, exist_ok=True)
    (args.state_dir / 'cycle-status.json').write_text(json.dumps(summary, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary['success'] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', type=local_api, default=os.getenv('BOOKMAKER_API_URL', 'http://127.0.0.1:8080'))
    parser.add_argument('--state-dir', type=Path, default=Path('.local/crawler'))
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--check', action='store_true', help='Stop after one complete event has been validated and delivered')
    parser.add_argument('--no-publish', action='store_true')
    parser.add_argument('--seed', help='One current canonical event URL for verification')
    parser.add_argument('--sports', default=os.getenv('BOOKMAKER_SPORTS', 'all'), help='all or comma-separated sport keys')
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--concurrency', type=int, default=int(os.getenv('BOOKMAKER_CRAWL_CONCURRENCY', '4')))
    parser.add_argument('--max-events', type=int, default=5000)
    parser.add_argument('--max-listings', type=int, default=200)
    parser.add_argument('--max-scrolls', type=int, default=200)
    parser.add_argument('--cycle-timeout', type=int, default=1800)
    parser.add_argument('--executable')
    args = parser.parse_args()
    try:
        selected_sports(args.sports)
    except ValueError as exc:
        parser.error(str(exc))
    if args.check:
        args.once = True
    if not 1 <= args.concurrency <= 8 or not 10 <= args.interval <= 3600 or min(args.max_events, args.max_listings, args.max_scrolls, args.cycle_timeout) < 1:
        parser.error('Invalid crawler limits')
    os.umask(0o077)
    if args.once:
        try:
            raise SystemExit(one_cycle(args))
        except (ValueError, OSError):
            parser.exit(1, 'Crawler configuration or storage is unavailable. No existing data was deleted.\n')
    child = None
    stopped = False

    def stop(*unused):
        nonlocal stopped
        stopped = True
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped:
        started = time.monotonic()
        child = subprocess.Popen([sys.executable, '-m', 'matchlab.scrapy_worker', *sys.argv[1:], '--once'])
        child.wait()
        child = None
        remaining = max(0, args.interval - (time.monotonic() - started))
        while remaining > 0 and not stopped:
            time.sleep(min(1, remaining))
            remaining = max(0, args.interval - (time.monotonic() - started))


if __name__ == '__main__':
    main()
