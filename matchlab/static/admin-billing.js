"use strict";
window.AdminBilling=(()=>{
  const $=id=>document.getElementById(id),root=$('admin-billing'),dialog=$('ab-detail');
  const node=(tag,cls='',text='')=>{const e=document.createElement(tag);e.className=cls;e.textContent=text;return e;};
  const money=n=>n==null?'—':(n/100).toLocaleString('ru-RU',{style:'currency',currency:'RUB',maximumFractionDigits:2});
  const num=n=>Number(n||0).toLocaleString('ru-RU');
  const unitRules=new Intl.PluralRules('ru'),units=n=>`${num(n)} ${{one:'разбор',few:'разбора',many:'разборов',other:'разбора'}[unitRules.select(n)]}`;
  const when=s=>s?new Date(s).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
  const names={creating:'Создаётся',pending:'Ожидает оплаты',succeeded:'Оплачен',canceled:'Отменён',refunded:'Возвращён',failed:'Ошибка создания'};
  const kinds={payment:'Начисление за оплату',unlock:'Открытие разбора',bonus_unlock:'Открытие за бонус',refund:'Корректировка после возврата'};
  let offset=0,timer,controller,detailController,epoch=0,detailEpoch=0,current=null,detailId=null,detailOffset=0;
  const active=()=>location.hash==='#admin'&&!root.hidden&&Account.user?.role==='admin';
  function stop(){clearTimeout(timer);controller?.abort();detailController?.abort();epoch++;detailEpoch++;root.removeAttribute('aria-busy');if(dialog.open)dialog.close();}
  const badge=state=>node('span','ab-badge ab-'+state,names[state]||state);
  function metric(label,value,note){const box=node('article','ar-metric');box.append(node('span','',label),node('strong','',value),node('small','',note));return box;}
  function table(headers,rows){
    if(!rows.length)return node('p','ar-chart-empty','Записей по этим условиям пока нет.');
    const t=node('table','admin-table'),head=node('thead'),hr=node('tr'),body=node('tbody');
    headers.forEach(h=>hr.append(node('th','',h)));head.append(hr);
    for(const values of rows){const tr=node('tr');for(const value of values){const td=node('td');td.append(value instanceof Node?value:document.createTextNode(String(value??'—')));tr.append(td);}body.append(tr);}
    t.append(head,body);return t;
  }
  function button(text,fn,cls='text-button'){const b=node('button',cls,text);b.type='button';b.addEventListener('click',fn);return b;}
  function chart(data){
    const box=$('ab-chart');box.replaceChildren();
    if(!data.length){box.append(node('p','ar-chart-empty','График появится после первого заказа.'));return;}
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 720 210');svg.setAttribute('role','img');svg.setAttribute('aria-label','Заказы по дате создания: оплаченные и остальные');
    const max=Math.max(1,...data.map(d=>d.orders)),step=660/data.length;
    for(const [i,d] of data.entries()){
      const x=40+i*step,w=Math.max(1,Math.min(40,step-3)),y=170-d.orders/max*140;
      const group=document.createElementNS(ns,'g');group.setAttribute('tabindex','0');group.setAttribute('aria-label',`${d.day}: заказов ${d.orders}, оплачено ${d.paid}, сумма ${money(d.gross_kopecks)}`);
      const title=document.createElementNS(ns,'title');title.textContent=group.getAttribute('aria-label');group.append(title);
      for(const [height,top,cls] of [[d.orders/max*140,y,'ab-bar-other'],[d.paid/max*140,170-d.paid/max*140,'ab-bar-paid']]){
        const rect=document.createElementNS(ns,'rect');for(const [key,value] of Object.entries({x,y:top,width:w,height,rx:3}))rect.setAttribute(key,String(value));rect.setAttribute('class',cls);group.append(rect);
      }
      svg.append(group);
    }
    for(const [text,x] of [[String(max),6],[data[0].day,40],[data.at(-1).day,600]]){const t=document.createElementNS(ns,'text');t.setAttribute('x',String(x));t.setAttribute('y',x===6?'28':'200');t.textContent=text;svg.append(t);}
    box.append(svg);
  }
  function render(d){
    current=d;const m=d.metrics,test=d.mode==='test';$('ab-notice').textContent=d.notice;
    $('ab-metrics').replaceChildren(
      metric(test?'Тестовая сумма оплат':'Сумма оплат',money(m.gross_kopecks),`Оплаченных заказов: ${num(m.paid)} · покупателей: ${num(m.buyers)}`),
      metric('Возвраты',money(m.refunded_kopecks),'По выбранным заказам, включая частичные'),
      metric('После возвратов',money(m.net_kopecks),test?'Проверочные суммы, не реальные деньги':'Сумма оплат минус возвраты, до расходов'),
      metric('Средний чек',money(m.average_check_kopecks),'По оплаченным заказам, до возвратов'),
      metric('Заказов',num(m.orders),`Ожидают: ${num(m.pending)} · отменены: ${num(m.canceled)} · ошибки: ${num(m.failed)}`),
      metric('Оплачено заказов',m.payment_rate==null?'—':(m.payment_rate*100).toLocaleString('ru-RU',{maximumFractionDigits:1})+'%',`Начислено за оплаты: ${num(m.credited_units)} разборов, до возвратов`));
    chart(d.daily);
    $('ab-packages').replaceChildren(table(['Пакет','Оплачено / заказов','Сумма оплат','Возвраты'],d.packages.map(p=>[units(p.units),`${p.paid} / ${p.orders}`,money(p.gross_kopecks),money(p.refunded_kopecks)])));
    $('ab-statuses').replaceChildren(...Object.entries(d.statuses).map(([state,count])=>{const e=node('div');e.append(badge(state),node('strong','',num(count)));return e;}));
    $('ab-orders').replaceChildren(table(['Создан · МСК','Покупатель','Пакет','Сумма','Статус','Возвращено','Заказ'],d.orders.map(o=>{
      const who=node('div');who.append(node('strong','',o.username||'Аккаунт недоступен'),node('small','ab-id',o.user_id));
      const open=button('Подробнее →',()=>showDetail(o.id));open.setAttribute('aria-label','Подробности заказа '+o.id);
      return [when(o.created_at),who,units(o.credits),money(o.amount_kopecks),badge(o.state),money(o.refunded_kopecks),open];
    })));
    $('ab-page').textContent=d.total?`${offset+1}–${Math.min(offset+d.limit,d.total)} из ${num(d.total)}`:'Заказов нет';
    $('ab-prev').disabled=offset===0;$('ab-next').disabled=offset+d.limit>=d.total;
    $('ab-methodology').textContent=d.methodology;$('ab-updated').textContent='Обновлено '+when(d.checked_at)+' МСК';
  }
  async function load(){
    clearTimeout(timer);controller?.abort();const id=++epoch;if(!active())return;controller=new AbortController();root.setAttribute('aria-busy','true');$('ab-error').textContent='';
    const params=new URLSearchParams({days:$('ab-days').value,mode:$('ab-mode').value,state:$('ab-state').value,query:$('ab-search').value,offset:String(offset),limit:'25'});
    try{const d=await Account.api('/api/admin/billing?'+params,{signal:controller.signal});if(id===epoch&&active())render(d);}
    catch(e){if(e.name!=='AbortError'&&id===epoch)$('ab-error').textContent=e.message;}
    finally{if(id===epoch){root.removeAttribute('aria-busy');if(active())timer=setTimeout(load,60000);}}
  }
  async function showDetail(id,position=0){
    detailId=id;detailOffset=position;detailController?.abort();detailController=new AbortController();const request=++detailEpoch;
    $('ab-detail-content').replaceChildren(node('p','muted','Загружаем заказ и операции аккаунта…'));if(!dialog.open)dialog.showModal();
    try{
      const d=await Account.api('/api/admin/billing/orders/'+encodeURIComponent(id)+'?offset='+position,{signal:detailController.signal});
      if(request!==detailEpoch||Account.user?.role!=='admin'||!dialog.open)return;
      const o=d.order,content=$('ab-detail-content');content.replaceChildren();
      content.append(node('p','ab-mode-label',o.mode==='test'?'Проверочная операция · реальные деньги не списывались':'Реальный платёж'),badge(o.state));
      const fields=node('dl','ab-detail-fields');
      for(const [label,value] of [['Покупатель',o.username||o.user_id],['Пакет',units(o.credits)],['Сумма',money(o.amount_kopecks)],['Возвращено',money(o.refunded_kopecks)],['Создан',when(o.created_at)+' МСК'],['Запись обновлена',when(o.updated_at)+' МСК'],['ID заказа',o.id],['ID платежа ЮKassa',o.payment_id||'Ещё не получен'],['Редакция условий',o.offer_version],['Ошибка',o.error_code||'Не записана'],['ID связанного разбора',o.analysis_id||'Не указан']])fields.append(node('dt','',label),node('dd','',value));
      content.append(fields,node('p','small muted','Время обновления записи не равно времени оплаты. Историческая дата фактической оплаты отдельно не сохранялась.'));
      const metrics=node('div','ab-wallet');metrics.append(metric('Баланс аккаунта',num(d.wallet.balance),'Включая бонусы'),metric('Бонусный остаток',num(d.wallet.bonus_units),'Показан отдельно от оплаченных единиц'),metric('Открыто ответов',num(d.wallet.opened_answers),'В выбранном режиме'));content.append(metrics);
      const refundTable=node('div','admin-table-wrap');refundTable.append(table(['Дата · МСК','Сумма','ID возврата'],d.refunds.map(r=>[when(r.at),money(r.amount_kopecks),r.id])));
      content.append(node('h3','','Возвраты по заказу'),refundTable);
      content.append(node('h3','','Движение разборов аккаунта'),node('p','small muted',d.ledger_notice));
      const wrap=node('div','admin-table-wrap');wrap.append(table(['Дата · МСК','Операция','Изменение','Основание'],d.ledger.map(r=>[when(r.at),kinds[r.kind]||r.kind,r.kind==='bonus_unlock'?'−1 бонус':(r.delta>0?'+':'')+r.delta,r.reference])));content.append(wrap);
      const paging=node('div','ab-paging');const prev=button('← Назад',()=>showDetail(id,Math.max(0,position-d.limit))),next=button('Далее →',()=>showDetail(id,position+d.limit));prev.disabled=position===0;next.disabled=position+d.limit>=d.ledger_total;
      paging.append(prev,node('span','small muted',d.ledger_total?`${position+1}–${Math.min(position+d.limit,d.ledger_total)} из ${d.ledger_total}`:'Операций нет'),next);content.append(paging);
    }catch(e){if(e.name!=='AbortError'&&request===detailEpoch)$('ab-detail-content').replaceChildren(node('p','account-message is-error',e.message));}
  }
  for(const id of ['ab-days','ab-mode','ab-state'])$(id).addEventListener('change',()=>{offset=0;load();});
  let searchTimer;$('ab-search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{offset=0;load();},300);});
  $('ab-prev').addEventListener('click',()=>{offset=Math.max(0,offset-25);load();});$('ab-next').addEventListener('click',()=>{offset+=25;load();});
  $('ab-detail-close').addEventListener('click',()=>dialog.close());dialog.addEventListener('close',()=>{detailController?.abort();detailEpoch++;});
  $('ab-export').addEventListener('click',()=>{if(!current)return;const url=URL.createObjectURL(new Blob([JSON.stringify(current,null,2)],{type:'application/json'}));const a=node('a');a.href=url;a.download='matchlab-payments-'+current.mode+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  document.addEventListener('accountchange',()=>{stop();current=null;offset=0;for(const id of ['ab-metrics','ab-chart','ab-packages','ab-statuses','ab-orders','ab-detail-content'])$(id).replaceChildren();if(dialog.open)dialog.close();$('ab-page').textContent='';$('ab-error').textContent='';});
  window.addEventListener('hashchange',()=>{if(!active())stop();});document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();else if(active())load();});
  return {load,stop};
})();
