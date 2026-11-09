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


async def exercise(base, repo):
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
                search_started, release_search = asyncio.Event(), asyncio.Event()
                async def pause_analysis(route):
                    search_started.set()
                    await release_search.wait()
                    await route.continue_()
                await page.route('**/api/archive/events/*/explain', pause_analysis)
                errors = []
                page.on(
                    "pageerror",
                    lambda error, captured=errors: captured.append(str(error)),
                )
                await page.goto(base + "/#matches")
                await expect(page.locator('#age-gate')).to_be_visible()
                await page.locator('#age-decline').click()
                await expect(page.locator('#age-message')).to_be_visible()
                await page.locator('#age-accept').click()
                await expect(page.locator('#privacy-banner')).to_be_visible()
                await page.locator('#privacy-reject').click()
                await expect(page.locator('#privacy-banner')).to_be_hidden()
                assert (await (await page.request.get(base+'/api/privacy/preferences')).json())['analytics'] is False
                for document in ('terms','privacy','consent','cookies','advertising-consent'):
                    response=await page.request.get(base+'/static/legal/'+document+'.html')
                    assert response.status==200 and 'Кричигин' in await response.text(), document
                await expect(page.locator('.page-footer')).to_contain_text('510302739790')
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
                await page.locator("#auth-terms").check()
                await page.locator("#auth-personal-data").check()
                await page.locator("#auth-age").check()
                await page.locator("#auth-submit").click()
                await expect(page.locator("#auth-dialog")).not_to_be_visible()
                if index == 0:
                    job=None
                    for _ in range(100):
                        job=repo.claim_statistics_request()
                        if job: break
                        await asyncio.sleep(.1)
                    assert job and job['event']['source_event_id']=='102', job
                    assert not search_started.is_set(), 'Анализ запущен до завершения получения статистики'
                    await expect(page.locator('#ai-wait-message')).to_contain_text('статистику')
                    assert repo.finish_statistics_request(job['job_id'],job['lease_id'],'ok')
                await asyncio.wait_for(search_started.wait(), timeout=10)
                await expect(page.locator('#ai-wait')).to_be_visible()
                await expect(page.locator('#ai-wait')).to_contain_text('Готовим разбор')
                assert await page.locator('.ai-wait-track span').evaluate('(node) => getComputedStyle(node).animationName') == 'none'
                assert not await page.evaluate('document.documentElement.scrollWidth > innerWidth')
                await page.screenshot(path=f'/tmp/matchlab-ai-wait-{theme}-{width}.png', full_page=True)
                release_search.set()
                await page.locator("#explanation").wait_for(state="visible")
                await expect(page.locator('#ai-wait')).to_be_hidden()
                result = await page.evaluate('''(value) => {
                    const target = document.createElement('div');
                    renderAnalysis(target, value);
                    return {text:target.textContent, items:target.querySelectorAll('ol li').length, images:target.querySelectorAll('img').length};
                }''', r'1\. **Проверка** &#x20; \`team_statistics\` <img src=x onerror=alert(1)>')
                assert result['items'] == 1 and result['images'] == 0, result
                assert 'статистика участников' in result['text'] and '&#x20;' not in result['text'], result
                await page.evaluate('''() => renderResearchSources(document.getElementById("ai-analysis-sources"), {
                    state: "ready", searched_at: new Date().toISOString(), sources: [
                      {id: 1, title: "<img src=x onerror=window.injected=1>", url: "https://example.org/news"},
                      {id: 2, title: "bad", url: "javascript:alert(1)"}
                    ]})''')
                source_links = page.locator('#ai-analysis-sources a')
                assert await source_links.count() == 1
                assert await source_links.first.get_attribute('href') == 'https://example.org/news'
                assert 'noopener' in await source_links.first.get_attribute('rel')
                assert not await page.evaluate('Boolean(window.injected)')
                await expect(page.locator("#account-button")).to_have_text(username)
                assert MARKER in str(REQUESTS)
                assert await page.evaluate("state.aiAnalysis.context.statistics_preparation.state")=='ok'
                text=await page.locator('#explanation').inner_text()
                assert text.startswith('Наиболее вероятный исход') and 'Рынки' in text and 'Аргументы' in text,text
                assert await page.locator('#explanation .report-section').count()==3
                await expect(page.locator('#ai-analysis-meta')).to_contain_text('История участников найдена.')
                assert 'Пуассон' not in text and 'half_life' not in text
                if index==0:
                    AIHandler.fail_context=True
                    try:
                        await page.locator('details').filter(has=page.locator('#notes')).locator('summary').click()
                        await page.locator('#notes').fill('Проверка недоступного контекста')
                        await page.locator('#explain-button').click()
                        await expect(page.locator('#explanation')).to_contain_text('Не удалось передать контекст нейросети')
                        await expect(page.locator('#ai-analysis-meta')).to_contain_text('Расчётный разбор')
                        assert await page.locator('#ai-request-error').is_hidden()
                    finally: AIHandler.fail_context=False
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
                if index == 0:
                    await page.evaluate("Account.api('/api/bookmakers/events/102/explain',{method:'POST',body:'{}'}).catch(()=>{})")
                    await expect(page.locator('#legal-renewal')).to_be_visible()
                    for selector in ('#legal-terms','#legal-data','#legal-age'):
                        await page.locator(selector).check()
                    await page.locator('#legal-submit').click()
                    await expect(page.locator('#legal-renewal')).to_be_hidden()
                await page.locator("#admin-link").click()
                await page.locator(".admin-metric").first.wait_for()
                await expect(page.locator('#admin-ai-list')).to_contain_text('ИИ ответил')
                await expect(page.locator('#admin-ai-list')).to_contain_text('Превышен контекст')
                await expect(page.locator('#admin-ai-list')).to_contain_text('Вызовы ИИ / части')
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
                    asyncio.run(exercise(base,repo))
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
