"""Archive + live product flow through real SQLite, FastAPI and Chromium."""

import asyncio
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from browser_market_ui import REQUESTS, AIHandler
from test_archive import header, result

from matchlab import archive
from matchlab.storage import Repository
from winline.projection import FORMAT


def send(repo, mode, **fields):
    return archive.ingest(repo, {"format": FORMAT, "mode": mode, **fields})


def feed(repo, events):
    import hashlib
    import json

    cycle = hashlib.sha256(json.dumps(events).encode()).hexdigest()
    send(repo, "feed", cycle=cycle, events=events)
    send(
        repo,
        "commit",
        cycle=cycle,
        source_at=max(e["observed_at"] for e in events),
        expected=len(events),
    )
    send(
        repo,
        "status",
        status={
            "checked_at": archive.now(),
            "events": len(events),
            "results": 1,
            "lines": 3,
            "source_cursor": 1,
            "sent_cursor": 1,
            "last_snapshot_at": max(e["observed_at"] for e in events),
            "worker_state": "collecting",
            "worker_job": "fast",
        },
    )


async def exercise(base, repo):
    from playwright.async_api import async_playwright, expect

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=shutil.which("chromium")
        )
        try:
            for theme, width in (("dark", 1440), ("light", 390), ("dark", 320)):
                context = await browser.new_context(
                    viewport={"width": width, "height": 950},
                    color_scheme=theme,
                    reduced_motion="reduce",
                    locale="ru-RU",
                )
                page = await context.new_page()
                errors = []
                page.on(
                    "pageerror",
                    lambda error, captured=errors: captured.append(str(error)),
                )
                live, future = header(live=True), header("102")
                feed(repo, [live, future])
                send(repo, "detail", event=live, markets=[live["primary"]])
                await page.goto(base + "/#matches")
                await page.locator('.match-card[data-event-id="102"]').wait_for()
                await expect(page.locator(".match-card")).to_have_count(1)
                await page.locator('[data-phase="live"]').click()
                card = page.locator('.match-card[data-event-id="101"]')
                await expect(card).to_contain_text("0:0")
                await card.locator(".match-card-title").click()
                await expect(page).to_have_url(base + "/#stream/101")
                await expect(page.locator("#event-phase")).to_contain_text("0:0")
                await expect(page.locator("#archive-team-cards")).to_contain_text("1:0")
                assert not await page.evaluate(
                    "document.documentElement.scrollWidth > innerWidth"
                ), (theme, width, "detail overflow")
                await page.locator("#explain-button").click()
                await page.locator("#explanation").wait_for(state="visible")
                assert REQUESTS[-1]["archive_history"]["state"] == "ready"
                assert REQUESTS[-1]["event"]["live_score"] == "0:0"
                assert not await page.evaluate("Boolean(window.analysisInjected)")
                # The normal ten-second timer must deliver score and prices without navigation.
                live["observed_at"] = archive.now()
                live["score"] = "2:1"
                live["primary"]["outcomes"][0]["odds"] = 2.55
                live["primary"]["observed_at"] = live["observed_at"]
                feed(repo, [live, future])
                await expect(page.locator("#event-phase")).to_contain_text(
                    "2:1", timeout=16000
                )
                await expect(page.locator("#event-primary")).to_contain_text("2.55")
                assert page.url.endswith("#stream/101")
                await page.screenshot(
                    path=f"/tmp/matchlab-live-{theme}-{width}.png", full_page=True
                )
                # Lost updates must retain the last observation and show its age.
                old = deepcopy(live)
                old["observed_at"] = (
                    datetime.now(UTC) - timedelta(minutes=3)
                ).isoformat()
                old["primary"]["observed_at"] = old["observed_at"]
                with repo.connection() as db:
                    active = db.execute("SELECT cycle FROM archive_feed").fetchone()[0]
                    db.execute(
                        "UPDATE archive_feed_rows SET payload=? WHERE cycle=? AND event_id='101'",
                        (archive.packed(old), active),
                    )
                await expect(page.locator("#event-freshness")).to_have_class(
                    re.compile(r"\bstale\b"), timeout=16000
                )
                await page.locator('a[href="#archive"]').first.click()
                await page.locator(".archive-result-card").first.wait_for()
                await page.locator("#archive-search").fill("КОМАНДА")
                await expect(page.locator("#archive-summary")).to_contain_text(
                    "Результатов: 1"
                )
                assert not await page.evaluate(
                    "document.documentElement.scrollWidth > innerWidth"
                ), (theme, width, "archive overflow")
                await page.screenshot(
                    path=f"/tmp/matchlab-archive-{theme}-{width}.png", full_page=True
                )
                await page.locator("#archive-search").fill("Нет такого участника")
                await expect(page.locator("#archive-summary")).to_contain_text(
                    "Совпадающих результатов пока нет"
                )
                assert not errors, errors
                await context.close()
        finally:
            await browser.close()


def main():
    ai = ThreadingHTTPServer(("127.0.0.1", 0), AIHandler)
    Thread(target=ai.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="matchlab-archive-ui-") as folder:
            repo = Repository(Path(folder) / "matchlab.sqlite3")
            repo.initialize()
            send(repo, "results", source="a" * 64, rows=[result()])
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            env = dict(
                os.environ,
                MATCHLAB_DATA_DIR=folder,
                QWEN_BASE_URL=f"http://127.0.0.1:{ai.server_port}/v1",
                QWEN_MODEL="archive-fixture",
                QWEN_API_KEY="",
                BOOKMAKER_INGEST_TOKEN="test",
            )
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "matchlab.api:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                base = f"http://127.0.0.1:{port}"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:
                        opener.open(base + "/api/archive/status", timeout=1).close()
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise AssertionError("Fixture API did not start")
                asyncio.run(exercise(base, repo))
                print("Archive, live polling, AI context and responsive layouts: OK")
            finally:
                server.terminate()
                server.wait(timeout=20)
    finally:
        ai.shutdown()
        ai.server_close()


if __name__ == "__main__":
    main()
