"""End-to-end registration gate, compact statistics, private history and admin UI."""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from browser_archive_smoke import feed, send
from browser_market_ui import REQUESTS, AIHandler
from test_archive import header, rekey, result
from test_product import event
from test_statistics import stats

from matchlab import accounts
from matchlab.storage import Repository

PASSWORD = "browser-fixture-password-123"
MARKER = "FULL_AI_STATISTICS_END_MARKER"


async def exercise(base):
    from playwright.async_api import async_playwright, expect

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=shutil.which("chromium")
        )
        try:
            for index, (theme, width) in enumerate(
                (("dark", 1440), ("light", 390), ("dark", 320))
            ):
                context = await browser.new_context(
                    viewport={"width": width, "height": 1000},
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
                await page.goto(base + "/#matches")
                await page.locator('.match-card[data-event-id="102"]').wait_for()
                await page.locator(".match-ai-button").first.click()
                await expect(page.locator("#auth-dialog")).to_be_visible()
                await expect(page.locator("#auth-title")).to_contain_text(
                    "Ваш личный аналитик"
                )
                await page.screenshot(
                    path=f"/tmp/matchlab-register-{theme}-{width}.png", full_page=True
                )
                username = f"browser_reader_{index}"
                await page.locator("#auth-username").fill(username)
                await page.locator("#auth-password").fill(PASSWORD)
                await page.locator("#auth-submit").click()
                await expect(page.locator("#auth-dialog")).not_to_be_visible()
                await page.locator("#explanation").wait_for(state="visible")
                await expect(page.locator("#account-button")).to_have_text(username)
                assert MARKER in REQUESTS[-1]["team_statistics"]["sections"][0]["text"]
                await page.locator("#team-statistics > summary").click()
                assert MARKER not in await page.locator("#team-statistics").inner_text()
                assert (
                    len(await page.locator("#team-statistics-sections").inner_text())
                    < 1600
                )
                assert await page.locator(".snapshot-info").is_hidden()
                assert "/go/winline/102?source=stream" in await page.locator(
                    "#event-source"
                ).get_attribute("href")
                response = await context.request.get(
                    base + "/go/winline/102?source=stream", max_redirects=0
                )
                assert (
                    response.status == 302
                    and "/stavki/event/102" in response.headers["location"]
                )
                assert not await page.evaluate(
                    "document.documentElement.scrollWidth>innerWidth"
                ), (width, "match overflow")
                await page.screenshot(
                    path=f"/tmp/matchlab-compact-match-{theme}-{width}.png",
                    full_page=True,
                )
                await page.locator('a[href="#history"]').first.click()
                await page.locator(".history-card").first.wait_for()
                await page.locator("#account-button").click()
                await page.locator("#account-logout").click()
                await page.locator(".match-card").first.wait_for()
                await expect(page.locator("#account-button")).to_have_text("Войти")
                assert await page.locator("#history-list").inner_text() == ""
                await page.locator("#account-button").click()
                await page.locator("#auth-username").fill("admin_fixture")
                await page.locator("#auth-password").fill(PASSWORD)
                await page.locator("#auth-submit").click()
                await expect(page.locator("#admin-link")).to_be_visible()
                await page.locator("#admin-link").click()
                await page.locator(".admin-metric").first.wait_for()
                assert not await page.evaluate(
                    "document.documentElement.scrollWidth>innerWidth"
                ), (width, "admin overview overflow")
                await page.screenshot(
                    path=f"/tmp/matchlab-admin-{theme}-{width}.png", full_page=True
                )
                await page.locator('[data-admin-tab="promos"]').click()
                await page.locator("#admin-promo-code").fill(f"BROWSER_{index}")
                await page.locator("#admin-promo-title").fill("Первый запуск")
                await page.locator('#admin-promo-form button[type="submit"]').click()
                await expect(page.locator("#admin-password-dialog")).to_be_visible()
                await page.locator("#admin-password").fill(PASSWORD)
                await page.locator("#admin-password-form button").click()
                await expect(page.locator("#admin-promos")).to_contain_text(
                    f"BROWSER_{index}"
                )
                promo = page.locator("#admin-promos tr").filter(
                    has_text=f"BROWSER_{index}"
                )
                await promo.get_by_role("button", name="Включить", exact=True).click()
                await expect(promo).to_contain_text("Активен")
                await page.locator('[data-admin-tab="push"]').click()
                await page.locator("#push-title").fill(
                    'Матчи дня <img src=x onerror="window.adminInjected=1">'
                )
                await page.locator("#push-body").fill("Короткая подборка событий")
                await page.locator('#admin-push-form button[type="submit"]').click()
                await expect(page.locator("#admin-campaigns")).to_contain_text(
                    "Матчи дня"
                )
                await (
                    page.locator("#admin-campaigns")
                    .get_by_role("button", name="Предпросмотр")
                    .first.click()
                )
                await expect(page.locator("#push-confirm-dialog")).to_be_visible()
                assert await page.locator("#push-confirm-send").is_disabled()
                assert not await page.evaluate("Boolean(window.adminInjected)")
                await page.locator("#push-confirm-close").click()
                assert not await page.evaluate(
                    "document.documentElement.scrollWidth>innerWidth"
                ), (width, "push overflow")
                await page.locator('[data-admin-tab="users"]').click()
                await page.locator("#admin-user-search").fill(username)
                await expect(page.locator("#admin-users")).to_contain_text(username)
                assert not await page.evaluate(
                    "document.documentElement.scrollWidth>innerWidth"
                ), (width, "users overflow")
                await page.locator('[data-admin-tab="audit"]').click()
                await expect(page.locator("#admin-audit")).to_contain_text(
                    "Создан промокод"
                )
                assert not errors, errors
                await context.close()
        finally:
            await browser.close()


def main():
    ai = ThreadingHTTPServer(("127.0.0.1", 0), AIHandler)
    Thread(target=ai.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="matchlab-accounts-ui-") as folder:
            repo = Repository(Path(folder) / "matchlab.sqlite3")
            repo.initialize()
            accounts.create_user(repo, "admin_fixture", PASSWORD, "admin")
            legacy = event("102")
            legacy.update(
                home="Спартак М",
                away="Акрон Тольятти",
                statistics_url="https://winline.ru/stats/match/72334410",
            )
            repo.import_event_markets(legacy)
            statistics = stats("102")
            statistics["sections"][0]["text"] = (
                "Подтверждённая статистика сезона. " * 60
            )
            # Sort priority puts the recent-results section first in model context.
            for section in statistics["sections"]:
                section["text"] += (
                    "\n" + ("Подробные подтверждённые показатели. " * 100) + MARKER
                )
            repo.import_statistics(statistics)
            h = header("102")
            h.update(
                members=[legacy["home"], legacy["away"]],
                kickoff_at=legacy["kickoff_at"],
            )
            live = header("101", live=True)
            feed(repo, [h, live])
            historical = result()
            historical["data"].update(member1=legacy["home"], member2=legacy["away"])
            send(repo, "results", source="a" * 64, rows=[rekey(historical)])
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            env = dict(
                os.environ,
                MATCHLAB_DATA_DIR=folder,
                PUBLIC_ORIGIN=base,
                QWEN_BASE_URL=f"http://127.0.0.1:{ai.server_port}/v1",
                QWEN_MODEL="fixture",
                QWEN_API_KEY="",
                BOOKMAKER_INGEST_TOKEN="fixture",
            )
            with open("/tmp/matchlab-account-browser-server.log", "w") as log:
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
                    stdout=log,
                    stderr=log,
                )
                try:
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({})
                    )
                    for _ in range(100):
                        try:
                            opener.open(base + "/api/auth/me", timeout=1).close()
                            break
                        except OSError:
                            time.sleep(0.1)
                    else:
                        raise AssertionError("App did not start")
                    asyncio.run(exercise(base))
                    print(
                        "Registration, private AI, compact statistics and admin desktop/mobile: OK"
                    )
                finally:
                    server.terminate()
                    server.wait(timeout=20)
    finally:
        ai.shutdown()
        ai.server_close()


if __name__ == "__main__":
    main()
