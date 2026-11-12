"use strict";
window.Credits=(()=>{
  const $=id=>document.getElementById(id),dialog=$('credits-dialog');
  const node=(tag,cls='',text='')=>{const e=document.createElement(tag);e.className=cls;e.textContent=text;return e;};
  const rub=kopecks=>(kopecks/100).toLocaleString('ru-RU',{maximumFractionDigits:2})+' ₽';
  const rules=new Intl.PluralRules('ru'),units=n=>`${n} ${{one:'разбор',few:'разбора',many:'разборов',other:'разбора'}[rules.select(n)]}`;
  let config=null,balance=0,owner=null,epoch=0,order=null,intent=null,pollTimer=null,checking=false,requestKey=null,openedOffset=0;
  let unlimited=false,currentSubscription=null,selectedPlan=null,subscriptionKey=null;
  const when=s=>s?new Date(s).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+' МСК':'после подтверждения оплаты';
  const demonstration=()=>new URLSearchParams(location.search).get('billing_demo')==='1'&&Account.user?.role==='admin';
  const planName=p=>p.credits===0?'Безлимитные разборы':units(p.credits)+' каждый день';
  const stateNames={creating:'Создаём платёж',pending:'Ожидаем оплату',checking:'Проверяем оплату',succeeded:'Оплачено',canceled:'Отменено',refunded:'Возврат',failed:'Платёж не создан',review:'Платёж требует проверки'};
  const key=()=>owner?'matchlab:payment:'+(config?.test_mode?'test':'live')+':'+owner:null;
  function remember(){try{if(key())order?sessionStorage.setItem(key(),JSON.stringify({id:order.id,analysis_id:order.analysis_id})):sessionStorage.removeItem(key());}catch{}}
  function header(){
    const demo=demonstration();$('billing-demo-banner').hidden=!demo;$('account-credit-buy').disabled=demo;
    const testing=!!config?.test_checkout_allowed,preview=config?.sales_state==='prelaunch',available=!!config?.checkout_enabled;
    $('credits-open').hidden=!config?.enabled&&!preview;
    $('credits-open').disabled=demo;
    $('credits-open').textContent=Account.user&&config?.enabled?(unlimited?'Безлимит активен':`Разборы: ${balance}`):'Тарифы';
    $('account-wallet').hidden=!config?.enabled||!Account.user;
    $('credits-balance').textContent=String(balance);$('account-credit-balance').replaceChildren(node('strong','profile-balance-value',Number(balance).toLocaleString('ru-RU')),node('span','profile-balance-unit',units(balance).slice(String(balance).length)));$('account-credit-balance').setAttribute('aria-label',units(balance));$('account-unlimited').hidden=!unlimited;$('profile-credit-lifetime').hidden=unlimited;
    $('credits-mode').textContent=testing?'Проверка интеграции · тестовый магазин':'Разборы и подписки';
    $('credits-launch-note').hidden=available;
    $('credits-launch-note').textContent='Оплата временно недоступна. Купленные разборы можно использовать.';
    $('credits-consent-row').hidden=!available;
    $('credits-email-row').hidden=!config?.receipt_email_required;
    $('credits-email').required=!!config?.receipt_email_required;
    $('credits-offer-link').href=config?.offer_url||'/static/legal/offer.html';
    $('credits-offer-link').textContent=testing?'условия проверки оплаты':'публичную оферту';
    $('credits-consent-comment').textContent=testing?'Реальные деньги в тестовом магазине не списываются.':'';
    $('credits-pay').disabled=!config?.checkout_enabled;
    $('credits-pay').textContent=available?'Перейти к оплате':'Оплата временно недоступна';
    $('account-credit-buy').textContent=available?'Купить разборы':'Тарифы и условия';
    $('account-payment-mode').hidden=!testing;
    $('account-payment-mode').textContent=testing?'Проверка интеграции · тестовый магазин, реальные деньги не списываются.':'';
    if(demo){$('credits-open').textContent='Демо · 5 разборов';$('account-credit-buy').textContent='Покупки отключены в демонстрации';}
    if(config?.enabled){$('account-bonus-note').textContent='Разбор списывается ТОЛЬКО за открытие решения «Ставить». «Пропустить» и подготовка ответа бесплатны. Бонусы входят в доступный баланс.';}
  }
  function showOrder(value){
    order=value;remember();$('credits-order').hidden=false;$('credits-packages-area').hidden=true;
    $('subscription-plans-area').hidden=true;$('subscription-checkout').hidden=true;$('credits-one-time').hidden=true;
    $('credits-order-title').textContent=stateNames[value.state]||'Проверяем оплату';
    $('credits-order-description').textContent=`${value.subscription_id?planName(value)+' · оплата каждые 24 часа':units(value.credits||0)} · ${rub(value.amount_kopecks||0)}${value.test_mode?' · Проверочная операция без списания денег':''}`;
    $('credits-check').hidden=['succeeded','canceled','refunded','failed'].includes(value.state);
    $('credits-continue').hidden=!value.confirmation_url||!['pending','creating','checking'].includes(value.state);
    $('credits-continue').href=value.confirmation_url||'#';
    $('credits-order-open').hidden=value.state!=='succeeded'||!value.analysis_id;
    $('credits-order-open').textContent='Открыть прогноз за 1 разбор';
    $('credits-new-purchase').hidden=!['succeeded','canceled','refunded','failed'].includes(value.state);
    if(value.state==='succeeded')$('credits-message').textContent=value.subscription_id&&value.credits===0?'Безлимит оплачен на 24 часа. Управление и отмена — в личном кабинете.':`Начислено: ${units(value.credits)}. Баланс: ${units(balance)}. Разбор списывается ТОЛЬКО за «Ставить». «Пропустить» бесплатно.`;
    if(value.state==='canceled')$('credits-message').textContent='Оплата отменена. Разборы не начислены.';
    if(value.state==='review')$('credits-message').textContent='Статус оплаты требует проверки поддержки. Напишите m@krichigin.ru, повторно не оплачивайте.';
    if(value.state==='failed')$('credits-message').textContent='Платёж не создан. Повторите попытку позже или обратитесь в поддержку. Баланс не изменён.';
  }
  async function refresh(){
    await ready;await Account.ready;const serial=epoch;
    if(!config?.enabled||!Account.user){balance=0;header();return;}
    try{
      const data=await Account.api(demonstration()?'/api/admin/billing/subscription-demo':'/api/billing/wallet');if(serial!==epoch)return;
      balance=data.balance;unlimited=!!data.subscription?.unlimited_active;currentSubscription=data.subscription?.current||null;header();renderSubscription();
      if(config.receipt_email_required&&!$('credits-email').value&&data.receipt_email)$('credits-email').value=data.receipt_email;
      $('account-bonus').textContent=`Бонусных разборов: ${data.bonus_balance}`;
      const list=$('account-credit-orders');list.replaceChildren();
      for(const value of data.orders){
        const row=node('div','credit-order-row'),text=node('div');
        text.append(node('strong','',value.subscription_id?planName(value):units(value.credits)),node('span','small muted',new Date(value.created_at).toLocaleDateString('ru-RU',{timeZone:'Europe/Moscow'})+(value.test_mode?' · Проверочная операция':'')));
        const meta=node('div','profile-payment-meta'),status=node('span','profile-payment-status',stateNames[value.state]||'Проверяем');status.dataset.state=value.state;
        meta.append(node('strong','profile-payment-amount',rub(value.amount_kopecks)),status);
        if(value.refunded_kopecks)text.append(node('span','small muted','Возврат '+rub(value.refunded_kopecks)));
        row.append(text,meta);
        if(['creating','pending'].includes(value.state)){
          const button=node('button','text-button','Проверить');button.type='button';button.addEventListener('click',()=>{if(!dialog.open)dialog.showModal();showOrder(value);poll();});row.append(button);
        }
        list.append(row);
      }
      if(!data.orders.length)list.append(empty('Здесь будет история оплат',demonstration()?'В демонстрации реальные покупки не отображаются.':'Все покупки и продления появятся здесь после оформления заказа.'));
      if(!config.test_checkout_allowed&&data.orders.some(value=>value.test_mode)){
        $('account-payment-mode').hidden=false;$('account-payment-mode').textContent='В истории есть проверочные операции без списания денег. Они не являются покупками по коммерческой оферте.';
      }
      $('account-credit-adjustment').textContent=data.adjustment_due?'Баланс скорректирован после возврата. Вопросы: m@krichigin.ru.':'';
      if(demonstration()){$('account-open-analyses').replaceChildren(node('p','small muted','Демонстрация не меняет доступ к сохранённым ответам.'));$('account-open-more').hidden=true;}else await loadOpened(true);
    }catch(error){if(serial===epoch)$('credits-message').textContent=error.message;}
  }
  async function loadOpened(reset=false){
    if(!Account.user||!config?.enabled)return;
    const serial=epoch,data=await Account.api('/api/billing/unlocks?offset='+(reset?0:openedOffset)+'&limit=5');if(serial!==epoch)return;
    const target=$('account-open-analyses');if(reset)target.replaceChildren();
    for(const item of data.items){const link=node('a','credit-opened',item.title);link.href=item.route;link.append(node('span','small muted','Открыто · повторный просмотр бесплатно'),ProductUX.icon('arrow'));target.append(link);}
    openedOffset=data.offset+data.items.length;$('account-open-more').hidden=openedOffset>=data.total;
    $('profile-open-count').hidden=!data.total;$('profile-open-count').textContent=String(data.total);
    if(!data.total)target.append(empty('Первый разбор — впереди','Выберите интересующий матч. Открытый ответ сохранится здесь.',true));
  }
  function empty(title,description,link=false){
    const box=node('div','profile-empty'),text=node('div');const symbol=node('span','profile-empty-symbol');symbol.append(ProductUX.icon('history'));box.append(symbol);text.append(node('strong','',title),node('p','',description));
    if(link){const a=node('a','','Выбрать матч');a.href='#matches';a.append(ProductUX.icon('arrow'));text.append(a);}box.append(text);return box;
  }
  function packages(){
    $('credits-packages').replaceChildren(...config.packages.map((p,i)=>{
      const label=node('label','credit-package'),radio=node('input');radio.type='radio';radio.name='credit-package';radio.value=p.id;radio.checked=i===0;
      const text=node('span');text.append(node('strong','',units(p.credits)),node('small','',rub(p.amount_kopecks/p.credits)+' за разбор'));
      label.append(radio,text,node('b','',rub(p.amount_kopecks)));return label;
    }));
  }
  async function open(identifier=null){
    await ready;await Account.ready;if(!config)return;
    if(demonstration()){location.hash='#account';return;}
    await loadConfig();
    if(config.checkout_enabled&&!await Account.ensure(()=>open(identifier)))return;
    const serial=epoch;
    intent=identifier;await refresh();
    if(serial!==epoch)return;
    if(!dialog.open)dialog.showModal();
    $('credits-message').textContent='';$('credits-consent').checked=false;
    if(order&&['creating','pending','checking'].includes(order.state)){showOrder(order);poll();return;}
    order=null;requestKey=null;$('credits-order').hidden=true;$('credits-packages-area').hidden=false;packages();
    $('subscription-plans-area').hidden=false;$('subscription-checkout').hidden=true;$('credits-one-time').hidden=false;$('credits-one-time').open=false;plans();
    $('credits-pay').disabled=!config.checkout_enabled;
    if(!config.checkout_enabled)$('credits-message').textContent='Оплата временно недоступна. Здесь можно ознакомиться с тарифами и условиями. Доступные разборы на балансе можно использовать.';
  }
  async function poll(){
    if(checking||!order||!Account.user)return;clearTimeout(pollTimer);const serial=epoch,id=order.id;checking=true;
    try{
      const value=await Account.api('/api/billing/orders/'+id);if(serial!==epoch||order?.id!==id)return;
      await refresh();if(serial!==epoch)return;showOrder(value);
      if(['creating','pending','checking'].includes(value.state))pollTimer=setTimeout(poll,Math.max(4,value.retry_after||4)*1000);
      else{try{if(key())sessionStorage.removeItem(key());}catch{}}
    }catch(error){if(serial===epoch)$('credits-message').textContent=error.message;}
    finally{checking=false;}
  }
  $('credits-pay').addEventListener('click',async()=>{
    if(!config?.checkout_enabled){$('credits-message').textContent='Оплата временно недоступна.';return;}
    if(!$('credits-consent').checked){$('credits-message').textContent='Подтвердите условия приобретения разборов.';return;}
    if(config.receipt_email_required&&!$('credits-email').reportValidity())return;
    const button=$('credits-pay');button.disabled=true;const serial=epoch;
    const selected=dialog.querySelector('input[name="credit-package"]:checked');
    if(!selected){button.disabled=false;return;}
    requestKey=requestKey||crypto.randomUUID().replaceAll('-','');
    try{
      const value=await Account.api('/api/billing/orders',{method:'POST',body:JSON.stringify({package_id:selected.value,request_key:requestKey,
        analysis_id:intent,offer_accepted:true,offer_version:config.offer_version,receipt_email:config.receipt_email_required?$('credits-email').value.trim():undefined})});
      if(serial!==epoch)return;showOrder(value);
      if(value.confirmation_url&&value.state==='pending'&&dialog.open)location.assign(value.confirmation_url);
      else poll();
    }catch(error){if(serial===epoch)$('credits-message').textContent=error.message;}
    finally{button.disabled=false;}
  });
  async function spend(identifier,callback){
    if(demonstration())throw new Error('Для работы с реальным балансом выйдите из демонстрации.');
    if(!await Account.ensure(()=>spend(identifier,callback)))return;
    const accountId=Account.user.id;let value;
    try{value=await Account.api('/api/billing/analyses/'+identifier+'/unlock',{method:'POST',body:'{}'});}
    catch(error){if(error.status===402){await open(identifier);return;}throw error;}
    if(Account.user?.id!==accountId)return;await refresh();
    if(dialog.open)dialog.close();
    if(callback)await callback();
    else{const route=value.route||'#result/'+identifier;if(location.hash===route)dispatchEvent(new HashChangeEvent('hashchange'));else location.hash=route;}
  }
  function renderLocked(target,payload,callback){
    target.classList.remove('verdict-report','structured-report');
    const root=node('section','credit-gate');root.dataset.analysisId=payload.id;
    if(payload.decision==='skip')root.append(node('h2','credit-skip','Рекомендуем пропустить матч'));
    else root.append(node('h2','','Разбор готов'));
    const skeleton=node('div','credit-skeleton');skeleton.setAttribute('aria-hidden','true');
    skeleton.append(node('strong','','Претендент на победу'),node('p','','Подробный ответ ИИ и аргументы по данным участников'),node('p','','Сравнение результатов, встречные доводы и итоговое решение'));
    root.append(skeleton,node('p','',payload.decision==='skip'?'Претендент на победу по мнению ИИ и обоснование пропуска доступны в полном разборе.':'Откройте полный ответ: выбор ИИ, аргументы и обоснование решения.'));
    const message=node('p','account-message');message.setAttribute('role','status');
    if(payload.unlock.available){
      const preview=!config?.checkout_enabled&&!(payload.unlock.balance>=1)&&!payload.unlock.unlimited;
      const button=node('button','primary-button',payload.unlock.unlimited?'Открыть по подписке':preview?'Посмотреть тарифы':'Разблокировать за 1 разбор');button.type='button';
      button.addEventListener('click',async()=>{button.disabled=true;message.textContent='';try{if(preview)await open(payload.id);else await spend(payload.id,callback);}catch(error){message.textContent=error.message;}finally{button.disabled=false;}});
      root.append(button,node('p','credit-rule','Разбор списывается ТОЛЬКО за решение «Ставить». «Пропустить» бесплатно.'),node('p','small muted',payload.unlock.unlimited?'Безлимит активен: накопленный баланс не расходуется.':'Повторное открытие этого ответа — бесплатно. При нулевом балансе выберите тариф.'));
    }else root.append(node('p','notice','Подтверждённого разбора ИИ пока нет. Разборы не списаны; платная разблокировка недоступна.'));
    root.append(message);target.replaceChildren(root);
  }
  $('credits-close').addEventListener('click',()=>dialog.close());
  $('credits-open').addEventListener('click',()=>open());$('account-credit-buy').addEventListener('click',()=>open());
  $('credits-check').addEventListener('click',poll);
  $('credits-order-open').addEventListener('click',async()=>{const b=$('credits-order-open');b.disabled=true;try{await spend(order.analysis_id);}catch(e){$('credits-message').textContent=e.message;}finally{b.disabled=false;}});
  $('credits-new-purchase').addEventListener('click',()=>{order=null;remember();open();});
  $('account-open-more').addEventListener('click',async()=>{const b=$('account-open-more');b.disabled=true;try{await loadOpened();}catch(e){$('account-message').textContent=e.message;}finally{b.disabled=false;}});
  async function loadConfig(){
    const serial=epoch;
    try{const value=await Account.api('/api/billing/config');if(serial===epoch){config=value;header();}return value;}
    catch{if(serial===epoch){config={enabled:false};header();}}
  }
  function plans(){
    $('subscription-launch').hidden=!!config?.subscriptions_enabled;
    $('subscription-plans').replaceChildren(...(config?.subscription_plans||[]).map(p=>{
      const card=node('article','subscription-plan'+(p.id==='daily_five'?' subscription-plan-featured':''));
      card.append(node('p','subscription-plan-caption',p.credits===0?'Без ограничений':'Накапливаются без срока действия'));
      const quantity=node('div','subscription-quantity');quantity.append(node('strong','',p.credits===0?'∞':String(p.credits)),node('small','','/1 день'));
      card.append(quantity,node('h3','',p.credits===0?'Безлимитные разборы':p.credits===1?'Разбор':'Разборов'));
      const price=node('p','subscription-price');price.append(node('strong','',rub(p.amount_kopecks)),node('span','','каждый день'));
      card.append(price,node('p','small muted',p.credits===0?'Полный доступ на 24 часа. Накопленный баланс не тратится.':'Каждая оплата пополняет баланс. Остаток сохраняется после отмены.'));
      const b=node('button','primary-button','Подписаться');b.type='button';b.disabled=!config?.subscriptions_enabled;
      b.setAttribute('aria-label','Подписаться: '+planName(p)+', '+rub(p.amount_kopecks)+' каждый день');
      b.addEventListener('click',()=>choosePlan(p));card.append(b);return card;
    }));
  }
  async function choosePlan(p){
    if(!await Account.ensure(()=>choosePlan(p)))return;
    selectedPlan=p;subscriptionKey=null;
    $('subscription-plans-area').hidden=true;$('credits-one-time').hidden=true;$('subscription-checkout').hidden=false;$('credits-message').textContent='';
    $('subscription-checkout-title').textContent=planName(p);
    $('subscription-checkout-price').textContent=rub(p.amount_kopecks)+' сейчас, затем '+rub(p.amount_kopecks)+' каждые 24 часа';
    $('subscription-consent-text').textContent=p.consent_text;
    $('subscription-offer').checked=false;$('subscription-recurring').checked=false;
    if(!$('subscription-email').value)$('subscription-email').value=$('credits-email').value;
    $('subscription-email').focus();
  }
  function renderSubscription(){
    const s=currentSubscription,area=$('subscription-summary'),confirmation=area.querySelector('.subscription-cancel-confirm');
    $('account-subscription').hidden=!Account.user||!config?.enabled;$('account-subscription').dataset.state=s?.state||'none';area.replaceChildren();
    $('subscription-cancel').hidden=!s?.auto_renew;$('subscription-choose').hidden=!!s;$('profile-subscription-terms').hidden=!s;
    if(!s){
      $('subscription-name').textContent='Нет активной подписки';$('subscription-state').textContent='Не подключена';$('subscription-state').dataset.state='none';
      area.append(node('p','',config?.subscriptions_enabled?'Выберите ежедневный тариф или безлимит. Накопленные разборы не сгорают.':'Подписки скоро откроются. Накопленные разборы доступны без ограничений по сроку.'));
      $('subscription-choose').textContent='Тарифы';$('subscription-choose').setAttribute('aria-label','Посмотреть тарифы');return;
    }
    $('subscription-name').textContent=planName(s);
    const status={creating:'Ожидаем первый платёж',active:'Подписка действует',canceled:'Автопродление отключено',past_due:'Оплата не прошла',review:'Требуется проверка'};
    $('subscription-state').textContent=status[s.state]||'Проверяем';$('subscription-state').dataset.state=s.state;
    if(demonstration())area.append(node('p','notice','Демонстрация интерфейса: настоящая карта не привязана, платежи не выполняются.'));
    const facts=node('dl','profile-subscription-facts');
    const fact=(label,value)=>{const d=node('div');d.append(node('dt','',label),node('dd','',value));facts.append(d);};
    fact('Стоимость',rub(s.amount_kopecks)+' / 24 ч');
    fact(s.auto_renew?'Следующее списание':'Оплаченный период до',s.period_end_at?when(s.period_end_at):'После подтверждения оплаты');
    area.append(facts,node('p','small muted',s.credits?`При каждой оплате +${units(s.credits)} на баланс. Разборы не сгорают.`:'Безлимит действует в оплаченном периоде. Накопленные разборы не тратятся.'));
    if(!s.auto_renew)area.append(node('p','small muted','Новых автоматических списаний не будет.'));
    if(s.error_code==='payment_refunded')area.append(node('p','notice','По оплате оформлен возврат. Остальные накопленные разборы сохраняются.'));
    if(s.error_code==='payment_method_not_saved')area.append(node('p','notice','ЮKassa не сохранила способ оплаты. Оплаченный доступ предоставлен, повторных списаний не будет.'));
    if(s.state==='review')area.append(node('p','notice','Не оплачивайте повторно. Напишите m@krichigin.ru — проверим исходный платёж. Накопленные разборы доступны.'));
    if(s.canceled_at)area.append(node('p','credit-rule','Карта отвязана от автоплатежей сервиса.'));
    if(confirmation&&s.auto_renew&&confirmation.dataset.subscriptionId===s.id)area.append(confirmation);
  }
  $('subscription-choose').addEventListener('click',()=>open());
  $('subscription-back').addEventListener('click',()=>{
    selectedPlan=null;subscriptionKey=null;$('subscription-checkout').hidden=true;$('subscription-plans-area').hidden=false;$('credits-one-time').hidden=false;$('credits-message').textContent='';
  });
  $('subscription-pay').addEventListener('click',async()=>{
    if(!selectedPlan||!config?.subscriptions_enabled)return;
    if(!$('subscription-offer').checked||!$('subscription-recurring').checked){$('credits-message').textContent='Подтвердите оферту и отдельно — ежедневные автоматические списания.';return;}
    if(!$('subscription-email').reportValidity())return;
    const b=$('subscription-pay'),serial=epoch;b.disabled=true;
    subscriptionKey=subscriptionKey||crypto.randomUUID().replaceAll('-','');
    try{
      const value=await Account.api('/api/billing/subscriptions',{method:'POST',body:JSON.stringify({plan_id:selectedPlan.id,request_key:subscriptionKey,
        receipt_email:$('subscription-email').value.trim(),offer_accepted:true,offer_version:config.offer_version,
        recurring_accepted:true,recurring_version:config.recurring_version})});
      if(serial!==epoch)return;showOrder(value);
      if(value.confirmation_url&&value.state==='pending')location.assign(value.confirmation_url);else poll();
    }catch(error){if(serial===epoch)$('credits-message').textContent=error.message;}
    finally{b.disabled=false;}
  });
  $('subscription-cancel').addEventListener('click',()=>{
    const area=$('subscription-summary');
    if(area.querySelector('.subscription-cancel-confirm'))return;
    const box=node('div','subscription-cancel-confirm');box.dataset.subscriptionId=currentSubscription.id;box.append(node('strong','','Отвязать карту и отключить автоплатежи?'),node('p','','Удалим привязку способа оплаты к подписке и остановим новые автоплатежи. Накопленные разборы и открытые ответы сохранятся; оплаченный безлимит действует до конца периода. Уже переданный банку платёж проверим отдельно — он не подключит подписку заново.'));
    const yes=node('button','secondary-button','Да, отвязать карту'),no=node('button','text-button','Оставить карту и подписку');yes.type=no.type='button';
    no.addEventListener('click',()=>box.remove());yes.addEventListener('click',async()=>{
      yes.disabled=true;const serial=epoch,id=currentSubscription?.id;
      try{await Account.api(demonstration()?'/api/admin/billing/subscription-demo/cancel':'/api/billing/subscriptions/'+id+'/cancel',{method:'POST',body:'{}'});if(serial!==epoch)return;
        await refresh();$('subscription-message').textContent='Карта отвязана. Автопродление отключено. Накопленные разборы не сгорают.';
      }catch(e){if(serial===epoch)$('subscription-message').textContent=e.message;}
      finally{yes.disabled=false;}
    });box.append(yes,no);area.append(box);yes.focus();
  });
  $('billing-demo-reset').addEventListener('click',async()=>{const b=$('billing-demo-reset');b.disabled=true;try{await Account.api('/api/admin/billing/subscription-demo/reset',{method:'POST',body:'{}'});await refresh();$('subscription-message').textContent='';}catch(e){$('subscription-message').textContent=e.message;}finally{b.disabled=false;}});
  setInterval(()=>{if(Account.user&&location.hash==='#account'&&!document.hidden)refresh();},60000);
  const ready=loadConfig();
  document.addEventListener('accountchange',async()=>{
    epoch++;clearTimeout(pollTimer);checking=false;order=null;owner=Account.user?.id||null;balance=0;unlimited=false;currentSubscription=null;selectedPlan=null;subscriptionKey=null;
    $('account-subscription').hidden=true;$('subscription-email').value='';$('subscription-offer').checked=false;$('subscription-recurring').checked=false;$('subscription-message').textContent='';
    $('credits-email').value='';
    openedOffset=0;$('profile-open-count').hidden=true;$('profile-open-count').textContent='';$('account-open-analyses').replaceChildren();$('account-credit-orders').replaceChildren();$('account-open-more').hidden=true;
    $('credits-message').textContent='';$('credits-order-title').textContent='';$('credits-order-description').textContent='';
    if(dialog.open)dialog.close();header();await loadConfig();await refresh();
    if(!owner||!config?.enabled||demonstration())return;
    try{
      const saved=JSON.parse(sessionStorage.getItem(key())||'null');
      if(saved?.id){order=saved;if(!dialog.open)dialog.showModal();poll();}
    }catch{}
  });
  Promise.all([ready,Account.ready]).then(()=>{owner=Account.user?.id||null;refresh();if(demonstration())return;
    try{const saved=owner?JSON.parse(sessionStorage.getItem(key())||'null'):null;if(saved?.id){order=saved;dialog.showModal();poll();}}catch{}
  });
  return {ready,open,refresh,renderLocked,get enabled(){return !!config?.enabled;}};
})();
