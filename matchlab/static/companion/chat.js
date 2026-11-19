'use strict';
const el = id => document.getElementById(id);
let session, revision = 0, mode = '', pending = false, retry = null;
async function api(path, method='GET', body) {
  const options = {method, headers: {'Content-Type':'application/json','X-CSRF-Token':session?.csrf || ''}};
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await fetch('/api/'+path, options);
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.detail || 'Запрос не выполнен.'),{status:response.status});
  return data;
}
function openSheet(title, box) {
  el('sheetTitle').textContent=title;
  for (const id of ['account','loginBox','register']) el(id).hidden=id!==box;
  el('notice').textContent='';
  if (!el('sheet').open) el('sheet').showModal();
}
function agreements(next) {
  mode=next;
  openSheet(next==='register'?'Создать аккаунт':'Условия общения','register');
  el('register').reset();
  el('credentials').hidden=next!=='register';
  for (const input of el('credentials').querySelectorAll('input')) input.disabled=next!=='register';
  el('agree').textContent=next==='register'?'Создать аккаунт':'Продолжить';
}
function draw(data) {
  revision=data.revision;
  const list=el('messages');
  list.replaceChildren();
  for (const turn of data.turns) {
    for (const [key,cls] of [['user','bubble mine'],['assistant','bubble']]) {
      const p=document.createElement('p'); p.className=cls; p.textContent=turn[key]; list.append(p);
    }
  }
  el('welcome').hidden=data.turns.length>0;
  el('thread').scrollTop=el('thread').scrollHeight;
}
async function refresh() {
  session=await api('auth/me');
  el('profile').textContent=session.user?'Профиль':'Войти';
  if (session.user) {
    const data=await api('chat'); draw(data);
    if (!data.accepted) agreements('confirm');
  } else {
    draw({revision:0,turns:[]});
    const providers=el('providers'); providers.replaceChildren();
    for (const p of session.providers.filter(x=>x!=='local')) {
      const button=document.createElement('button');
      button.textContent={yandex:'Яндекс',vk:'ВКонтакте',ok:'Одноклассники',mail:'Mail.ru'}[p];
      button.onclick=guard(async()=>{const result=await api('auth/social/'+p+'/start','POST',{action:'login'}); location.assign(result.authorization_url);});
      providers.append(button);
    }
  }
}
function guard(fn) {
  return async e=>{e?.preventDefault();try{await fn(e);}catch(err){el(el('sheet').open?'notice':'status').textContent=err.message;}};
}
el('profile').onclick=guard(async()=>{
  if (!session?.user) {openSheet('Войти в чат','loginBox');return;}
  openSheet('Профиль','account'); el('identity').textContent=session.user.display_name||session.user.username;
  const box=el('payments'); box.replaceChildren();
  const wallet=await api('billing/wallet');
  if (wallet.balance) {const p=document.createElement('p'); p.textContent='Баланс прежнего сервиса: '+wallet.balance; box.append(p);}
  if (wallet.orders.length) {
    const details=document.createElement('details'), title=document.createElement('summary');
    title.textContent='История платежей';details.append(title);
    const states={succeeded:'Оплачен',pending:'Ожидает оплаты',refunded:'Возврат',canceled:'Отменён'};
    for(const order of wallet.orders){
      const p=document.createElement('p');
      p.textContent=new Date(order.created_at).toLocaleDateString('ru-RU')+' · '+(order.amount_kopecks/100).toLocaleString('ru-RU')+' ₽ · '+(states[order.state]||'В обработке');
      details.append(p);
    }
    box.append(details);
  }
  for (const sub of wallet.subscription?.history||[]) {
    if (!sub.auto_renew) continue;
    const button=document.createElement('button');button.textContent='Отвязать карту подписки '+sub.plan_id;
    button.onclick=guard(async()=>{if(confirm('Отменить продление и отвязать способ оплаты?')){await api('billing/subscriptions/'+sub.id+'/cancel','POST',{});button.remove();el('notice').textContent='Продление отменено.';}});
    box.append(button);
  }
});
el('registerToggle').onclick=()=>agreements('register');
el('login').onsubmit=guard(async()=>{await api('auth/login','POST',Object.fromEntries(new FormData(el('login'))));el('login').reset();el('sheet').close();await refresh();});
el('register').onsubmit=guard(async()=>{const path={confirm:'auth/confirm-documents',social:'auth/social/complete',register:'auth/register'}[mode];
  await api(path,'POST',{...Object.fromEntries(new FormData(el('register'))),age_confirmed:true,terms_accepted:true,personal_data_consent:true,legal_version:session.legal_version});
  el('register').reset();el('sheet').close();await refresh();
});
el('logout').onclick=guard(async()=>{await api('auth/logout','POST',{});location.replace('/');});
el('clear').onclick=guard(async()=>{if(confirm('Удалить всю историю общения с Мирой?')){await api('chat','DELETE');el('sheet').close();retry=null;await refresh();}});
el('compose').onsubmit=guard(async()=>{
  if (!session?.user){openSheet('Войти в чат','loginBox');return;}
  if (pending) return;
  const message=el('message').value.trim();if(!message)return;
  if (!retry || retry.message!==message || retry.revision!==revision) retry={message,revision,id:crypto.randomUUID()};
  pending=true;el('send').disabled=true;el('message').readOnly=true;el('profile').disabled=true;el('status').textContent='Мира печатает…';
  try {
    draw(await api('chat','POST',retry));retry=null;el('message').value='';el('status').textContent='';
  } catch(err){if([401,403,409].includes(err.status))await refresh();throw err;}
  finally{pending=false;el('send').disabled=false;el('message').readOnly=false;el('profile').disabled=false;el('message').focus();}
});
el('message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();el('compose').requestSubmit();}};
for(const button of el('starters').children)button.onclick=()=>{el('message').value=button.textContent;el('message').focus();};
guard(async()=>{await refresh();const social=new URLSearchParams(location.search).get('social');
  if(social==='complete'){await api('auth/social/pending');agreements('social');}
  else if(social && social!=='success')el('status').textContent='Вход не завершён. Попробуйте снова.';
  if(social)history.replaceState(null,'','/');
})();
