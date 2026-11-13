"use strict";
window.PublicResults=(()=>{
  const $=id=>document.getElementById(id);
  const node=(tag,cls='',value='')=>{const n=document.createElement(tag);n.className=cls;n.textContent=ServiceWording.text(value);return n;};
  const fmt=value=>Number(value||0).toLocaleString('ru-RU',{maximumFractionDigits:2});
  const percent=value=>value==null?'—':(value*100).toLocaleString('ru-RU',{maximumFractionDigits:1})+'%';
  const when=value=>new Date(value).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  let route='',epoch=0,timer=null,offset=0;
  async function get(path){const r=await fetch(path,{credentials:'same-origin'});if(!r.ok)throw new Error('Не удалось загрузить результаты. Попробуйте ещё раз.');return r.json();}
  function outcome(row){return row.state==='settled'?(row.bet_won?['Зашло','win']:['Не зашло','loss']):row.purchased_archive?['Сохранённый разбор','pending']:row.today_pick?[row.started?'Время начала уже наступило':'До начала матча','pending']:row.state==='unresolved'?['Итог не подтверждён','pending']:['Ожидаем итог','pending'];}
  function metric(label,value,description,cls=''){
    const box=node('div','pr-metric '+cls);box.append(node('span','pr-label',label),node('strong','',value),node('span','pr-caption',description));return box;
  }
  function renderMetrics(target,data){
    const m=data.metrics;
    target.replaceChildren(metric('Процент верных решений',percent(m.win_rate),`${fmt(m.won)} из ${fmt(m.settled)} проверенных`,'accuracy '+(m.win_rate==null?'':'win-rate')),
      metric('Решений «Заходим»',fmt(m.decisions),`${fmt(m.matches)} матчей · ${fmt(m.pending)} ожидают итога`),
      metric('Зашло / не зашло',`${fmt(m.won)} / ${fmt(m.lost)}`,m.unresolved?`${fmt(m.unresolved)} итогов пока не подтверждены`:'По подтверждённым итогам'),
      metric('Средний коэффициент при разборе',m.average_odds==null?'—':m.average_odds.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2}),
        m.average_odds_count?`Рекомендаций «Заходим»: ${fmt(m.average_odds_count)} · включая ожидающие итога`:'Появится после рекомендаций «Заходим»'));
  }
  async function widget(){
    const current=epoch;
    try{const data=await get('/api/results?limit=1');if(current!==epoch)return;renderMetrics($('results-widget-metrics'),data);$('results-widget-note').textContent=data.metrics.decisions?'Только реальные рекомендации «Заходим» до начала матчей. Верность решения определяется по подтверждённому исходу решения. Коэффициенты зафиксированы при разборе.':'История заполняется: подтверждённые рекомендации появятся здесь.';}
    catch(error){if(current===epoch)$('results-widget-note').textContent='Результаты временно недоступны. Откройте историю, чтобы повторить загрузку.';}
  }
  function chart(data){
    const box=$('pr-chart');box.replaceChildren();
    if(!data.metrics.settled){box.append(node('p','muted','График появится, когда получим подтверждённые итоги рекомендаций.'));return;}
    const days=data.daily,max=Math.max(1,...days.map(d=>d.bet_wins+d.bet_losses));
    for(const d of days){
      const bar=node('div','pr-day');bar.setAttribute('role','listitem');bar.setAttribute('aria-label',`${d.day}: подтвердившихся решений ${d.bet_wins}, неподтвердившихся ${d.bet_losses}`);bar.title=bar.getAttribute('aria-label');
      const stack=node('div','pr-day-stack');
      for(const [key,cls] of [['bet_wins','win'],['bet_losses','loss']]){const part=node('span',cls);part.style.height=(d[key]/max*100)+'%';stack.append(part);}
      bar.append(stack,node('span','pr-day-date',d.day.slice(8)));box.append(bar);
    }
  }
  function item(row){
    const a=node('a','pr-row');a.href='#result/'+row.id;
    const title=node('div','pr-row-title');title.append(node('strong','',`${row.event.home} — ${row.event.away}`),node('span','pr-caption',`${row.sport_label} · ${row.event.competition}`),node('span','pr-caption',`${when(row.event.kickoff_at)} МСК · ответ ${when(row.forecast_at)}`));
    const pick=node('div','pr-row-pick');pick.append(node('strong','',row.selection_label),node('span','pr-caption',`${row.market_label} · кф. ${fmt(row.odds)}`),node('span','pr-caption',row.origin==='system'?'Системный анализ':'Разбор по запросу пользователя'));
    const [label,cls]=outcome(row),result=node('div','pr-row-result'),open=node('span','pr-open','Посмотреть ответ');open.append(ProductUX.icon('arrow'));result.append(node('span','pr-outcome '+cls,label),node('strong','pr-score',row.actual_score||'—'),open);
    a.append(title,pick,result);return a;
  }
  async function list(nextOffset=offset){
    offset=nextOffset;const current=++epoch;$('pr-status').textContent='Сверяем решения с итогами матчей…';$('pr-list').setAttribute('aria-busy','true');
    try{
      const data=await get('/api/results?'+new URLSearchParams({sport:$('pr-sport').value,offset:String(offset),limit:'20'}));if(current!==epoch||route!=='#results')return;
      renderMetrics($('pr-metrics'),data);chart(data);
      const chosen=data.sport;$('pr-sport').replaceChildren(new Option('Все виды спорта',''),...Object.entries(data.sports).map(([id,p])=>new Option(p.label,id)));$('pr-sport').value=chosen;
      $('pr-list').replaceChildren(...(data.matches.length?data.matches.map(item):[node('p','pr-empty','За этот период подходящих рекомендаций пока нет. Пропуски и неподтверждённые ответы ИИ сюда не добавляются.')]));
      $('pr-methodology').replaceChildren(...data.methodology.map(line=>node('p','',line)));
      $('pr-status').textContent=`Обновлено ${when(data.checked_at)} МСК.${data.archive_current?'':' Новые итоги могут появиться с задержкой.'}`;
      $('pr-page').textContent=data.total?`${data.offset+1}–${Math.min(data.total,data.offset+data.limit)} из ${data.total} решений`:'Решений: 0';
      $('pr-prev').disabled=data.offset===0;$('pr-next').disabled=data.offset+data.limit>=data.total;
    }catch(error){if(current===epoch)$('pr-status').textContent=error.message;}
    finally{if(current===epoch)$('pr-list').setAttribute('aria-busy','false');}
  }
  async function detail(id){
    const current=++epoch,box=$('public-result-content');box.replaceChildren(node('p','muted','Загружаем сохранённый ответ…'));
    $('public-result-title').textContent='Рекомендация ИИ';$('public-result-meta').textContent='';
    $('public-result-back').href='#results';$('public-result-back').textContent='← Ко всем результатам';
    try{
      const row=await get('/api/results/'+id);if(current!==epoch||route!=='#result/'+id)return;
      if(row.locked){
        $('public-result-title').textContent='Разбор ИИ';$('public-result-meta').textContent='Сохранённый ответ · доступ по разблокировке';
        $('public-result-back').href='#matches';$('public-result-back').textContent='← К матчам';
        Credits.renderLocked(box,row,()=>detail(id));return;
      }
      $('public-result-title').textContent=`${row.event.home} — ${row.event.away}`;
      $('public-result-meta').textContent=`${row.sport_label} · ${row.event.competition} · ${when(row.event.kickoff_at)} МСК`;
      if(row.today_pick){$('public-result-back').href='#matches';$('public-result-back').textContent='← К подборке разборов';}
      const [label,cls]=outcome(row),lead=node('section','pr-answer-lead');
      lead.append(node('span','pr-outcome '+cls,label),node('p','pr-caption',`Рекомендация зафиксирована ${when(row.forecast_at)} МСК`),node('h2','',`Победа «${row.selection_label}»`),
        node('p','',`${row.market_label}${row.odds==null?'':` · коэффициент при разборе ${fmt(row.odds)}`}`));
      if(row.today_pick)lead.querySelector('h2').textContent='Претендент: '+row.selection_label;
      lead.append(node('p','pr-answer-decision',row.decision==='bet'?'Решение: Заходим':'Решение: Не заходим'));
      if(row.actual_score)lead.append(node('p','pr-final-score','Итог матча: '+row.actual_score));
      const blocks=row.blocks.map(block=>{const section=node('section','pr-answer-block');section.append(node('h3','',block.label),node('p','',block.text));return section;});
      box.replaceChildren(lead,...blocks,node('p','pr-answer-notice',row.notice));
    }catch(error){if(current===epoch)box.replaceChildren(node('p','notice error',error.message));}
  }
  function activate(next){
    clearInterval(timer);epoch++;route=next;
    window.TodayPicks?.activate(next==='#matches');
    if(next==='#results'){offset=0;list(0);timer=setInterval(()=>list(),60000);}
    else if(next.startsWith('#result/'))detail(next.slice(8));
    else if(next==='#matches'){widget();timer=setInterval(widget,60000);}
  }
  $('pr-sport').addEventListener('change',()=>list(0));$('pr-refresh').addEventListener('click',()=>list());
  $('pr-prev').addEventListener('click',()=>list(Math.max(0,offset-20)));$('pr-next').addEventListener('click',()=>list(offset+20));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)activate(route);});
  document.addEventListener('accountchange',()=>{if(route.startsWith('#result/'))detail(route.slice(8));});
  return {activate};
})();
