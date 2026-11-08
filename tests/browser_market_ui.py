"""Exercise the real market API and interface using an isolated fixture DB."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from matchlab.markets import FORMAT
from matchlab.storage import Repository


async def run(url):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=shutil.which('chromium'))
        try:
            for width, height in ((1440, 1000), (390, 844)):
                page = await browser.new_page(viewport={'width': width, 'height': height})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(url)
                await page.locator('#market-event-rows button').first.click()
                await page.locator('#market-detail-title').wait_for()
                assert await page.locator('#market-detail-title').inner_text() == 'Сандерленд — Челси'
                await page.locator('#market-groups summary').first.click()
                assert await page.locator('.market-outcomes').inner_text() == 'П1\n3.9\nX\n3.75\nП2\n1.89'
                await page.locator('#market-search').fill('Несуществующий рынок')
                assert await page.locator('#market-groups details').count() == 0
                await page.locator('#market-search').fill('1X2')
                assert await page.locator('#market-groups details').count() == 1
                assert not await page.evaluate('Boolean(window.marketInjected)')
                assert not errors, errors
                await page.close()
        finally:
            await browser.close()
    print('PASS: real API + desktop/mobile market list, lazy outcomes, searching, source prices and safe text rendering')


def main():
    with tempfile.TemporaryDirectory(prefix='matchlab-market-ui-') as folder:
        directory = Path(folder)
        if (ROOT / '.local/matchlab.sqlite3').exists():
            shutil.copy2(ROOT / '.local/matchlab.sqlite3', directory / 'matchlab.sqlite3')
        repo = Repository(directory / 'matchlab.sqlite3')
        repo.initialize()
        now = datetime.now(timezone.utc)
        repo.import_event_markets({'format': FORMAT, 'source': 'winline', 'sport': 'football',
            'source_event_id': '17006967', 'url': 'https://winline.ru/stavki/sport/futbol/angliya/apl/17006967',
            'home': 'Сандерленд', 'away': 'Челси', 'competition': 'Англия. Премьер-лига', 'phase': 'prematch',
            'observed_at': now.isoformat(), 'kickoff_at': (now + timedelta(days=2)).isoformat(),
            'parser_version': 'isolated-ui-fixture', 'markets': [{'name': 'Исход 1X2 <img src=x onerror="window.marketInjected=1">',
                'period': 'осн.время', 'outcomes': [{'name': 'П1', 'odds': 3.9}, {'name': 'X', 'odds': 3.75}, {'name': 'П2', 'odds': 1.89}]}],
            'coverage': {'expected_outcomes': 3, 'all_tab_selected': True, 'stable_passes': 2, 'hidden_groups': 0, 'scroll_exhausted': True}})
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, MATCHLAB_DATA_DIR=folder, QWEN_BASE_URL='', QWEN_MODEL='', QWEN_API_KEY='', BOOKMAKER_INGEST_TOKEN='')
        server = subprocess.Popen([str(ROOT / '.venv/bin/python'), '-m', 'uvicorn', 'matchlab.api:app', '--host', '127.0.0.1', '--port', str(port)],
                                  cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = f'http://127.0.0.1:{port}'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                try:
                    opener.open(base + '/api/bookmakers/events', timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError('Fixture API did not start')
            asyncio.run(run(base))
        finally:
            server.terminate()
            server.wait(timeout=20)


if __name__ == '__main__':
    main()
