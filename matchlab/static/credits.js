"use strict";
window.Credits=(()=>{
  const $=id=>document.getElementById(id),dialog=$('credits-dialog');
  const node=(tag,cls='',text='')=>{const e=document.createElement(tag);e.className=cls;e.textContent=text;return e;};
  const rub=kopecks=>(kopecks/100).toLocaleString('ru-RU',{maximumFractionDigits:2})+' ₽';
  const rules=new Intl.PluralRules('ru'),units=n=>`${n} ${{one:'разбор',few:'разбора',many:'разборов',other:'разбора'}[rules.select(n)]}`;
  let config=null,balance=0,owner=null,epoch=0,order=null,intent=null,pollTimer=null,checking=false,requestKey=null,openedOffset=0;
  const stateNames={creating:'Создаём платёж',pending:'Ожидаем оплату',checking:'Проверяем оплату',succeeded:'Оплачено',canceled:'Отменено',refunded:'Возврат',failed:'Платёж не создан'};
  const key=()=>owner?'matchlab:test-payment:'+owner:null;
  function remember(){try{if(key())order?sessionStorage.setItem(key(),JSON.stringify({id:order.id,analysis_id:order.analysis_id})):sessionStorage.removeItem(key());}catch{}}
  function header(){
    $('credits-open').hidden=!config?.enabled;
    $('credits-open').textContent=Account.user?`Разборы: ${balance}`:'Купить разборы';
    $('account-wallet').hidden=!config?.enabled||!Account.user;
    $('credits-balance').textContent=String(balance);$('account-credit-balance').textContent=units(balance);
    if(config?.enabled){$('account-bonus-note').textContent='Бонусные разборы входят в доступный баланс. За подготовку ответа баланс не списывается.';}
  }
  function showOrder(value){
    order=value;remember();$('credits-order').hidden=false;$('credits-packages-area').hidden=true;
    $('credits-order-title').textContent=stateNames[value.state]||'Проверяем оплату';
    $('credits-order-description').textContent=`${units(value.credits||0)} · ${rub(value.amount_kopecks||0)} · тестовая покупка`;
    $('credits-check').hidden=['succeeded','canceled','refunded','failed'].includes(value.state);
    $('credits-continue').hidden=!value.confirmation_url||!['pending','creating','checking'].includes(value.state);
    $('credits-continue').href=value.confirmation_url||'#';
    $('credits-order-open').hidden=value.state!=='succeeded'||!value.analysis_id;
    $('credits-order-open').textContent='Открыть прогноз за 1 разбор';
    $('credits-new-purchase').hidden=!['succeeded','canceled','refunded','failed'].includes(value.state);
    if(value.state==='succeeded')$('credits-message').textContent=`Покупка подтверждена: ${units(value.credits)}. Баланс: ${units(balance)}. Один ответ открывается за 1 разбор.`;
    if(value.state==='canceled')$('credits-message').textContent='Оплата отменена. Разборы не начислены.';
    if(value.state==='failed')$('credits-message').textContent='Тестовый платёж не создан. Проверьте настройки магазина или обратитесь в поддержку. Баланс не изменён.';
  }
  async function refresh(){
    await ready;await Account.ready;const serial=epoch;
    if(!config?.enabled||!Account.user){balance=0;header();return;}
    try{
      const data=await Account.api('/api/billing/wallet');if(serial!==epoch)return;
      balance=data.balance;header();
      $('account-bonus').textContent=`Бонусных разборов: ${data.bonus_balance}`;
      const list=$('account-credit-orders');list.replaceChildren();
      for(const value of data.orders){
        const row=node('div','credit-order-row'),text=node('div');
        text.append(node('strong','',`${units(value.credits)} · ${rub(value.amount_kopecks)}`),node('span','small muted',`${stateNames[value.state]||'Проверяем'} · ${new Date(value.created_at).toLocaleDateString('ru-RU',{timeZone:'Europe/Moscow'})}${value.refunded_kopecks?' · Возврат '+rub(value.refunded_kopecks):''}`));
        row.append(text);
        if(['creating','pending'].includes(value.state)){
          const button=node('button','text-button','Проверить');button.type='button';button.addEventListener('click',()=>{if(!dialog.open)dialog.showModal();showOrder(value);poll();});row.append(button);
        }
        list.append(row);
      }
      if(!data.orders.length)list.append(node('p','small muted','Покупок пока нет. Разборы появятся после подтверждения оплаты.'));
      $('account-credit-adjustment').textContent=data.adjustment_due?'Баланс скорректирован после возврата. Вопросы: m@krichigin.ru.':'';
      await loadOpened(true);
    }catch(error){if(serial===epoch)$('credits-message').textContent=error.message;}
  }
  async function loadOpened(reset=false){
    if(!Account.user||!config?.enabled)return;
    const serial=epoch,data=await Account.api('/api/billing/unlocks?offset='+(reset?0:openedOffset)+'&limit=20');if(serial!==epoch)return;
    const target=$('account-open-analyses');if(reset)target.replaceChildren();
    for(const item of data.items){const link=node('a','credit-opened',item.title);link.href=item.route;link.append(node('span','small muted','Открыто · повторный просмотр бесплатно'));target.append(link);}
    openedOffset=data.offset+data.items.length;$('account-open-more').hidden=openedOffset>=data.total;
    if(!data.total)target.append(node('p','small muted','Здесь будут сохранены разблокированные ответы ИИ.'));
  }
  function packages(){
    $('credits-packages').replaceChildren(...config.packages.map((p,i)=>{
      const label=node('label','credit-package'),radio=node('input');radio.type='radio';radio.name='credit-package';radio.value=p.id;radio.checked=i===0;
      const text=node('span');text.append(node('strong','',units(p.credits)),node('small','',rub(p.amount_kopecks/p.credits)+' за разбор'));
      label.append(radio,text,node('b','',rub(p.amount_kopecks)));return label;
    }));
  }
  async function open(identifier=null){
    await ready;if(!config?.enabled)return;
    if(!await Account.ensure(()=>open(identifier)))return;
    intent=identifier;await refresh();
    if(!dialog.open)dialog.showModal();
    $('credits-message').textContent='';$('credits-consent').checked=false;
    if(order&&['creating','pending','checking'].includes(order.state)){showOrder(order);poll();return;}
    order=null;requestKey=null;$('credits-order').hidden=true;$('credits-packages-area').hidden=false;packages();
    $('credits-pay').disabled=!config.checkout_enabled;
    if(!config.checkout_enabled)$('credits-message').textContent='Оплата временно недоступна. Уже купленные разборы можно использовать.';
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
    if(!$('credits-consent').checked){$('credits-message').textContent='Подтвердите условия тестовой покупки.';return;}
    const button=$('credits-pay');button.disabled=true;const serial=epoch;
    const selected=dialog.querySelector('input[name="credit-package"]:checked');
    if(!selected){button.disabled=false;return;}
    requestKey=requestKey||crypto.randomUUID().replaceAll('-','');
    try{
      const value=await Account.api('/api/billing/orders',{method:'POST',body:JSON.stringify({package_id:selected.value,request_key:requestKey,
        analysis_id:intent,offer_accepted:true,offer_version:config.offer_version})});
      if(serial!==epoch)return;showOrder(value);
      if(value.confirmation_url&&value.state==='pending'&&dialog.open)location.assign(value.confirmation_url);
      else poll();
    }catch(error){if(serial===epoch)$('credits-message').textContent=error.message;}
    finally{button.disabled=false;}
  });
  async function spend(identifier,callback){
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
      const button=node('button','primary-button','Разблокировать за 1 разбор');button.type='button';
      button.addEventListener('click',async()=>{button.disabled=true;message.textContent='';try{await spend(payload.id,callback);}catch(error){message.textContent=error.message;}finally{button.disabled=false;}});
      root.append(button,node('p','small muted','Повторное открытие этого ответа — бесплатно. При нулевом балансе можно купить пакет разборов.'));
    }else root.append(node('p','notice','Подтверждённого разбора ИИ пока нет. Разборы не списаны; платная разблокировка недоступна.'));
    root.append(message);target.replaceChildren(root);
  }
  $('credits-close').addEventListener('click',()=>dialog.close());
  $('credits-open').addEventListener('click',()=>open());$('account-credit-buy').addEventListener('click',()=>open());
  $('credits-check').addEventListener('click',poll);
  $('credits-order-open').addEventListener('click',async()=>{const b=$('credits-order-open');b.disabled=true;try{await spend(order.analysis_id);}catch(e){$('credits-message').textContent=e.message;}finally{b.disabled=false;}});
  $('credits-new-purchase').addEventListener('click',()=>{order=null;remember();open();});
  $('account-open-more').addEventListener('click',async()=>{const b=$('account-open-more');b.disabled=true;try{await loadOpened();}catch(e){$('account-message').textContent=e.message;}finally{b.disabled=false;}});
  const ready=Account.api('/api/billing/config').then(value=>{config=value;header();return value;}).catch(()=>{config={enabled:false};header();});
  document.addEventListener('accountchange',async()=>{
    epoch++;clearTimeout(pollTimer);checking=false;order=null;owner=Account.user?.id||null;balance=0;
    openedOffset=0;$('account-open-analyses').replaceChildren();$('account-credit-orders').replaceChildren();$('account-open-more').hidden=true;
    $('credits-message').textContent='';$('credits-order-title').textContent='';$('credits-order-description').textContent='';
    if(dialog.open)dialog.close();header();await refresh();
    if(!owner||!config?.enabled)return;
    try{
      const saved=JSON.parse(sessionStorage.getItem(key())||'null');
      if(saved?.id){order=saved;if(!dialog.open)dialog.showModal();poll();}
    }catch{}
  });
  Promise.all([ready,Account.ready]).then(()=>{owner=Account.user?.id||null;refresh();
    try{const saved=owner?JSON.parse(sessionStorage.getItem(key())||'null'):null;if(saved?.id){order=saved;dialog.showModal();poll();}}catch{}
  });
  return {ready,open,refresh,renderLocked,get enabled(){return !!config?.enabled;}};
})();
