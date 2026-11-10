"use strict";
window.FeaturedEvents=(()=>{
  const $=id=>document.getElementById(id),root=$('featured-events'),track=$('featured-track');
  const node=(tag,cls='',text='')=>{const e=document.createElement(tag);e.className=cls;e.textContent=text;return e;};
  let controller,serial=0,props=null;
  const motion=()=>matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth';
  function sync(){
    const card=track.firstElementChild;if(!card)return;
    const width=card.getBoundingClientRect().width+16,start=Math.round(track.scrollLeft/width);
    const visible=Math.max(1,Math.floor((track.clientWidth+16)/width));
    $('featured-position').textContent=`${start+1}–${Math.min(track.children.length,start+visible)} / ${track.children.length}`;
    $('featured-prev').disabled=track.scrollLeft<=3;$('featured-next').disabled=track.scrollLeft+track.clientWidth>=track.scrollWidth-2;
  }
  function move(direction){const card=track.firstElementChild;if(card)track.scrollBy({left:direction*(card.getBoundingClientRect().width+16),behavior:motion()});}
  $('featured-prev').addEventListener('click',()=>move(-1));$('featured-next').addEventListener('click',()=>move(1));
  track.addEventListener('scroll',sync,{passive:true});track.addEventListener('keydown',e=>{if(e.target===track&&['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();move(e.key==='ArrowRight'?1:-1);}});
  new ResizeObserver(sync).observe(track);
  function hide(){controller?.abort();serial++;root.hidden=true;}
  function render(events,p){
    const scroll=track.scrollLeft,focused=document.activeElement?.dataset.featured;
    const cards=events.map(e=>{
      const card=node('article','featured-card');card.setAttribute('role','listitem');card.dataset.eventId=e.source_event_id;
      const top=node('div','featured-top'),competition=node('span','featured-competition',e.competition);competition.title=e.competition;
      top.append(node('span',e.phase==='live'?'featured-phase live':'featured-phase',e.phase==='live'?'● LIVE':e.sport_label||'Матч'),competition);
      const main=node('a','featured-main');main.href=p.route(e);main.dataset.featured='match-'+e.source_event_id;
      const teams=node('div','featured-teams');
      for(const [side,label] of [['home','Хозяева'],['away','Гости']]){
        const row=node('div','featured-team'),initial=String(e[side]).trim().split(/\s+/).map(w=>w[0]).slice(0,2).join('').toUpperCase();
        const mark=node('span','featured-team-mark '+side,initial);mark.setAttribute('aria-hidden','true');
        row.append(mark,node('strong','',e[side]));teams.append(row);
      }
      main.append(teams);
      if(e.phase==='live')main.append(node('div','featured-score',e.live_score||'Матч идёт'));
      const time=e.kickoff_at?new Date(e.kickoff_at).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'Время уточняется';
      const timing=node('p','featured-time',e.phase==='live'?(e.live_clock||'Счёт на момент обновления'):time+' МСК');
      const preview=e.headline||e.primary_odds,odds=node('div','featured-odds');
      let outcomes=preview?.outcomes;
      if(!outcomes&&preview&&['home','draw','away'].every(s=>Number.isFinite(preview[s])))outcomes=[['home','П1'],['draw','Х'],['away','П2']].map(([s,name])=>({name,odds:preview[s],state:'open'}));
      if(outcomes?.length>=2&&outcomes.length<=3){
        for(const o of outcomes){const tile=node('div');tile.append(node('span','',o.name),node('b','',o.state==='open'&&Number.isFinite(o.odds)?o.odds.toFixed(2):'—'));odds.append(tile);}
      }else odds.append(node('span','small muted','Коэффициенты — на странице матча'));
      const bottom=node('div','featured-bottom'),ai=node('button','featured-ai','Разбор ИИ');ai.type='button';ai.dataset.featured='ai-'+e.source_event_id;
      if(e.phase==='live'&&p.neural)ai.textContent='О разборе ИИ';
      ai.addEventListener('click',()=>p.onAI(e));bottom.append(node('span','featured-freshness',e.is_stale?'Данные требуют обновления':e.phase==='live'?'Матч идёт':'До начала матча'),ai);
      card.append(top,main,timing,odds,bottom);return card;
    });
    track.replaceChildren(...cards);root.hidden=!cards.length;track.scrollLeft=scroll;
    if(focused)[...track.querySelectorAll('[data-featured]')].find(n=>n.dataset.featured===focused)?.focus({preventScroll:true});
    requestAnimationFrame(sync);
  }
  async function update(p){
    props=p;if(!p.visible){hide();return;}
    controller?.abort();controller=new AbortController();const epoch=++serial;
    try{
      const query=new URLSearchParams({limit:'100'});
      const response=await fetch('/api/bookmakers/featured-events?'+query,{signal:controller.signal,cache:'no-store'});
      if(!response.ok)throw new Error('Unavailable');const d=await response.json();
      if(epoch!==serial||!props?.visible)return;render(d.events||[],p);
    }catch(e){if(epoch===serial&&e.name!=='AbortError')root.hidden=true;}
  }
  return {update,hide};
})();
