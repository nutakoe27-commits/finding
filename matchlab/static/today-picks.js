"use strict";
window.TodayPicks=(()=>{
  const $=id=>document.getElementById(id),root=$('today-picks');
  const node=(tag,cls='',value='')=>{const e=document.createElement(tag);e.className=cls;e.textContent=value;return e;};
  const odds=value=>Number(value).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});
  const time=value=>new Date(value).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'});
  const when=value=>new Date(value).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  const dateKey=value=>new Date(value).toLocaleDateString('en-CA',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'});
  const day=value=>dateKey(value)===data.date?'Сегодня':'Завтра';
  const plural=new Intl.PluralRules('ru'),count=value=>`${value} ${{one:'разбор',few:'разбора',many:'разборов',other:'разбора'}[plural.select(value)]}`;
  let active=false,controller=null,timer=null,epoch=0,data=null,shown=6;
  function card(pick){
    const box=node('article','tp-card tp-bet');box.setAttribute('role','listitem');
    const head=node('div','tp-card-head'),kickoff=node('span','tp-kickoff tp-kickoff-blurred',day(pick.event.kickoff_at)+', '+time(pick.event.kickoff_at)+' МСК');
    kickoff.setAttribute('aria-hidden','true');head.append(node('span','tp-sport',pick.sport_label),kickoff);
    const competition=node('p','tp-competition tp-competition-blurred','Название турнира');competition.setAttribute('aria-hidden','true');
    const match=node('h3','tp-match'),names=node('span','tp-match-blurred','Участник 1 — Участник 2');names.setAttribute('aria-hidden','true');
    match.append(names,node('span','sr-only','Участники скрыты до открытия прогноза'));
    const preview=node('div','tp-preview');preview.append(node('span','tp-ready-dot'),node('span','','Прогноз готов · команда и аргументы внутри'));
    const line=node('div','tp-line'),price=node('div','tp-price');price.append(node('span','tp-label','Коэффициент при разборе'),node('strong','',odds(pick.odds)));line.append(price);
    const footer=node('div','tp-card-footer'),link=node('a','tp-open','Посмотреть прогноз →');link.href='#result/'+pick.id;link.setAttribute('aria-label',`Посмотреть прогноз: ${pick.sport_label}, коэффициент ${odds(pick.odds)}`);
    footer.append(node('span','tp-observed','Разбор '+when(pick.forecast_at)+' МСК'),link);
    box.append(head,competition,match,preview,line,footer);return box;
  }
  function render(){
    const picks=data.picks.filter(pick=>new Date(pick.event.kickoff_at).getTime()>Date.now());
    const cards=$('tp-cards'),focused=document.activeElement?.closest('#tp-cards .tp-open')?.getAttribute('href');
    cards.replaceChildren(...picks.slice(0,shown).map(card));
    if(focused)[...cards.querySelectorAll('.tp-open')].find(link=>link.getAttribute('href')===focused)?.focus({preventScroll:true});
    if(!picks.length){
      const box=node('div','tp-empty');box.append(node('strong','','На сегодня и завтра готовых прогнозов пока нет'),node('p','','Карточки появятся автоматически, когда системный анализ сохранит вердикт «Ставить» для матча на сегодня или завтра.'));
      cards.append(box);
    }
    $('tp-more').hidden=picks.length<=shown;$('tp-more').textContent='Показать ещё · осталось '+Math.max(0,picks.length-shown);
    const dateFormat={timeZone:'Europe/Moscow',day:'numeric',month:'long'};
    const start=new Date(data.date+'T12:00:00Z').toLocaleDateString('ru-RU',dateFormat);
    const end=new Date((data.through_date||data.date)+'T12:00:00Z').toLocaleDateString('ru-RU',dateFormat);
    $('tp-date').textContent=start===end?start:start+' — '+end;
    $('tp-status').textContent=`${count(picks.length)} · обновлено ${time(data.checked_at)} МСК`;
  }
  async function load(){
    if(!active||document.hidden)return;clearTimeout(timer);controller?.abort();controller=new AbortController();const serial=++epoch;
    $('tp-status').textContent='Обновляем сохранённые разборы…';$('tp-cards').setAttribute('aria-busy','true');
    try{
      const response=await fetch('/api/picks/today',{signal:controller.signal,cache:'no-store'});
      if(!response.ok)throw new Error('Не удалось загрузить подборку. Попробуйте ещё раз.');
      const result=await response.json();if(serial!==epoch||!active)return;
      data=result;render();$('tp-status').classList.remove('is-error');$('tp-retry').hidden=true;
    }catch(error){if(serial===epoch&&error.name!=='AbortError'){$('tp-status').textContent=error.message;$('tp-status').classList.add('is-error');$('tp-retry').hidden=false;}}
    finally{if(serial===epoch){$('tp-cards').setAttribute('aria-busy','false');if(active)timer=setTimeout(load,30000);}}
  }
  $('tp-retry').addEventListener('click',load);
  $('tp-more').addEventListener('click',()=>{shown+=6;render();});
  function activate(value){
    active=value;root.hidden=!value;clearTimeout(timer);
    if(!value){epoch++;controller?.abort();$('tp-cards').setAttribute('aria-busy','false');return;}
    load();
  }
  document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(timer);controller?.abort();}});
  return {activate};
})();
