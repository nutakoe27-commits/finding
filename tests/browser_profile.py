"""Profile layout and actual account forms against local, isolated fixtures."""
import asyncio
from datetime import datetime,timedelta,timezone
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from uuid import uuid4
ROOT=Path(__file__).resolve().parent.parent
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from matchlab import accounts,billing
from matchlab.storage import Repository
from outcome_helpers import forecast


async def exercise(base,folder):
 from playwright.async_api import async_playwright,expect
 repo=Repository(Path(folder)/'matchlab.sqlite3')
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
  try:
   for width,theme in ((1440,'light'),(1024,'light'),(390,'dark'),(320,'light')):
    ctx=await browser.new_context(viewport={'width':width,'height':1050},locale='ru-RU',color_scheme=theme,reduced_motion='reduce')
    user='profile_reader_'+str(width);password='profile-password-long-123'
    registered=await ctx.request.post(base+'/api/auth/register',data={'username':user,'password':password,'terms_accepted':True,
       'personal_data_consent':True,'age_confirmed':True,'legal_version':accounts.LEGAL_VERSION})
    assert registered.status==201,await registered.text()
    uid=(await registered.json())['user']['id'];now=datetime.now(timezone.utc)
    with repo.connection() as db:
     db.execute('UPDATE users SET display_name=? WHERE id=?',('Михаил Кричигин',uid))
     db.execute('INSERT INTO promo_codes VALUES (?,?,?,?,?,?,?,?,?,?)',(uuid4().hex,'PROFILE'+str(width),'Profile fixture',3,10,
       (now-timedelta(days=1)).isoformat(),(now+timedelta(days=1)).isoformat(),'active',now.isoformat(),'local-fixture'))
    page=await ctx.new_page();page.set_default_timeout(12000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    await page.goto(base+'/#account')
    if await page.locator('#age-gate').is_visible():await page.locator('#age-accept').click()
    if await page.locator('#privacy-banner').is_visible():await page.locator('#privacy-reject').click()
    await expect(page.locator('#account-name')).to_have_text('Михаил Кричигин')
    await expect(page.locator('#profile-avatar')).to_have_text('МК')
    await expect(page.locator('#account-usage')).to_be_hidden()
    await expect(page.locator('#account-credit-balance')).to_have_text('0 разборов')
    await expect(page.locator('#subscription-state')).to_have_text('Не подключена')
    await expect(page.locator('#account-open-analyses')).to_contain_text('Первый разбор')
    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(width,'empty profile overflow')
    if width>=1024:
     a=await page.locator('#account-wallet').bounding_box();b=await page.locator('#account-subscription').bounding_box()
     assert abs(a['y']-b['y'])<2 and b['x']>a['x'],(width,a,b)
     assert a['height']<355 and b['height']<355,(width,a,b)
    await page.screenshot(path=f'/workspace/outputs/profile-empty-{width}.png',full_page=False)
    await page.locator('#subscription-choose').click();await expect(page.locator('#credits-dialog')).to_be_visible();await page.locator('#credits-close').click()
    await page.locator('#profile-promo > summary').click();await page.locator('#promo-code').fill('WRONGCODE')
    await page.locator('#promo-redeem-form button').click()
    await expect(page.locator('#profile-promo-feedback')).to_have_class(__import__('re').compile('is-error'))
    await page.locator('#promo-code').fill('PROFILE'+str(width))
    await page.locator('#promo-redeem-form button').click()
    await expect(page.locator('#profile-promo-feedback')).to_contain_text('Промокод применён')
    await expect(page.locator('#account-credit-balance')).to_have_text('3 разбора')
    await page.locator('#profile-promo > summary').click()
    await page.locator('[data-profile-tab=settings]').click()
    await expect(page.locator('#profile-panel-analyses')).to_be_hidden()
    await page.locator('#account-password-card > summary').click()
    await page.locator('#account-old-password').fill('wrong-password-123');await page.locator('#account-new-password').fill('profile-new-password-123')
    await page.locator('#account-password-form button').click()
    await expect(page.locator('#profile-password-feedback')).not_to_be_empty()
    await expect(page.locator('#profile-password-feedback')).to_have_class(__import__('re').compile('is-error'))
    await page.locator('#account-old-password').fill(password);await page.locator('#account-password-form button').click()
    await expect(page.locator('#profile-password-feedback')).to_contain_text('Пароль изменён')
    await page.locator('#account-password-card > summary').click()
    await expect(page.locator('#account-password-form')).to_be_hidden()
    await page.screenshot(path=f'/workspace/outputs/profile-settings-{width}.png',full_page=False)
    await page.reload();await expect(page.locator('#profile-tab-settings')).to_have_attribute('aria-selected','true')
    await page.locator('#profile-tab-settings').focus();await page.keyboard.press('Home')
    await expect(page.locator('#profile-tab-analyses')).to_be_focused()
    await page.keyboard.press('End');await expect(page.locator('#profile-tab-settings')).to_be_focused()
    with repo.connection() as db:
     db.execute('UPDATE users SET bonus_analyses=12345 WHERE id=?',(uid,))
     for i,state in enumerate(('succeeded','canceled')):
      oid=uuid4().hex
      db.execute('''INSERT INTO payment_orders(id,user_id,request_key,mode,package_id,credits,amount_kopecks,state,
       created_at,updated_at,offer_version,receipt_email) VALUES (?,?,?,'live','five',5,219000,?,?,?,?,?)''',
       (oid,uid,uuid4().hex,state,now.isoformat(),now.isoformat(),billing.PUBLIC_OFFER_VERSION,'profile@example.ru'))
    for index,(home,away) in enumerate((('Барселона','Валенсия'),('Арсенал','Челси'),('Милан','Рома'))):
     key=uuid4().hex;e={'source_event_id':str(123000+width+index),'home':home,'away':away,'sport':'football','phase':'prematch','competition':'Пример турнира','kickoff_at':(now+timedelta(days=1)).isoformat()}
     item=forecast(repo,key,event=e,kickoff=now+timedelta(days=1),at=now,score=None)
     with repo.connection() as db:
      db.execute('UPDATE event_analyses SET user_id=? WHERE id=?',(uid,key))
      db.execute('INSERT INTO analysis_unlocks VALUES (?,?,?,?,?)',(uid,key,'live',now.isoformat(),'bonus'))
    await page.evaluate('Credits.refresh()');await page.locator('[data-profile-tab=payments]').click()
    await expect(page.locator('#account-credit-orders .credit-order-row')).to_have_count(2)
    await expect(page.locator('#profile-panel-payments')).to_be_visible()
    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(width,'large balance overflow')
    await page.screenshot(path=f'/workspace/outputs/profile-payments-{width}.png',full_page=False)
    await page.locator('[data-profile-tab=analyses]').click()
    await expect(page.locator('#account-open-analyses .credit-opened')).to_have_count(3)
    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(width,'overview with large balance overflow')
    await expect(page.locator('#profile-open-count')).to_have_text('3')
    await page.evaluate('window.scrollTo(0,0)')
    await page.screenshot(path=f'/workspace/outputs/profile-overview-{width}.png',full_page=False)
    assert not errors,errors
    await page.locator('#account-logout').click();await expect(page.locator('#account-button')).to_have_text('Войти');await page.goto(base+'/#account')
    await expect(page.locator('#account-guest')).to_be_visible();await expect(page.locator('#account-content')).to_be_hidden()
    await ctx.close()
  finally:await browser.close()
 print('PASS: 1440/1024/390/320; compact profile, empty/large balance, promo, real password rotation, keyboard tabs, history and logout; no provider or AI calls')


def main():
 Path('/workspace/outputs').mkdir(exist_ok=True)
 with tempfile.TemporaryDirectory(prefix='profile-browser-') as folder:
  with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
  base='http://127.0.0.1:'+str(port)
  env=dict(os.environ,MATCHLAB_DATA_DIR=folder,PUBLIC_ORIGIN=base,LIVE_BILLING_FIXTURE='true',SUBSCRIPTION_BROWSER_FIXTURE='true',
     QWEN_BASE_URL='',QWEN_MODEL='',PYTHONPATH=str(ROOT/'tests')+os.pathsep+str(ROOT)+os.pathsep+os.environ.get('PYTHONPATH',''))
  with open('/tmp/profile-browser-server.log','w') as log:
   server=subprocess.Popen([sys.executable,'-m','uvicorn','billing_browser_fixture:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
   try:
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
     try:opener.open(base+'/api/auth/me',timeout=1).close();break
     except OSError:time.sleep(.1)
    else:raise RuntimeError('Profile fixture did not start')
    asyncio.run(exercise(base,folder))
   finally:server.terminate();server.wait(timeout=20)
if __name__=='__main__':main()
