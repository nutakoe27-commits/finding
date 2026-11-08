"""Offline navigation/card acceptance; all Winline URLs are fulfilled locally."""
import argparse
import asyncio
from html import escape
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from matchlab.bookmakers import normalize_capture
from matchlab.capture import DOM_SCRIPT, redact
from matchlab.winline_browser import open_event_link, select_football_line


NAV = '<nav><a href="/stavki">Линия</a><a href="/live">Live сейчас</a><a href="/stavki/sport/futbol/">Футбол</a></nav>'


def card(event_id, home, away, body):
    return f'''<article class="card"><div class="card__competitors"><a href="/stavki/event/{event_id}"><div>{escape(home)}</div><div>{escape(away)}</div></a></div>
    {''.join('<div>'+escape(value)+'</div>' for value in body.splitlines())}
    <button onclick="window.betClicks++">Купить ставку</button>
    <form><span>private-card-form</span><input value="private-card-input"></form></article>'''


async def run(executable):
    from playwright.async_api import async_playwright
    recorded = json.loads((Path(__file__).parent / 'fixtures/winline_live_report.json').read_text())['examples'][0]
    live_body = recorded['parents'][-1]['text'].split('Оренбург\n', 1)[1]
    live = '<section class="block-tournament"><h2>Россия | РПЛ</h2>' + card(17021113, 'Крылья Советов', 'Оренбург', live_body) + '</section>'
    upcoming = '<section id="upcoming" class="block-tournament"><h2>Англия | Премьер-лига</h2>'
    upcoming += card(101, 'Брайтон', 'Брентфорд', 'Сегодня 21:00\nМатч\n1.90\n3.85\n3.75\n1.42\n- 1.5 +\n2.70\n1.95\n3.5\n1.85\n1 тайм\n1.10\n1.20\n1.30')
    upcoming += card(102, 'Манчестер Юнайтед', 'Астон Вилла', 'Завтра 21:00\nМатч\n2.16\n3.75\n3.10\n1 тайм\n1.11\n1.22\n1.33')
    upcoming += '</section>'
    loaded_line = False
    requests = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=executable)
        try:
            context = await browser.new_context(locale='ru-RU', timezone_id='Europe/Moscow')

            async def fulfill(route):
                nonlocal loaded_line
                url = urlsplit(route.request.url)
                assert url.hostname == 'winline.ru'
                requests.append(url.path)
                assert url.path.rstrip('/') in ('/stavki', '/stavki/sport/futbol')
                if url.path.rstrip('/') == '/stavki':
                    loaded_line = True
                    body = NAV + '<h1>Линия</h1>'
                else:
                    body = NAV + '<h1>Футбол</h1><div>Исход</div><div id="games">' + live + '</div><div style="height:2000px"></div>'
                    if loaded_line:
                        body += '<script>window.addEventListener("scroll",()=>{if(!document.getElementById("upcoming")) document.getElementById("games").insertAdjacentHTML("beforeend",' + json.dumps(upcoming) + ')});</script>'
                body += '<script>window.betClicks=0;</script>'
                await route.fulfill(status=200, content_type='text/html', body='<!doctype html><meta charset="utf-8">' + body)

            # No request is forwarded to the bookmaker or the internet.
            await context.route('**/*', fulfill)
            page = await context.new_page()
            await page.goto('https://winline.ru/stavki/sport/futbol')
            selected = await select_football_line(page)
            assert selected['line_selected'] and selected['football_selected']
            assert selected['scroll_steps'] > 0
            assert requests[:3] == ['/stavki/sport/futbol', '/stavki', '/stavki/sport/futbol/']
            payload = redact(await page.evaluate(DOM_SCRIPT))
            encoded = json.dumps(payload)
            assert 'private-card-form' not in encoded
            assert 'private-card-input' not in encoded
            document = {'format': 'matchlab.bookmaker-capture.v1', 'records': [
                {'source': 'winline', 'kind': 'public_dom', 'url': page.url,
                 'observed_at': '2026-11-08T09:30:00+00:00', 'payload': payload}]}
            rows = normalize_capture(document)['snapshots']
            assert len(rows) == 2, rows
            assert {row['source_event_id'] for row in rows} == {'101', '102'}
            assert {row['competition'] for row in rows} == {'Англия. Премьер-лига'}
            prices = {row['source_event_id']: row['odds'] for row in rows}
            assert prices['101'] == {'home': 1.9, 'draw': 3.85, 'away': 3.75}
            assert prices['102'] == {'home': 2.16, 'draw': 3.75, 'away': 3.1}
            assert await page.evaluate('window.betClicks') == 0
            await context.unroute('**/*', fulfill)

            async def current_event(route):
                path = urlsplit(route.request.url).path
                if path == '/':
                    body = '<a href="/stavki/event/900001"><div>Сандерленд</div><div>Челси</div></a>'
                elif path == '/stavki/event/900001':
                    await route.fulfill(status=302, headers={'Location': '/stavki/sport/futbol/angliya/apl/900001'}, body='')
                    return
                elif path == '/stavki/sport/futbol/angliya/apl/900001':
                    body = '<h1>Сандерленд — Челси</h1><div class="market">Исход 2.10 3.20 3.40</div>'
                else:
                    raise AssertionError(path)
                await route.fulfill(status=200, content_type='text/html', body='<meta charset="utf-8">' + body)

            await context.route('**/*', current_event)
            await page.goto('https://winline.ru/')
            selected_event = await open_event_link(page, teams=['Сандерленд', 'Челси'])
            assert selected_event['source_event_id'] == '900001'
            assert page.url.endswith('/stavki/sport/futbol/angliya/apl/900001')
        finally:
            await browser.close()
    print('PASS: public Line → Football navigation, bounded lazy loading, separate event/league context, full-match prices and live rejection; all network responses mocked')
    print('PASS: current event link is selected by teams after homepage initialization; no reused event ID')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium', default=shutil.which('chromium'))
    asyncio.run(run(parser.parse_args().chromium))
