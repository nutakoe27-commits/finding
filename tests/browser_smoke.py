"""Optional browser acceptance test against a running, populated Matchlab."""
import argparse
import asyncio
from pathlib import Path
import shutil

from playwright.async_api import async_playwright


async def check(base_url: str, chromium: str | None, screenshots: Path | None):
    async with async_playwright() as playwright:
        options = {"headless": True}
        if chromium:
            options["executable_path"] = chromium
        browser = await playwright.chromium.launch(**options)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1100}, locale="ru-RU")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base_url + "/#calculator", wait_until="networkidle")
            await page.locator('#age-accept').click(); await page.locator('#privacy-reject').click()
            await page.wait_for_function("!document.getElementById('predict-button').disabled")
            assert await page.locator("#home-team option").count() >= 2
            await page.locator("#predict-button").click()
            await page.locator("#result-content").wait_for(state="visible")
            assert await page.locator("#result-home").inner_text() == "Arsenal"
            assert await page.locator("#result-away").inner_text() == "Chelsea"
            probabilities = [float((await page.locator("#prob-" + key).inner_text()).removesuffix("%")) for key in ("home", "draw", "away")]
            assert abs(sum(probabilities) - 100) < 0.2
            if screenshots:
                screenshots.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshots / "desktop.png"), full_page=True)

            await page.locator("#odds-details").evaluate("element => { element.open = true; }")
            for key, value in (("home", "2.0"), ("draw", "3.5"), ("away", "4.0")):
                await page.locator("#odds-" + key).fill(value)
            await page.locator("#predict-button").click()
            await page.locator("#market-result").wait_for(state="visible")
            assert "EV" in await page.locator("#market-result").inner_text()

            await page.locator("#away-team").select_option("Arsenal")
            await page.locator("#predict-button").click()
            await page.locator("#forecast-error").wait_for(state="visible")
            assert not await page.locator("#result-content").is_visible()

            await page.locator("#as-of").fill("2024-05-01")
            await page.locator("#as-of").dispatch_event("change")
            await page.wait_for_function("!document.getElementById('predict-button').disabled")
            assert await page.locator("#odds-home").is_disabled()
            await page.locator("#away-team").select_option("Chelsea")
            await page.locator("#predict-button").click()
            await page.locator("#result-content").wait_for(state="visible")
            assert "ИСТОРИЧЕСКИЙ" in await page.locator("#result-tag").inner_text()
            assert "01.05.2024" in await page.locator("#training-note").inner_text()

            # A late response must not display a forecast for a selection that
            # the user has already changed.
            captured, release = asyncio.Event(), asyncio.Event()

            async def delayed_response(route):
                response = await route.fetch()
                captured.set()
                await release.wait()
                await route.fulfill(response=response)

            await page.route("**/api/forecast", delayed_response)
            await page.locator("#predict-button").click()
            await asyncio.wait_for(captured.wait(), timeout=10)
            await page.locator("#away-team").select_option("Liverpool")
            async with page.expect_response("**/api/forecast"):
                release.set()
            await page.wait_for_function("document.querySelector('.result-panel').getAttribute('aria-busy') === 'false'")
            assert not await page.locator("#result-content").is_visible(), "Stale forecast displayed"
            await page.unroute("**/api/forecast", delayed_response)
            await page.locator("#predict-button").click()
            await page.locator("#result-content").wait_for(state="visible")
            assert await page.locator("#result-away").inner_text() == "Liverpool"

            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile overflow"
            if screenshots:
                await page.screenshot(path=str(screenshots / "mobile.png"), full_page=True)
            assert not errors, errors
            print("PASS: forecast, manual odds, input validation, historical cutoff, stale response, mobile layout, no JavaScript errors")
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--chromium", default=shutil.which("chromium"))
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    asyncio.run(check(args.base_url, args.chromium, args.screenshots))
