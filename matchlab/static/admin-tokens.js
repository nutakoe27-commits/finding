"use strict";
window.AdminTokens=(()=>{
  const $=id=>document.getElementById(id),root=$('admin-tokens');
  const node=(tag,cls='',text='')=>{const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;};
  const fmt=n=>n==null?'—':Number(n).toLocaleString('ru-RU');
  const when=v=>new Date(v).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  const active=()=>location.hash==='#admin'&&!root.hidden&&Account.user?.role==='admin';
  let offset=0,timer,controller,serial=0;
  function stop(){clearTimeout(timer);controller?.abort();serial++;root.removeAttribute('aria-busy');}
  function summaryCell(gen){
    const box=node('span'),u=gen?.token_usage;
    if(gen?.cache_hit||gen?.model_calls===0){box.textContent='0 / 0';return box;}
    if(!u||u.total_tokens==null){box.className='muted';box.textContent='Не записано';return box;}
    box.textContent=(u.complete?'':'≥ ')+fmt(u.prompt_tokens)+' / '+fmt(u.completion_tokens);return box;
  }
  function peakCell(gen){const u=gen?.token_usage;return node('span',u?.peak_context_tokens==null?'muted':'',gen?.cache_hit||gen?.model_calls===0?'0':u?.peak_context_tokens==null?'—':(u.complete?'':'≥ ')+fmt(u.peak_context_tokens));}
  function metric(label,value,note){const n=node('article','ar-metric');n.append(node('span','',label),node('strong','',fmt(value)),node('small','',note));return n;}
  function metrics(d){
    const m=d.metrics;$('tk-metrics').replaceChildren(
      metric('Учтено токенов',m.total_tokens,`Вход ${fmt(m.prompt_tokens)} · выход ${fmt(m.completion_tokens)}`),
      metric('Медиана расхода запроса',m.request_tokens_p50,`95-й перцентиль: ${fmt(m.request_tokens_p95)} · с учётом повторов`),
      metric('95-й перцентиль пика окна',m.context_p95,'Вход одного вызова + запрошенный резерв ответа'),
      metric('Максимальный записанный вход',m.peak_prompt_max,`Максимальный пик окна: ${fmt(m.context_max)}`));
    const gaps=m.missing_requests+m.partial_requests;$('tk-coverage').hidden=!gaps;
    $('tk-coverage').textContent=`Без записанного usage: ${fmt(m.missing_requests)} запросов. Частично учтено: ${fmt(m.partial_requests)}. Сумма выше включает только подтверждённые токены.`;
    $('tk-message').textContent=`Запросов: ${fmt(m.requests)} · полностью учтённых генераций: ${fmt(m.recorded_requests)} · из кэша: ${fmt(m.cached_requests)}. Обновляется раз в минуту.`;
  }
  function histogram(d){
    const box=$('tk-histogram');box.replaceChildren();const max=Math.max(0,...d.histogram.map(b=>b.calls));
    if(!max){box.append(node('p','ar-chart-empty','Пока нет вызовов с записанным usage. Данные начнут накапливаться при новых запросах к модели.'));return;}
    const list=node('ul','tk-hist-list');
    for(const b of d.histogram){const li=node('li'),label=node('span','',b.label),track=node('span','tk-hist-track'),bar=node('span');bar.style.width=(b.calls/max*100)+'%';track.append(bar);li.append(label,track,node('b','',fmt(b.calls)));list.append(li);}
    box.append(list,node('p','ar-decisions-note','Диапазоны относятся к входу одного вызова. Повторы учитываются отдельными вызовами.'));
  }
  function daily(d){
    const box=$('tk-daily');box.replaceChildren();const max=Math.max(0,...d.daily.map(v=>v.prompt_tokens+v.completion_tokens));
    if(!max){box.append(node('p','ar-chart-empty','Нет записанного расхода. Старые запросы не оцениваем по размеру текста.'));return;}
    const bars=node('div','tk-daily-bars');bars.tabIndex=0;bars.setAttribute('role','group');bars.setAttribute('aria-label','Вход и выход по дням. Стрелки влево и вправо выбирают день.');
    const readout=node('p','ar-chart-readout');readout.setAttribute('aria-live','polite');let selected=d.daily.length-1;
    function show(i){selected=Math.max(0,Math.min(d.daily.length-1,i));const day=d.daily[selected];readout.textContent=`${day.day.slice(8,10)}.${day.day.slice(5,7)} · вход ${fmt(day.prompt_tokens)} · выход ${fmt(day.completion_tokens)}`+(day.unreported_requests?` · неполный учёт: ${day.unreported_requests} запросов`:'');}
    d.daily.forEach((day,i)=>{const b=node('button','tk-day');b.type='button';b.tabIndex=-1;b.setAttribute('aria-label',`${day.day}: вход ${day.prompt_tokens}, выход ${day.completion_tokens}`);
      const output=node('span','tk-output'),input=node('span','tk-input');output.style.height=day.completion_tokens/max*175+'px';input.style.height=day.prompt_tokens/max*175+'px';b.append(output,input);b.addEventListener('pointerenter',()=>show(i));b.addEventListener('click',()=>show(i));bars.append(b);});
    bars.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();show(selected+(e.key==='ArrowLeft'?-1:1));}});show(selected);
    const legend=node('p','tk-legend');legend.append(node('span','tk-legend-input','● Вход'),node('span','tk-legend-output','● Выход'),node('span','muted',`Макс. за день: ${fmt(max)}`));box.append(bars,readout,legend);
  }
  const states={recorded:['Учтено','win'],partial:['Частичный учёт','missed'],missing:['Не записано','neutral'],cache:['Кэш · 0','neutral'],not_called:['Без вызова · 0','neutral']};
  function requests(d){
    const box=$('tk-requests');box.replaceChildren();
    if(!d.requests.length)box.append(node('p','admin-empty','За выбранный период запросов пока нет.'));
    else {
      const table=node('table','admin-table ar-table tk-table'),head=node('thead'),tr=node('tr');
      const labels=['Запрос · МСК','Вход','Выход','Всего','Пик окна','Учёт / вызовы'];for(const l of labels)tr.append(node('th','',l));head.append(tr);table.append(head);const body=node('tbody');
      for(const r of d.requests){
        const u=r.token_usage,prefix=u&&!u.complete?'≥ ':'',cells=labels.map(l=>{const n=node('td');n.dataset.label=l;return n;}),row=node('tr');
        cells[0].append(node('strong','ar-match-name',when(r.at)),node('span','ar-sub',(r.username||'Удалённый аккаунт')+' · '+(r.kind==='event'?'Матч':'Сценарий')));
        for(const [i,key] of [[1,'prompt_tokens'],[2,'completion_tokens'],[3,'total_tokens'],[4,'peak_context_tokens']])cells[i].append(node(i===3?'strong':'span','tk-count',u?.[key]==null?'—':prefix+fmt(u[key])));
        const [text,tone]=states[r.usage_state]||states.missing;cells[5].append(node('span','ar-pill ar-tone-'+tone,text));
        cells[5].append(node('small','ar-sub',`${u?.model_calls??r.model_calls??'—'} выз. · ${r.latency_ms==null?'—':(r.latency_ms/1000).toLocaleString('ru-RU',{maximumFractionDigits:1})+' с'}`));
        if(u?.calls?.length){const detail=node('details','ar-row-details');detail.append(node('summary','','По вызовам'));
          u.calls.forEach((c,i)=>detail.append(node('p','',`${i+1}. Вход ${fmt(c.prompt_tokens)}, выход ${fmt(c.completion_tokens)}, всего ${fmt(c.total_tokens)}; резерв ответа ${fmt(c.max_output_tokens)}.`)));cells[5].append(detail);}
        row.append(...cells);body.append(row);
      }
      table.append(body);box.append(table);
    }
    $('tk-page').textContent=d.total?`${d.offset+1}–${Math.min(d.offset+d.limit,d.total)} из ${fmt(d.total)} запросов`:'Запросов: 0';
    $('tk-prev').disabled=d.offset===0;$('tk-next').disabled=d.offset+d.limit>=d.total;
  }
  async function load(nextOffset=offset){
    if(!active())return;stop();const epoch=serial;controller=new AbortController();offset=nextOffset;root.setAttribute('aria-busy','true');
    $('tk-message').textContent='Читаем записанный расход…';$('tk-message').classList.remove('is-error');
    try {
      const params=new URLSearchParams({days:$('tk-days').value,kind:$('tk-kind').value,offset:String(offset),limit:'25'});
      const d=await Account.api('/api/admin/ai-token-usage?'+params,{signal:controller.signal});if(epoch!==serial||!active())return;
      metrics(d);histogram(d);daily(d);requests(d);$('tk-updated').textContent='Проверено '+when(d.checked_at)+' МСК';
    } catch(e){if(epoch===serial&&e.name!=='AbortError'){$('tk-message').textContent=e.message;$('tk-message').classList.add('is-error');}}
    finally {if(epoch===serial){root.removeAttribute('aria-busy');if(active()&&!document.hidden)timer=setTimeout(()=>load(),60000);}}
  }
  for(const id of ['tk-days','tk-kind'])$(id).addEventListener('change',()=>load(0));
  $('tk-prev').addEventListener('click',()=>load(Math.max(0,offset-25)));$('tk-next').addEventListener('click',()=>load(offset+25));
  document.addEventListener('accountchange',()=>{stop();offset=0;for(const id of ['tk-metrics','tk-histogram','tk-daily','tk-requests','tk-page'])$(id).replaceChildren();$('tk-message').textContent='';$('tk-coverage').hidden=true;$('tk-updated').textContent='Ожидаем данные';});
  window.addEventListener('hashchange',()=>{if(!active())stop();});document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();else if(active())load();});
  return {load,stop,summaryCell,peakCell};
})();
