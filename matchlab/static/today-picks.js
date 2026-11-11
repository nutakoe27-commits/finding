"use strict";
window.TodayPicks=(()=>{
  const $=id=>document.getElementById(id),root=$('today-picks');
  const node=(tag,cls='',value='')=>{const e=document.createElement(tag);e.className=cls;e.textContent=value;return e;};
  const odds=value=>Number(value).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});
  const time=value=>new Date(value).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'});
  const when=value=>new Date(value).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  let active=false,controller=null,timer=null,epoch=0,data=null,shown=6;
  let filters={};
  function card(pick){
    const box=node('article','tp-card tp-bet');box.setAttribute('role','listitem');
    const head=node('div','tp-card-head');head.append(node('span','tp-sport',pick.sport_label),node('span','tp-kickoff',time(pick.event.kickoff_at)+' МСК'));
    const competition=node('p','tp-competition',pick.event.competition);competition.title=pick.event.competition;
    const match=node('h3','tp-match',`${pick.event.home} — ${pick.event.away}`);
    const preview=node('div','tp-preview');preview.append(node('span','tp-ready-dot'),node('span','','Прогноз готов · команда и аргументы внутри'));
    const line=node('div','tp-line'),price=node('div','tp-price');price.append(node('span','tp-label','Коэффициент при разборе'),node('strong','',odds(pick.odds)));line.append(price);
    const footer=node('div','tp-card-footer'),link=node('a','tp-open','Посмотреть прогноз →');link.href='#result/'+pick.id;link.setAttribute('aria-label',`Посмотреть прогноз: ${pick.event.home} — ${pick.event.away}`);
    footer.append(node('span','tp-observed','Разбор '+when(pick.forecast_at)+' МСК'),link);
    box.append(head,competition,match,preview,line,footer);return box;
  }
  function render(){
    const picks=data.picks.filter(pick=>new Date(pick.event.kickoff_at).getTime()>Date.now());
    const cards=$('tp-cards'),focused=document.activeElement?.closest('#tp-cards .tp-open')?.getAttribute('href');
    cards.replaceChildren(...picks.slice(0,shown).map(card));
    if(focused)[...cards.querySelectorAll('.tp-open')].find(link=>link.getAttribute('href')===focused)?.focus({preventScroll:true});
    if(!picks.length){
      const box=node('div','tp-empty');box.append(node('strong','',data.available?'По этим фильтрам прогнозов нет':'На сегодня готовых прогнозов пока нет'),node('p','',data.available?'Попробуйте расширить диапазон коэффициентов.':'Карточки появятся автоматически, когда системный анализ сохранит вердикт «Ставить» для сегодняшнего матча.'));
      cards.append(box);
    }
    $('tp-more').hidden=picks.length<=shown;$('tp-more').textContent='Показать ещё · осталось '+Math.max(0,picks.length-shown);
    $('tp-date').textContent=new Date(data.checked_at).toLocaleDateString('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'long'});
    $('tp-status').textContent=`${picks.length} разборов${data.available!==picks.length?' из '+data.available:''} · обновлено ${time(data.checked_at)} МСК`;
  }
  async function load(){
    if(!active||document.hidden)return;clearTimeout(timer);controller?.abort();controller=new AbortController();const serial=++epoch;
    $('tp-status').textContent='Обновляем сохранённые разборы…';$('tp-cards').setAttribute('aria-busy','true');
    try{
      const response=await fetch('/api/picks/today?'+new URLSearchParams(filters),{signal:controller.signal,cache:'no-store'});
      if(!response.ok)throw new Error('Не удалось загрузить подборку. Нажмите «Применить», чтобы повторить.');
      const result=await response.json();if(serial!==epoch||!active)return;
      data=result;render();$('tp-status').classList.remove('is-error');
    }catch(error){if(serial===epoch&&error.name!=='AbortError'){$('tp-status').textContent=error.message;$('tp-status').classList.add('is-error');}}
    finally{if(serial===epoch){$('tp-cards').setAttribute('aria-busy','false');if(active)timer=setTimeout(load,30000);}}
  }
  $('tp-filters').addEventListener('submit',event=>{
    event.preventDefault();const min=$('tp-min'),max=$('tp-max');max.setCustomValidity('');
    if(min.value&&max.value&&Number(min.value)>Number(max.value)){max.setCustomValidity('Значение «до» должно быть не меньше значения «от».');max.reportValidity();return;}
    filters={};if(min.value)filters.min_odds=min.value;if(max.value)filters.max_odds=max.value;
    shown=6;load();
  });
  for(const id of ['tp-min','tp-max'])$(id).addEventListener('input',()=>$('tp-max').setCustomValidity(''));
  $('tp-reset').addEventListener('click',()=>{$('tp-filters').reset();$('tp-max').setCustomValidity('');filters={};shown=6;load();});
  $('tp-more').addEventListener('click',()=>{shown+=6;render();});
  function activate(value){
    active=value;root.hidden=!value;clearTimeout(timer);
    if(!value){epoch++;controller?.abort();$('tp-cards').setAttribute('aria-busy','false');return;}
    load();
  }
  document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(timer);controller?.abort();}});
  return {activate};
})();
