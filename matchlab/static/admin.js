"use strict";
window.Admin = (() => {
  const $=id=>document.getElementById(id), api=Account.api;
  const node=(tag,className="",text="")=>{const item=document.createElement(tag);item.className=className;item.textContent=text;return item;};
  const format=value=>Number(value||0).toLocaleString("ru-RU");
  const when=value=>value ? new Date(value).toLocaleString("ru-RU",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "—";
  let tab="overview",userOffset=0,auditOffset=0,request=0,elevatedUntil=0,resolvePassword=null,pushPreview=null,lastOverview=null;
  const statuses={draft:"Черновик",active:"Активен",paused:"Приостановлен",queued:"В очереди",sending:"Отправляется",complete:"Завершена",cancelled:"Отменена",success:"Успешно",limited:"Сводка · без диагностики",failed:"Ошибка",pending:"В работе",ai_ready:"ИИ ответил",ai_partial:"ИИ · частично",ai_skipped:"ИИ не вызывался",ai_context_limit:"Превышен контекст",ai_error:"Ошибка ИИ",ai_cached:"Ответ из кэша"};
  const aiReasons={context_limit:"Превышен контекст",quotation_mismatch:"Цитата не совпала с данными",unsupported_number:"Число не подтверждено источником",timeout:"Истекло время ожидания",connection:"Нет соединения с ИИ",proxy:"Ошибка прокси",authentication:"Ошибка ключа ИИ",model_or_endpoint:"Модель или адрес API не найдены",provider_http:"Ошибка сервера ИИ",output_limit:"Ответ не завершён",reasoning_only:"Нет итогового ответа",invalid_response:"Некорректный ответ ИИ",insufficient_data:"Недостаточно подтверждённых данных"};
  const actions={"admin.bootstrap":"Создан администратор","admin.reauth":"Подтверждён доступ","admin.reauth_failed":"Неверный пароль администратора","account.password_changed":"Изменён пароль","user.enabled":"Включён аккаунт","user.disabled":"Ограничен аккаунт","promo.created":"Создан промокод","promo.active":"Включён промокод","promo.paused":"Приостановлен промокод","promo.draft":"Промокод возвращён в черновики","promo.redeemed":"Активирован промокод","links.updated":"Изменены партнёрские ссылки","push.draft":"Создан черновик уведомления","push.queued":"Запланирована отправка уведомления","push.cancelled":"Отменена отправка"};
  function message(value,error=false){$("admin-message").textContent=value;$("admin-message").classList.toggle("is-error",error);}
  function table(headers,rows){
    if(!rows.length)return node("p","admin-empty","Пока нет записей. Здесь появятся реальные данные сервиса.");
    const table=node("table","admin-table"),head=node("thead"),hr=node("tr");headers.forEach(value=>hr.append(node("th","",value)));head.append(hr);
    const body=node("tbody");rows.forEach(values=>{const row=node("tr");values.forEach(value=>{const cell=node("td");cell.append(value instanceof Node ? value : document.createTextNode(String(value ?? "—")));row.append(cell);});body.append(row);});table.append(head,body);return table;
  }
  function button(label,action,className="text-button"){
    const b=node("button",className,label);b.type="button";b.addEventListener("click",async()=>{b.disabled=true;try{await action();}catch(error){message(error.message,true);}finally{b.disabled=false;}});return b;
  }
  function badge(status){return node("span","admin-status status-"+status,statuses[status]||status);}
  function aiResult(item){
    const gen=item.generation;if(!gen?.state && !gen?.cache_hit)return badge(item.status);
    const value=gen.cache_hit ? 'ai_cached' : gen.state==='ready' ? 'ai_ready' : gen.state==='partial' ? 'ai_partial' : gen.state==='skipped' ? 'ai_skipped' : gen.reason==='context_limit' ? 'ai_context_limit' : 'ai_error';
    const box=node('div','admin-ai-result');box.append(badge(value));
    if(gen.reason)box.append(node('small','muted',aiReasons[gen.reason]||'Причина не установлена'));
    return box;
  }
  function confirmPassword(){
    $("admin-password-error").textContent="";$("admin-password").value="";$("admin-password-dialog").showModal();$("admin-password").focus();
    return new Promise(resolve=>{resolvePassword=resolve;});
  }
  async function sensitive(action){
    if(Date.now()>=elevatedUntil && !await confirmPassword())return;
    try{return await action();}catch(error){if(error.reauth){elevatedUntil=0;if(await confirmPassword())return action();}else throw error;}
  }
  $("admin-password-form").addEventListener("submit",async event=>{
    event.preventDefault();const b=event.currentTarget.querySelector("button");b.disabled=true;
    try{await api("/api/admin/reauth",{method:"POST",body:JSON.stringify({password:$("admin-password").value})});$("admin-password").value="";elevatedUntil=Date.now()+590000;$("admin-password-dialog").close();resolvePassword?.(true);resolvePassword=null;}
    catch(error){$("admin-password-error").textContent=error.message;}finally{b.disabled=false;}
  });
  function cancelPassword(){resolvePassword?.(false);resolvePassword=null;$("admin-password").value="";}
  $("admin-password-close").addEventListener("click",()=>{$("admin-password-dialog").close();cancelPassword();});
  $("admin-password-dialog").addEventListener("cancel",cancelPassword);
  $("admin-confirm-access").addEventListener("click",()=>sensitive(async()=>message("Пароль подтверждён для этого сеанса.")).catch(e=>message(e.message,true)));
  function metrics(data){
    const values=[["Пользователей",data.users,`Новых за период: ${format(data.new_users)}`],["Активных аккаунтов",data.active_users,"По входам за выбранный период"],["Посетителей",data.visitors,"Уникальные браузеры"],["Готовых разборов",data.ai.success,`Сбоев: ${format(data.ai.failed)} · Из кэша: ${format(data.ai.cached)}`],["Переходов в Winline",data.events.outbound_click||0,"Нажатия на ссылки событий"],["Подписчиков push",data.push_subscribers,"Аккаунты с активной подпиской"],["Принято push-службой",data.push_sent,"За выбранный период"],["Активаций промокодов",data.promo_activations,`Начислено бонусов: ${format(data.promo_credits)}`]];
    $("admin-metrics").replaceChildren(...values.map(([label,value,note])=>{const card=node("article","admin-metric");card.append(node("span","small muted",label),node("strong","",format(value)),node("small","muted",note));return card;}));
  }
  function traffic(data){
    if(!data.daily.length){$("admin-traffic").replaceChildren(node("p","admin-empty","Посещения начнут отображаться после открытия страниц пользователями."));return;}
    const days=[];const end=new Date();end.setUTCHours(0,0,0,0);
    const byDate=new Map(data.daily.map(i=>[i.day,i.visitors]));for(let i=data.days-1;i>=0;i--){const date=new Date(end);date.setUTCDate(date.getUTCDate()-i);const day=date.toISOString().slice(0,10);days.push({day,value:byDate.get(day)||0});}
    const ns="http://www.w3.org/2000/svg",svg=document.createElementNS(ns,"svg");svg.setAttribute("viewBox","0 0 640 190");svg.setAttribute("role","img");svg.setAttribute("aria-label","Уникальные браузеры по дням: "+days.map(d=>`${d.day}: ${d.value}`).join(", "));
    const max=Math.max(1,...days.map(d=>d.value)),points=days.map((d,i)=>`${12+i*616/Math.max(1,days.length-1)},${160-d.value/max*140}`).join(" ");
    for(const y of [20,90,160]){const line=document.createElementNS(ns,"line");line.setAttribute("x1","12");line.setAttribute("x2","628");line.setAttribute("y1",String(y));line.setAttribute("y2",String(y));line.setAttribute("class","admin-chart-grid");svg.append(line);}
    const path=document.createElementNS(ns,"polyline");path.setAttribute("points",points);path.setAttribute("class","admin-chart-line");svg.append(path);
    const caption=node("div","admin-chart-caption");caption.append(node("span","",days[0].day),node("span","",`Максимум: ${format(max)}`),node("span","",days.at(-1).day));$("admin-traffic").replaceChildren(svg,caption);
  }
  function funnel(data){
    const stages=[["Просмотры страниц",data.events.page_view||0],["Открытия матчей",data.events.match_view||0],["Намерения получить разбор",data.events.ai_intent||0],["Готовые разборы",data.ai.success],["Регистрации",data.new_users]];const max=Math.max(1,...stages.map(i=>i[1]));
    $("admin-funnel").replaceChildren(...stages.map(([label,value])=>{const row=node("div","admin-funnel-row"),top=node("div");top.append(node("span","",label),node("strong","",format(value)));const bar=node("div","admin-funnel-bar"),fill=node("span");fill.style.width=`${value/max*100}%`;bar.append(fill);row.append(top,bar);return row;}),node("p","small muted","Количество событий, а не когортная конверсия."));
  }
  async function loadOverview(){
    const data=await api("/api/admin/overview?days="+$("admin-period").value);if(tab!=="overview" || location.hash!=="#admin")return;lastOverview=data;metrics(data);traffic(data);funnel(data);
    const health=data.archive,box=node("div","admin-health-grid");
    for(const [label,value] of [["Поток матчей",health.connected && !health.source_stale ? "Обновляется" : health.available ? "Данные сохранены, поток задерживается" : "Ожидает подключение"],["Событий в потоке",format(health.events)],["Результатов в архиве",format(health.stored_results)],["Последнее наблюдение",when(health.last_snapshot_at)],["Среднее время запроса к сайту",data.ai.avg_ms ? `${(data.ai.avg_ms/1000).toFixed(1)} с` : "Пока нет готовых разборов без кэша"],["Сбои и неполные разборы",`${format(data.ai.failed)} из ${format(data.ai.total)} запросов`]]){const item=node("div");item.append(node("small","muted",label),node("strong","",value));box.append(item);}
    $("admin-service-health").replaceChildren(box);
    $("admin-ai-list").replaceChildren(table(["Время","Пользователь","Запрос","Результат","Вызовы ИИ / части","Время сайта"],data.recent_ai.map(i=>[when(i.at),i.username||"Удалённый аккаунт",i.kind==="event"?"Матч":"Сценарий",aiResult(i),i.generation?.cache_hit ? '0 выз. · кэш' : i.generation?.model_calls==null ? '—' : `${i.generation.model_calls} выз. / ${i.generation.parts_completed??'—'} из ${i.generation.parts_total??'—'} частей`,i.latency_ms==null?"—":`${(i.latency_ms/1000).toFixed(1)} с`])));
  }
  async function loadUsers(){
    const query=new URLSearchParams({search:$("admin-user-search").value,offset:String(userOffset)}),data=await api("/api/admin/users?"+query);if(tab!=="users")return;
    $("admin-user-total").textContent=`Аккаунтов: ${format(data.total)}`;
    $("admin-users").replaceChildren(table(["Пользователь","Регистрация","Последний вход","Разборы","Бонусы","Доступ"],data.users.map(user=>{
      const controls=node("div","button-row");controls.append(badge(user.enabled?"active":"paused"));
      if(user.role!=="admin")controls.append(button(user.enabled?"Ограничить":"Включить",()=>sensitive(async()=>{
        if(user.enabled && !confirm(`Ограничить доступ аккаунта ${user.username}? Его текущие сеансы будут завершены.`))return;
        await api("/api/admin/users/"+user.id,{method:"PATCH",body:JSON.stringify({enabled:!user.enabled})});message("Доступ пользователя обновлён.");await loadUsers();
      })));
      return [user.username+(user.role==="admin"?" · администратор":""),when(user.created_at),when(user.last_login_at),format(user.analyses),format(user.bonus_analyses),controls];
    })));
    $("admin-users-prev").disabled=userOffset===0;$("admin-users-next").disabled=userOffset+data.users.length>=data.total;
  }
  async function loadPromos(){
    const data=await api("/api/admin/promos");if(tab!=="promos")return;
    $("admin-promos").replaceChildren(table(["Промокод","Кампания","Бонус","Активации","Срок","Статус"],data.promos.map(p=>{
      const controls=node("div","button-row");controls.append(badge(p.status),button(p.status==="active"?"Приостановить":"Включить",()=>sensitive(async()=>{await api("/api/admin/promos/"+p.id,{method:"PATCH",body:JSON.stringify({status:p.status==="active"?"paused":"active"})});message("Состояние промокода обновлено.");await loadPromos();})));
      return [p.code,p.title,format(p.bonus),`${format(p.uses)} / ${format(p.max_uses)}`,`${when(p.starts_at)} — ${when(p.ends_at)}`,controls];
    })));
  }
  async function previewCampaign(id){
    const data=await api(`/api/admin/push/${id}/preview`);pushPreview={id,audience:data.audience};
    $("push-confirm-title").textContent=data.campaign.title;$("push-confirm-body").textContent=data.campaign.body;
    $("push-confirm-audience").textContent=`Подписанных устройств: ${data.audience}. Время: ${when(data.campaign.scheduled_at)}.`;
    $("push-confirm-send").disabled=!data.enabled || !data.audience || data.campaign.status!=="draft";$("push-confirm-dialog").showModal();
  }
  async function loadPush(){
    const data=await api("/api/admin/push");if(tab!=="push")return;
    $("admin-push-state").textContent=!data.enabled?"Нужна настройка ключей":data.worker_recent?"Очередь работает":"Очередь не отвечает";
    $("admin-push-note").textContent=`Подписанных устройств: ${format(data.audiences.all)}. ${data.enabled ? (data.worker_recent ? "Отправкой занимается очередь на VPS. Счётчик доставки означает приём push-службой." : "Ключи настроены, но нет свежего отчёта службы доставки. Сообщения останутся в очереди до её запуска.") : "Черновики можно готовить сейчас. Для отправки создайте ключи Web Push и включите службу доставки по инструкции установки."}`;
    $("admin-campaigns").replaceChildren(table(["Кампания","Время","Статус","Устройства","Принято / ошибки","Действия"],data.campaigns.map(c=>{
      const controls=node("div","button-row");if(c.status==="draft")controls.append(button("Предпросмотр",()=>previewCampaign(c.id)));
      if(!["complete","cancelled"].includes(c.status))controls.append(button("Отменить",()=>sensitive(async()=>{if(!confirm("Отменить неотправленную часть кампании?"))return;await api(`/api/admin/push/${c.id}/cancel`,{method:"POST",body:"{}"});await loadPush();})));
      return [c.title,when(c.scheduled_at),badge(c.status),format(c.audience),`${format(c.sent)} / ${format(c.failed)}`,controls];
    })));
  }
  async function loadLinks(){const data=await api("/api/admin/links");if(tab==="links")$("admin-link-template").value=data.template;}
  async function loadAudit(){
    const data=await api("/api/admin/audit?offset="+auditOffset);if(tab!=="audit")return;
    $("admin-audit").replaceChildren(table(["Время","Кто","Действие","Объект"],data.events.map(i=>[when(i.at),i.username||"Системная команда",actions[i.action]||i.action,(i.target||"—").slice(0,32)])));
    $("admin-audit-prev").disabled=auditOffset===0;$("admin-audit-next").disabled=auditOffset+data.events.length>=data.total;
  }
  async function loadQuality(mode){
    const target=$("quality-result");target.replaceChildren(node("p","muted","Проверяем сохранённые данные…"));
    $("quality-saved").disabled=true;$("quality-historical").disabled=true;
    try {
      const report=await api('/api/admin/forecast-quality?limit=10&mode='+mode);target.replaceChildren();
      const number=value=>(value*100).toLocaleString('ru-RU',{maximumFractionDigits:1})+'%';
      target.append(node('h3','',mode==='saved'?'Сохранённые предматчевые прогнозы':'Ретроспективная реконструкция · не предматчевый тест'));
      target.append(node('p','',`Оценено ${report.evaluated} из ${report.requested}. Выбрано: ${report.selected}. Ожидают однозначного результата: ${report.pending}.`));
      const metrics=report.metrics;
      if(metrics) target.append(node('p','',`Исход угадан: ${metrics.favorite_hits} из ${metrics.evaluated} (${number(metrics.accuracy)}). Интервал неопределённости доли попаданий: ${metrics.accuracy_wilson_95.map(number).join('–')}. Brier: ${metrics.brier_3way.toFixed(3)}, равномерный ориентир: ${metrics.uniform_baseline.brier_3way.toFixed(3)}. Log loss: ${metrics.log_loss.toFixed(3)}, ориентир: ${metrics.uniform_baseline.log_loss.toFixed(3)}. Для Brier и log loss меньше — лучше.`));
      else target.append(node('p','notice','Подходящих прогнозов для проверки пока нет. Матчи с недостаточными данными не заменяются более удобными примерами.'));
      const wrap=node('div','admin-table-wrap');wrap.append(table(['Матч','Начало','П1 / Х / П2','Счёт','Исход'],report.matches.map(row=>[
        `${row.home} — ${row.away}`,when(row.kickoff_at),row.probabilities?['home','draw','away'].map(k=>number(row.probabilities[k])).join(' / '):'Недостаточно истории',row.actual_score,row.favorite_hit===null?'Не оценён':row.favorite_hit?'Совпал':'Не совпал'])));target.append(wrap);
      const limitations=node('ul','small muted');report.limitations.forEach(text=>limitations.append(node('li','',text)));target.append(limitations);
      if(report.excluded_forecasts.other_model_version) target.append(node('p','small muted',`Прогнозов других версий расчёта исключено: ${report.excluded_forecasts.other_model_version}.`));
      target.append(button('Скачать JSON отчёта',()=>{const blob=new Blob([JSON.stringify(report,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),link=node('a');link.href=url;link.download=`winline-10-${mode}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}));
    } catch(error) {target.replaceChildren(node('p','account-message is-error',error.message));}
    finally {$("quality-saved").disabled=false;$("quality-historical").disabled=false;}
  }
  $("quality-saved").addEventListener('click',()=>loadQuality('saved'));
  $("quality-historical").addEventListener('click',()=>loadQuality('historical-results'));
  const loaders={overview:loadOverview,quality:async()=>{},users:loadUsers,push:loadPush,promos:loadPromos,links:loadLinks,audit:loadAudit};
  async function load(){const id=++request;$("admin-refresh").disabled=true;try{await loaders[tab]();}catch(error){if(id===request)message(error.message,true);}finally{if(id===request)$("admin-refresh").disabled=false;}}
  async function open(){
    await Account.ready;const allowed=Account.user?.role==="admin";$("admin-denied").hidden=allowed;$("admin-content").hidden=!allowed;$("admin-refresh").hidden=!allowed;
    if(allowed)await load();
  }
  document.querySelectorAll("[data-admin-tab]").forEach(b=>b.addEventListener("click",()=>{tab=b.dataset.adminTab;document.querySelectorAll("[data-admin-tab]").forEach(i=>{i.classList.toggle("active",i===b);i.setAttribute("aria-current",i===b?"page":"false");});document.querySelectorAll("[data-admin-panel]").forEach(p=>{p.hidden=p.dataset.adminPanel!==tab;});message("");load();}));
  $("admin-refresh").addEventListener("click",load);$("admin-period").addEventListener("change",load);
  let userSearch;$("admin-user-search").addEventListener("input",()=>{clearTimeout(userSearch);userSearch=setTimeout(()=>{userOffset=0;loadUsers().catch(e=>message(e.message,true));},300);});
  $("admin-users-prev").addEventListener("click",()=>{userOffset=Math.max(0,userOffset-30);load();});$("admin-users-next").addEventListener("click",()=>{userOffset+=30;load();});
  $("admin-audit-prev").addEventListener("click",()=>{auditOffset=Math.max(0,auditOffset-50);load();});$("admin-audit-next").addEventListener("click",()=>{auditOffset+=50;load();});
  const localDate=date=>new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);
  $("admin-promo-start").value=localDate(new Date());$("admin-promo-end").value=localDate(new Date(Date.now()+30*86400000));
  $("admin-promo-form").addEventListener("submit",event=>{event.preventDefault();sensitive(async()=>{
    await api("/api/admin/promos",{method:"POST",body:JSON.stringify({code:$("admin-promo-code").value,title:$("admin-promo-title").value,bonus:Number($("admin-promo-bonus").value),max_uses:Number($("admin-promo-limit").value),starts_at:new Date($("admin-promo-start").value).toISOString(),ends_at:new Date($("admin-promo-end").value).toISOString()})});
    message("Промокод создан черновиком. Включите его, когда будете готовы.");await loadPromos();
  }).catch(e=>message(e.message,true));});
  $("admin-push-form").addEventListener("submit",event=>{event.preventDefault();sensitive(async()=>{
    await api("/api/admin/push",{method:"POST",body:JSON.stringify({title:$("push-title").value,body:$("push-body").value,url:$("push-url").value,segment:$("push-segment").value,scheduled_at:$("push-scheduled").value?new Date($("push-scheduled").value).toISOString():null})});
    message("Черновик сохранён. Проверьте аудиторию и подтвердите отправку в списке кампаний.");await loadPush();
  }).catch(e=>message(e.message,true));});
  for(const [source,target,fallback] of [["push-title","push-preview-title","Заголовок уведомления"],["push-body","push-preview-body","Так подписчики увидят ваше сообщение."]])$(source).addEventListener("input",()=>{$(target).textContent=$(source).value||fallback;});
  $("push-confirm-close").addEventListener("click",()=>{$("push-confirm-dialog").close();pushPreview=null;});
  $("push-confirm-send").addEventListener("click",()=>{
    const preview=pushPreview;if(!preview)return;$("push-confirm-dialog").close();
    sensitive(async()=>{await api(`/api/admin/push/${preview.id}/send`,{method:"POST",body:JSON.stringify({confirm_audience:preview.audience})});pushPreview=null;message("Кампания поставлена в очередь. Статусы доставки обновляются при нажатии «Обновить».");await loadPush();}).catch(e=>message(e.message,true));
  });
  $("admin-link-form").addEventListener("submit",event=>{event.preventDefault();sensitive(async()=>{await api("/api/admin/links",{method:"PUT",body:JSON.stringify({template:$("admin-link-template").value})});message("Правило ссылок сохранено.");}).catch(e=>message(e.message,true));});
  $("admin-export").addEventListener("click",()=>{
    if(!lastOverview)return;const d=lastOverview,rows=[["Период, дней",d.days],["Пользователи",d.users],["Регистрации",d.new_users],["Посетители",d.visitors],["Успешные ИИ-запросы",d.ai.success],["Ошибки ИИ",d.ai.failed],["Переходы Winline",d.events.outbound_click||0],["Активации промокодов",d.promo_activations]];
    const blob=new Blob(["\ufeffПоказатель;Значение\n"+rows.map(r=>r.join(";")).join("\n")],{type:"text/csv;charset=utf-8"});const url=URL.createObjectURL(blob),a=node("a");a.href=url;a.download="prognozich-analytics.csv";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  document.addEventListener("accountchange",()=>{elevatedUntil=0;if(location.hash==="#admin")open();});
  if(location.hash==="#admin")open();
  return {open};
})();
