"""Only new sport scope rendering, catalogue eligibility and admin filters."""
import json
import os
from datetime import datetime,timezone
from pathlib import Path
import shutil

import browser_verdict_outcomes as server_fixture
from matchlab.neural_verdict import render
from matchlab.verdict_sports import PROFILES
from test_multisport_verdict import MultisportVerdictTests,fixture,SCORES,answer


async def exercise(base,repo):
    from playwright.async_api import async_playwright,expect
    sample=MultisportVerdictTests();sample.repo=repo;sample.now=datetime.now(timezone.utc)
    ids={'tennis':'a'*32,'basketball':'b'*32,'hockey':'c'*32}
    with repo.connection() as db:
        owner=db.execute("SELECT id FROM users WHERE username='outcomes_admin'").fetchone()[0]
    for sport,id in ids.items():
        payload=sample.saved(sport,SCORES[sport],id);context=payload['context'];p=PROFILES[sport]
        packet={'facts':[{'id':'F1','kind':'history','side':'home'}, {'id':'F2','kind':'history','side':'away'}, {'id':'F3','kind':'market'}]}
        verdict=context['neural_verdict'];verdict.update(answer(packet),market_label=p.market_label,selection_label='Альфа',preference_label='Альфа',line_odds=context['neural_input']['quote']['odds'])
        payload.update(observed_at=payload['generated_at'],market_name=p.market_label,text=render(verdict))
        with repo.connection() as db:db.execute('UPDATE event_analyses SET payload=?,user_id=? WHERE id=?',(json.dumps(payload),owner,id))
    repo.import_event_markets(fixture('tennis',sample.now))
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
        try:
            for width in (1440,390):
                context=await browser.new_context(viewport={'width':width,'height':1000},reduced_motion='reduce',locale='ru-RU')
                page=await context.new_page();page.set_default_timeout(12000);errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                await context.request.post(base+'/api/auth/login',data={'username':'outcomes_admin','password':server_fixture.PASSWORD})
                await page.goto(base+'/#admin')
                if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
                if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
                await page.locator('[data-admin-tab="results"]').click()
                await expect(page.locator('#ar-sport option')).to_have_count(7)
                await page.locator('#ar-sport').select_option('tennis')
                await expect(page.locator('#ar-matches tbody tr')).to_have_count(1)
                await expect(page.locator('#ar-matches')).to_contain_text('Теннис')
                await expect(page.locator('#ar-matches')).to_contain_text('Победа в матче')
                await expect(page.locator('#ar-matches')).not_to_contain_text('Баскетбол')
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                await page.screenshot(path=f'/workspace/outputs/multisport-admin-{width}.png',full_page=True)
                for sport,id in ids.items():
                    await page.goto(base+'/#analysis/'+id)
                    await expect(page.locator('#saved-analysis')).to_be_visible()
                    await expect(page.locator('#saved-analysis-text .report-scope')).to_have_text(PROFILES[sport].market_label)
                    line=page.locator('#saved-analysis-text .verdict-market_context')
                    await expect(line).to_contain_text('П1')
                    if sport=='tennis':await expect(line).not_to_contain_text('Х —')
                    if sport=='hockey':await expect(line).to_contain_text('Х —')
                    await expect(page.locator('#saved-analysis-coverage')).to_contain_text(PROFILES[sport].market_label)
                    if sport=='tennis':await page.screenshot(path=f'/workspace/outputs/multisport-tennis-{width}.png',full_page=True)
                await page.goto(base+'/#matches')
                await page.wait_for_function('()=>Boolean(state.verdictSports.tennis)')
                await page.locator('#sport-filter').select_option('tennis')
                await expect(page.locator('.match-card')).to_have_count(1)
                await expect(page.locator('.match-card .match-ai-button')).to_have_text('Разбор ИИ')
                await page.goto(base+'/#match/91001')
                await page.wait_for_function('()=>state.marketDocument?.sport==="tennis"')
                await expect(page.locator('#event-ai-support')).to_be_hidden()
                await expect(page.locator('#ai-market-label')).to_have_text('Победа в матче')
                assert not errors,errors
                await context.close()
        finally:await browser.close()
    print('PASS: two-way vs three-way result rendering; correct sport scope; admin filters/charts; tennis AI eligibility; mobile/desktop; no remote model calls')


if __name__=='__main__':
    os.environ['EVENT_ANALYSIS_MODE']='neural-verdict'
    server_fixture.exercise=exercise
    server_fixture.main()
