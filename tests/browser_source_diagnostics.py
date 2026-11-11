"""Only the new administrator explanation of a guarded request; no AI calls."""
from datetime import datetime,timezone
import json
import shutil

import browser_verdict_outcomes as fixture


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    blocker='Нет пригодных данных для сравнения об участниках: Чарльстон Кугарс.'
    with repo.connection() as db:
        owner=db.execute("SELECT id FROM users WHERE username='outcomes_admin'").fetchone()[0]
        db.execute('INSERT INTO ai_usage(id,user_id,at,kind,target,status,details) VALUES (?,?,?,?,?,?,?)',
            ('guarded-diagnostic',owner,datetime.now(timezone.utc).isoformat(),'event','/api/bookmakers/events/17136987/explain','limited',
             json.dumps({'state':'skipped','reason':'insufficient_data','model_calls':0,'parts_total':1,'parts_completed':0,'input_blockers':[blocker]})))
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for width in (1440,390):
                context=await browser.new_context(viewport={'width':width,'height':1000},locale='ru-RU')
                page=await context.new_page();page.set_default_timeout(12000);errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                await context.request.post(base+'/api/auth/login',data={'username':'outcomes_admin','password':fixture.PASSWORD})
                await page.goto(base+'/#admin')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await expect(page.locator('#admin-ai-list')).to_contain_text(blocker)
                await expect(page.locator('#admin-ai-list')).to_contain_text('ИИ не вызывался')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                assert not errors,errors
                await page.locator('#admin-ai-list').screenshot(path=f'/workspace/outputs/source-diagnostics-{width}.png')
                await context.close()
        finally:await browser.close()
    print('PASS: precise missing-participant reason in administrator history; 1440/390; no AI calls')


if __name__=='__main__':
    fixture.exercise=exercise
    fixture.main()
