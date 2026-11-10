"use strict";
window.AdminResults = (() => {
  const $=id=>document.getElementById(id), root=$('admin-results');
  const node=(tag,cls='',value='')=>{const n=document.createElement(tag);n.className=cls;n.textContent=value;return n;};
  const fmt=(n,digits=0)=>Number(n||0).toLocaleString('ru-RU',{maximumFractionDigits:digits});
  const pct=n=>n==null?'—':fmt(n*100,1)+'%';
  const when=s=>s?new Date(s).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
  const visible=()=>location.hash==='#admin' && !root.hidden && Account.user?.role==='admin';
  let timer,controller,serial=0,last=null,offset=0;
  function stop(){clearTimeout(timer);controller?.abort();serial++;root.removeAttribute('aria-busy');}
  function options(select,rows,selected){
    select.replaceChildren(...rows.map(([value,label])=>{const o=node('option','',label);o.value=value;return o;}));
    select.value=selected;
  }
  function metric(label,value,note){const n=node('article','ar-metric');n.append(node('span','',label),node('strong','',value),node('small','',note));return n;}
  function metrics(d){
    const m=d.metrics;
    $('ar-metrics').replaceChildren(
      metric('Попадания в победителя',pct(m.accuracy),`${fmt(m.hits)} из ${fmt(m.preferences)} проверенных предпочтений`),
      metric('Рекомендации ставить',fmt(m.bets),`${fmt(m.bet_wins)} выигрышных · ${fmt(m.bet_losses)} проигрышных`),
      metric('Решения пропустить',fmt(m.skips),`${fmt(m.skip_avoided_loss)} избежали проигрыша · ${fmt(m.skip_missed_win)} пропущенных побед`),
      metric('Ожидают подтверждения',fmt(m.pending+m.unresolved),`${fmt(m.settled)} итогов подтверждено · ${fmt(m.fixtures)} уникальных матчей`));
    $('ar-accuracy-value').textContent=pct(m.accuracy);
    $('ar-bank-value').textContent=m.priced_bets?fmt(m.paper_bank,3)+' ед.':'—';
  }
  function chart(target,data,field,{percentage=false,hasData=false}={}){
    target.replaceChildren();
    if(!hasData){target.append(node('div','ar-chart-empty',percentage?'Пока нет подтверждённых предпочтений. Здесь появится история попаданий.':'Пока нет завершённых ставок с сохранённой ценой. Виртуальный результат не рассчитан.'));return;}
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');
    const width=target.clientWidth<480?360:680,right=width-16;
    svg.setAttribute('viewBox',`0 0 ${width} 225`);svg.setAttribute('role','img');svg.setAttribute('tabindex','0');
    svg.setAttribute('aria-label',percentage?'Накопленная точность по дням. Стрелки влево и вправо выбирают дату.':'Виртуальный банк по дням. Стрелки влево и вправо выбирают дату.');
    const points=data.map((d,i)=>({d,i,value:d[field]})).filter(p=>p.value!=null);
    let lo=0,hi=1;
    if(!percentage){lo=Math.min(100,...points.map(p=>p.value));hi=Math.max(100,...points.map(p=>p.value));const pad=Math.max(.1,(hi-lo)*.15);lo-=pad;hi+=pad;}
    const x=i=>52+i*(right-52)/Math.max(1,data.length-1),y=v=>174-(v-lo)/(hi-lo)*150;
    const shape=(tag,attrs,cls)=>{const n=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));if(cls)n.setAttribute('class',cls);svg.append(n);return n;};
    for(let i=0;i<=3;i++){
      const v=lo+(hi-lo)*i/3,py=y(v);
      shape('line',{x1:52,x2:right,y1:py,y2:py},'ar-plot-grid');
      const label=shape('text',{x:43,y:py+4,'text-anchor':'end'},'ar-plot-label');label.textContent=percentage?fmt(v*100)+'%':fmt(v,2);
    }
    if(!percentage)shape('line',{x1:52,x2:right,y1:y(100),y2:y(100)},'ar-plot-baseline');
    const coords=points.map(p=>`${x(p.i)},${y(p.value)}`).join(' ');
    shape('polygon',{points:`${x(points[0].i)},174 ${coords} ${x(points.at(-1).i)},174`},'ar-plot-area');
    shape('polyline',{points:coords},'ar-plot-path');
    for(const [i,anchor] of [[0,'start'],[Math.floor((data.length-1)/2),'middle'],[data.length-1,'end']]){
      const label=shape('text',{x:x(i),y:202,'text-anchor':anchor},'ar-plot-label');label.textContent=data[i].day.slice(8,10)+'.'+data[i].day.slice(5,7);
    }
    const marker=shape('circle',{cx:0,cy:0,r:4},'ar-plot-marker');
    const readout=node('p','ar-chart-readout');readout.setAttribute('aria-live','polite');let selected=points.length-1;
    function show(i){selected=Math.max(0,Math.min(points.length-1,i));const p=points[selected];marker.setAttribute('cx',String(x(p.i)));marker.setAttribute('cy',String(y(p.value)));
      readout.textContent=`${p.d.day.slice(8,10)}.${p.d.day.slice(5,7)} · `+(percentage?`${pct(p.value)} · проверено предпочтений: ${fmt(p.d.cumulative_preferences)}`:`банк ${fmt(p.value,3)} ед. · результат за день ${p.d.paper_profit>0?'+':''}${fmt(p.d.paper_profit,3)} ед.`);}
    svg.addEventListener('pointermove',e=>{const rect=svg.getBoundingClientRect(),px=(e.clientX-rect.left)/rect.width*width;
      show(points.reduce((best,p,i)=>Math.abs(x(p.i)-px)<Math.abs(x(points[best].i)-px)?i:best,0));});
    svg.addEventListener('pointerleave',()=>show(points.length-1));
    svg.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();show(selected+(e.key==='ArrowLeft'?-1:1));}});
    show(selected);target.append(svg,readout);
  }
  function decisions(m){
    const container=$('ar-decisions');container.replaceChildren();
    if(!m.settled){container.append(node('div','ar-chart-empty','После подтверждения итогов здесь появится распределение ставок и пропусков.'));return;}
    const items=[['Ставка выиграла',m.bet_wins,'win'],['Ставка проиграла',m.bet_losses,'loss'],
      ['Избежал проигрыша',m.skip_avoided_loss,'avoid'],['Пропущена победа',m.skip_missed_win,'missed'],['Без предпочтения',m.skip_unassessed,'neutral']];
    const layout=node('div','ar-ring-layout'),ring=node('div','ar-ring'),legend=node('ul','ar-legend');
    let position=0;const parts=[];
    for(const [label,count,tone] of items){
      const color=tone==='neutral'?'var(--muted)':`var(--ar-${tone})`,end=position+count/m.settled*100;
      if(count)parts.push(`${color} ${position}% ${end}%`);position=end;
      const li=node('li'),dot=node('span','ar-legend-dot');dot.style.background=color;li.append(dot,node('span','',label),node('b','',fmt(count)));legend.append(li);
    }
    ring.style.background=`conic-gradient(${parts.join(',')})`;ring.setAttribute('role','img');ring.setAttribute('aria-label',items.map(([l,n])=>`${l}: ${n}`).join('; '));
    const center=node('div','ar-ring-center');center.append(node('strong','',fmt(m.settled)),node('small','','матчей'));ring.append(center);layout.append(ring,legend);
    container.append(layout,node('p','ar-decisions-note','Оценка пропусков — по известному итогу. Пропущенная победа не доказывает ошибку исходного решения.'));
  }
  const results={bet_won:['Ставка выиграла','win'],bet_lost:['Ставка проиграла','loss'],avoided_loss:['Избежал проигрыша','avoid'],
    missed_win:['Пропущена победа','missed'],no_preference:['Без предпочтения','neutral'],pending:['Ожидаем итог','neutral'],
    ambiguous_result:['Неоднозначный итог','neutral'],forecast_after_actual_kickoff:['Ответ после начала','neutral']};
  function pill(text,tone='neutral'){return node('span','ar-pill ar-tone-'+tone,text);}
  function matches(d){
    const box=$('ar-matches');box.replaceChildren();
    if(!d.matches.length){box.append(node('p','admin-empty',d.source==='trials'&&!d.reports.length?'Сохранённые архивные прогоны пока не найдены.':'По этим фильтрам пока нет разборов. Результаты появятся после сохранения предматчевых решений ИИ и получения итогов из архива.'));}
    else {
      const table=node('table','admin-table ar-table'),thead=node('thead'),tr=node('tr');
      const labels=['Матч · МСК','Предпочтение ИИ','Итог','Попадание','Действие','Проверка решения'];
      for(const label of labels)tr.append(node('th','',label));thead.append(tr);table.append(thead);
      const tbody=node('tbody');
      for(const c of d.matches){
        const row=node('tr'),cells=Array.from({length:6},()=>node('td'));
        cells.forEach((cell,i)=>{cell.dataset.label=labels[i];});
        cells[0].append(node('strong','ar-match-name',`${c.event.home} — ${c.event.away}`),node('span','ar-sub',c.event.competition),node('span','ar-sub',when(c.event.kickoff_at)+' МСК'));
        cells[1].append(node('span','',c.preference_label||'Не выбрана'),node('small','ar-sub',c.version));
        cells[2].append(node('strong','ar-score',c.actual_score||'—'));
        cells[3].append(c.preference_hit===null?pill(c.state==='settled'?'Не оценено':'Ожидаем итог'):pill(c.preference_hit?'Совпало':'Не совпало',c.preference_hit?'win':'loss'));
        cells[4].append(pill(c.decision==='bet'?'Ставить':'Пропустить',c.decision==='bet'?'avoid':'neutral'));
        if(c.decision==='bet')cells[4].append(node('small','ar-sub',c.paper_stake==null?'Виртуальный расчёт недоступен':`${fmt(c.paper_stake,3)} ед. · кф. ${fmt(c.odds,3)}`));
        const [label,tone]=results[c.decision_result]||['Не удалось сверить','neutral'];cells[5].append(pill(label,tone));
        if(c.paper_profit!=null)cells[5].append(node('span','ar-sub',`${c.paper_profit>0?'+':''}${fmt(c.paper_profit,3)} ед. к виртуальному банку`));
        const detail=node('details','ar-row-details');detail.append(node('summary','','Причина решения'));
        detail.append(node('p','ar-sub',(d.diagnostic_only?'Снимок: ':'Ответ ИИ: ')+when(c.forecast_at)+' МСК'));
        if(c.token_usage?.total_tokens!=null)detail.append(node('p','ar-sub',`Токены модели: ${c.token_usage.complete?'':'≥ '}${fmt(c.token_usage.total_tokens)} · пик входа ${fmt(c.token_usage.peak_prompt_tokens)}`));
        for(const block of c.reason){const p=node('p');p.append(node('strong','',block.label),document.createTextNode(block.text));detail.append(p);}
        cells[5].append(detail);row.append(...cells);tbody.append(row);
      }
      table.append(tbody);box.append(table);
    }
    $('ar-page').textContent=d.total?`${d.offset+1}–${Math.min(d.offset+d.limit,d.total)} из ${fmt(d.total)} матчей`:'Матчей: 0';
    $('ar-prev').disabled=d.offset===0;$('ar-next').disabled=d.offset+d.limit>=d.total;
  }
  function exclusions(d){
    const labels={duplicate_forecast:'Повторные разборы',system_or_model_skip:'Технические пропуски и ошибки',not_prematch:'Ответы после начала / live',
      model_not_ready:'Нет готового ответа ИИ',invalid_record:'Неполные записи',other_sport:'Другие виды спорта',future_price:'Цена из будущего',
      quote_event_mismatch:'Несовпадение матча и цены',future_statistics:'Статистика получена позже ответа',invalid_time:'Некорректное время'};
    const total=Object.values(d.excluded).reduce((a,b)=>a+b,0);
    $('ar-exclusions').textContent=total?'Не включены в выборку: '+Object.entries(d.excluded).map(([k,n])=>`${labels[k]||'Другие непригодные записи'} — ${fmt(n)}`).join('; ')+'.':'Повторы и непригодные записи в выбранном периоде не найдены.';
  }
  async function load(nextOffset=offset){
    if(!visible())return;stop();const epoch=serial;controller=new AbortController();offset=nextOffset;root.setAttribute('aria-busy','true');
    $('ar-message').textContent='Сверяем сохранённые решения с архивом…';$('ar-message').classList.remove('is-error');$('ar-export').disabled=true;
    const source=$('ar-source').value;const params=new URLSearchParams({days:$('ar-days').value,source,version:$('ar-version').value,
      state:$('ar-state').value,offset:String(offset),limit:'25'});
    if(source==='trials'&&$('ar-report').value)params.set('report_id',$('ar-report').value);
    try {
      const d=await Account.api('/api/admin/verdict-outcomes?'+params,{signal:controller.signal});if(epoch!==serial||!visible())return;
      last=d;offset=d.offset;options($('ar-version'),[['','Все версии'],...d.versions.map(v=>[v,v])],d.version);
      $('ar-report-field').hidden=d.source!=='trials';options($('ar-report'),d.reports.map(r=>[r.id,r.id]),d.report_id);
      $('ar-diagnostic').hidden=!d.diagnostic_only;$('ar-updated').textContent='Проверено '+when(d.checked_at)+' МСК';
      metrics(d);chart($('ar-accuracy-chart'),d.daily,'cumulative_accuracy',{percentage:true,hasData:d.metrics.preferences>0});
      chart($('ar-bank-chart'),d.daily,'cumulative_bank',{hasData:d.metrics.priced_bets>0});decisions(d.metrics);matches(d);exclusions(d);
      $('ar-message').textContent=d.diagnostic_only?'Архивный тест · результаты пересверены с текущим архивом Winline.':'Реальные разборы сайта · автоматическое обновление раз в минуту.';
      $('ar-export').disabled=false;
    } catch(error){if(epoch===serial&&error.name!=='AbortError'){$('ar-message').textContent=error.message;$('ar-message').classList.add('is-error');}}
    finally {if(epoch===serial){root.removeAttribute('aria-busy');if(visible()&&!document.hidden)timer=setTimeout(()=>load(),60000);}}
  }
  for(const id of ['ar-days','ar-version','ar-state','ar-report'])$(id).addEventListener('change',()=>load(0));
  $('ar-source').addEventListener('change',()=>{$('ar-version').value='';$('ar-report').replaceChildren();$('ar-report-field').hidden=$('ar-source').value!=='trials';load(0);});
  $('ar-prev').addEventListener('click',()=>load(Math.max(0,offset-25)));$('ar-next').addEventListener('click',()=>load(offset+25));
  $('ar-export').addEventListener('click',()=>{
    if(!last||!visible())return;const url=URL.createObjectURL(new Blob([JSON.stringify(last,null,2)],{type:'application/json'}));
    const a=node('a');a.href=url;a.download=`verdict-outcomes-${last.source}-${last.daily.at(-1).day}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  document.addEventListener('accountchange',()=>{stop();last=null;offset=0;
    for(const id of ['ar-metrics','ar-accuracy-chart','ar-bank-chart','ar-decisions','ar-matches','ar-page','ar-exclusions'])$(id).replaceChildren();
    $('ar-accuracy-value').textContent='—';$('ar-bank-value').textContent='—';$('ar-updated').textContent='Ожидаем проверку';
    $('ar-message').textContent='';$('ar-export').disabled=true;options($('ar-version'),[['','Все версии']],'');$('ar-report').replaceChildren();
  });
  window.addEventListener('hashchange',()=>{if(!visible())stop();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();else if(visible())load();});
  return {load,stop};
})();
