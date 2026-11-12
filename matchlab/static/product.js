"use strict";
// Product interactions stay separate from the match data and verdict protocols.
window.ProductUX = (() => {
  const $ = id => document.getElementById(id);
  const positions = new Map();
  const fields = ['match-search','competition-filter','day-filter','sort-filter'];
  const paths = {
    matches:'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
    favorite:'m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z',
    history:'M4 11a8 8 0 1 1 2 6M4 4v7h7M12 7v5l3 2',
    search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
    spark:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4M18 4h4',
    arrow:'M7 17 17 7M7 7h10v10',
    clock:'M12 8v4l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
    chart:'M4 4v16h16M8 15l4-5 4 2 4-7',
    close:'m6 6 12 12M6 18 18 6',
    refresh:'M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1',
    previous:'m14 6-6 6 6 6',
    next:'m10 6 6 6-6 6'
  };
  function icon(name) {
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');svg.classList.add('ux-icon');
    const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',paths[name]||paths.spark);svg.append(path);return svg;
  }
  document.querySelectorAll('[data-ux-icon]').forEach(node=>node.replaceChildren(icon(node.dataset.uxIcon)));
  function capture(view,state) {
    if(!['#matches','#favorites'].includes(view))return;
    positions.set(view,{offset:state.marketOffset,sport:state.sport,phase:state.phase,scroll:window.scrollY,
      fields:Object.fromEntries(fields.map(id=>[id,$(id).value]))});
  }
  function restore(view) {
    const position=positions.get(view);if(!position)return null;
    for(const id of fields) {
      const input=$(id),value=position.fields[id];
      if(input.tagName==="SELECT"&&value&&![...input.options].some(option=>option.value===value)) input.add(new Option(value,value));
      input.value=value;
    }
    return position;
  }
  function replaceContent(id,nodes) {
    const box=$(id),active=document.activeElement;
    const key=box.contains(active)?active.dataset.focusKey:null;
    const existing=new Map([...box.children].filter(node=>node.dataset.focusKey).map(node=>[node.dataset.focusKey,node]));
    nodes=nodes.map(node=>{
      const previous=existing.get(node.dataset.focusKey);
      return previous?.outerHTML===node.outerHTML?previous:node;
    });
    box.replaceChildren(...nodes);
    if(key)[...box.querySelectorAll('[data-focus-key]')].find(node=>node.dataset.focusKey===key)?.focus({preventScroll:true});
  }
  function replaceList(nodes) {
    const box=$('market-event-rows'),active=document.activeElement;
    const focused=box.contains(active)?active.dataset.focusKey:null;
    const existing=new Map([...box.querySelectorAll('.match-card')].map(node=>[node.dataset.eventId,node]));
    const signature=node=>{const copy=node.cloneNode(true);copy.querySelector('.match-freshness')?.remove();return copy.outerHTML;};
    for(const group of nodes)for(const card of group.querySelectorAll('.match-card')){
      const previous=existing.get(card.dataset.eventId);
      if(previous&&signature(previous)===signature(card)){
        const fresh=card.querySelector('.match-freshness');
        if(fresh)previous.querySelector('.match-freshness')?.replaceChildren(...fresh.childNodes);
        card.replaceWith(previous);
      }
    }
    replaceContent('market-event-rows',nodes);
    if(focused)[...box.querySelectorAll('[data-focus-key]')].find(node=>node.dataset.focusKey===focused)?.focus({preventScroll:true});
  }
  function filters(items) {
    const box=$('active-filters');
    const key=box.contains(document.activeElement)?document.activeElement.dataset.filterKey:null;
    box.replaceChildren(...items.map(item=>{
      const button=document.createElement('button');button.type='button';button.className='filter-chip';
      button.dataset.filterKey=item.key;button.append(document.createTextNode(item.label),icon('close'));
      button.setAttribute('aria-label','Убрать фильтр: '+item.label);button.addEventListener('click',()=>{
        item.clear();$('match-search').focus({preventScroll:true});
      });return button;
    }));
    box.hidden=!items.length;
    if(key)box.querySelector(`[data-filter-key="${key}"]`)?.focus({preventScroll:true});
  }
  let started=0;
  function startWait(){started=Date.now();wait('statistics');}
  function wait(stage) {
    const data=['statistics','markets'].includes(stage),queued=stage==='queued';
    $('ai-progress').dataset.stage=data?'data':'analysis';
    $('ai-progress-analysis').textContent=queued?'Ожидание очереди':'Разбор ИИ';
    const note=$('ai-wait-note');
    if(note.dataset.stage!==stage && ['queued','background'].includes(stage)) {
      const link=document.createElement('a');link.href='#history';link.textContent='вашей истории';
      note.replaceChildren('Задание принято. Разбор сохранится в ',link,'. Можно посмотреть другие матчи.');
    } else if(note.dataset.stage!==stage) note.textContent='Оставайтесь на странице, пока подготавливается задание. Затем можно будет вернуться к нему в истории.';
    note.dataset.stage=stage;
    const seconds=Math.max(0,Math.floor((Date.now()-started)/1000));
    $('ai-wait-elapsed').textContent=`${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;
  }
  function filterHistory() {
    const value=$('history-search').value.trim().toLocaleLowerCase('ru-RU');
    const cards=[...$('history-list').children];let count=0;
    cards.forEach(card=>{card.hidden=value&&!card.textContent.toLocaleLowerCase('ru-RU').includes(value);if(!card.hidden)count++;});
    $('history-search-status').textContent=value?`Найдено: ${count} из ${cards.length}`:cards.length?`Сохранено разборов: ${cards.length}`:'';
    $('history-search-empty').hidden=!value||count>0||!cards.length;
  }
  $('history-search').addEventListener('input',filterHistory);
  function reportText(node) {
    if(node.nodeType===Node.TEXT_NODE)return node.textContent;
    if(node.nodeType!==Node.ELEMENT_NODE)return '';
    if(node.tagName==='BR')return '\n';
    const text=[...node.childNodes].map(reportText).join('');
    if(node.tagName==='LI')return '- '+text.trim()+'\n';
    return /^(H[1-6]|P|DIV|SECTION|UL|OL|DETAILS|SUMMARY)$/.test(node.tagName)?text.trim()+'\n\n':text;
  }
  document.querySelectorAll('[data-copy-target]').forEach(button=>button.addEventListener('click',async()=>{
    try {
      await navigator.clipboard.writeText(reportText($(button.dataset.copyTarget)).replace(/\n{3,}/g,'\n\n').trim());
      $('ux-status').textContent='Разбор скопирован';button.textContent='Скопировано';
      setTimeout(()=>{button.textContent='Копировать разбор';},2200);
    } catch {
      $('ux-status').textContent='Не удалось скопировать. Можно выделить текст разбора вручную.';
      button.textContent='Выделите текст вручную';setTimeout(()=>{button.textContent='Копировать разбор';},3000);
    }
  }));
  $('auth-password-toggle').addEventListener('click',()=>{
    const input=$('auth-password'),visible=input.type==='password';input.type=visible?'text':'password';
    $('auth-password-toggle').textContent=visible?'Скрыть':'Показать';$('auth-password-toggle').setAttribute('aria-pressed',String(visible));
  });
  $('match-search-clear').addEventListener('click',()=>{
    $('match-search').value='';$('match-search').dispatchEvent(new Event('input'));$('match-search').focus();
  });
  $('match-search').addEventListener('keydown',event=>{
    if(event.key==='Escape'&&$('match-search').value){event.preventDefault();$('match-search-clear').click();}
    if(event.key==='Enter'){$('catalog-section').scrollIntoView({block:'start',behavior:'auto'});}
  });
  return {icon,capture,restore,replaceContent,replaceList,filters,startWait,wait,filterHistory};
})();
