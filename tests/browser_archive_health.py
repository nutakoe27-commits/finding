"""Only archive delivery freshness, pending explanations and recovery in admin."""
import json
from datetime import datetime,timedelta,timezone
import shutil
from pathlib import Path

import browser_verdict_outcomes as fixture
from outcome_helpers import archive_result


def delivery(repo,old=False):
    stamp=(datetime.now(timezone.utc)-timedelta(hours=18) if old else datetime.now(timezone.utc)).isoformat()
    value={'last_snapshot_at':stamp,'source_cursor':20,'sent_cursor':20}
    with repo.connection() as db:db.execute('INSERT OR REPLACE INTO archive_status VALUES (1,?,?)',(json.dumps(value),stamp))


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for width in (1440,390):
                delivery(repo,old=True)
                context=await browser.new_context(viewport={'width':width,'height':1000},reduced_motion='reduce',locale='ru-RU')
                page=await context.new_page();page.set_default_timeout(12000);errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                await context.request.post(base+'/api/auth/login',data={'username':'outcomes_admin','password':fixture.PASSWORD})
                await page.goto(base+'/#admin')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await page.locator('[data-admin-tab="results"]').click()
                await expect(page.locator('#ar-sync-health')).to_contain_text('Нет свежего отчёта о доставке архива')
                await expect(page.locator('#ar-sync-health')).to_contain_text('Последняя доставка')
                await expect(page.locator('#ar-updated')).to_contain_text('Отчёт пересчитан')
                await expect(page.locator('#admin-results')).to_have_attribute('data-archive-state','disconnected')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                await page.screenshot(path=f'/workspace/outputs/archive-delivery-stopped-{width}.png',full_page=True)
                delivery(repo)
                await page.evaluate('()=>AdminResults.load()')
                await expect(page.locator('#ar-sync-health')).to_be_hidden()
                await expect(page.locator('#admin-results')).to_have_attribute('data-archive-state','ready')
                if width==1440:
                    with repo.connection() as db:
                        saved=json.loads(db.execute("SELECT payload FROM event_analyses WHERE id='waiting'").fetchone()[0])
                    archive_result(repo,saved['event'],'2:0','waiting')
                    await page.evaluate('()=>AdminResults.load()')
                    await expect(page.locator('#ar-matches')).not_to_contain_text('Ожидаем итог')
                    await expect(page.locator('#ar-matches')).to_contain_text('Пропущена победа')
                await page.evaluate('async()=>{await Account.api("/api/auth/logout",{method:"POST",body:"{}"});await Account.refresh()}')
                await expect(page.locator('#ar-sync-health')).to_be_hidden()
                assert not errors,errors
                await context.close()
        finally:await browser.close()
    print('PASS: stale delivery warning vs report timestamp; mobile layout; restored delivery; newly received result settles; logout cleanup; no model calls')


if __name__=='__main__':
    fixture.exercise=exercise
    fixture.main()
