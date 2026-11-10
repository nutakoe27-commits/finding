"""Replay the observed public Winline fast-bets DOM without clicking any odds."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from matchlab.popular_markets import POPULAR_SCRIPT
from matchlab.independent_context import without_prices

FIXTURES=Path(__file__).parent/'fixtures'


async def main():
    expected=json.loads((FIXTURES/'winline_popular_markets.json').read_text())['markets']
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            page=await browser.new_page(viewport={'width':1280,'height':900})
            await page.set_content((FIXTURES/'winline_popular_markets.html').read_text())
            first=await page.evaluate(POPULAR_SCRIPT)
            assert first['complete'] and not first['unsupported'],first
            assert len(first['markets'])==8
            assert without_prices(first['markets'])==without_prices(expected)
            original=deepcopy(first['markets'])
            await page.locator('.odd-btn > span:not(.odd-btn__label)').evaluate_all("els=>els.forEach((e,i)=>e.textContent=i%2?'1000':'1.01')")
            changed=await page.evaluate(POPULAR_SCRIPT)
            assert without_prices(changed['markets'])==without_prices(original),'Price changes altered the selected conditions'
            await page.locator('.coef-btn').first.evaluate("e=>e.textContent='+ 0.5 -'")
            moved=await page.evaluate(POPULAR_SCRIPT)
            assert [o['parameter'] for o in moved['markets'][1]['outcomes']]==['+0.5','-0.5']
            await page.locator('.coef-btn').first.evaluate("e=>e.textContent='0'")
            zero=await page.evaluate(POPULAR_SCRIPT)
            assert [o['parameter'] for o in zero['markets'][1]['outcomes']]==['0','0']
            await page.locator('.coef-btn').first.evaluate("e=>e.textContent='unknown condition'")
            missing=await page.evaluate(POPULAR_SCRIPT)
            assert len(missing['markets'])==7 and not missing['complete'] and len(missing['unsupported'])==1
            print('PASS: observed 8 popular cards, 18 outcomes, real periods and line directions, price independence, malformed-card rejection')
        finally:await browser.close()


if __name__=='__main__':asyncio.run(main())
