"use strict";
window.AdminPricing=(()=>{
  const $=id=>document.getElementById(id),api=Account.api;
  const node=(tag,cls='',text='')=>{const e=document.createElement(tag);e.className=cls;e.textContent=text;return e;};
  const rub=n=>(n/100).toLocaleString('ru-RU',{maximumFractionDigits:2})+' ₽';
  const plurals=new Intl.PluralRules('ru'),units=n=>`${n} ${{one:'разбор',few:'разбора',many:'разборов',other:'разбора'}[plurals.select(n)]}`;
  const days=n=>`${n} ${{one:'день',few:'дня',many:'дней',other:'дня'}[plurals.select(n)]}`;
  const date=s=>s?new Date(s).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'})+' МСК':'Исходные тарифы';
  const toInput=s=>s?new Date(new Date(s).getTime()+3*3600000).toISOString().slice(0,16):'';
  const fromInput=s=>s?new Date(s+':00+03:00').toISOString():null;
  const labels={pricing:'Тарифы',offer:'Оферта',recurring:'Автоплатежи','payment-refund':'Оплата и возвраты',terms:'Соглашение',privacy:'Конфиденциальность',consent:'Согласие на обработку данных','test-purchase':'Тестовый магазин'};
  let current=null,draft=null,kind='subscriptions',dirty=false,preview=null,history=[],loadedOwner=null;
  function message(text,error=false){$('pricing-message').textContent=text;$('pricing-message').classList.toggle('is-error',error);}
  function changed(event){
    dirty=true;preview=null;$('pricing-dirty').textContent='Есть неопубликованные изменения';
    const card=event?.target.closest('.pricing-editor-card');if(!card)return;
    const get=name=>card.querySelector(`[data-field="${name}"]`),n=Number(get('credits').value);
    card.querySelector('summary strong').textContent=get('unlimited')?.checked?'Безлимит':n>0?units(n):'Укажите количество';
    card.querySelector('.pricing-summary-state').textContent=get('enabled').checked?'В продаже':'Скрыт';
    try{card.querySelector('summary small').textContent=rub(money(get('amount').value))+(kind==='subscriptions'?' / '+days(Number(get('period_days').value)):' · разовая покупка');}catch{card.querySelector('summary small').textContent='Укажите цену';}
  }
  function field(card,label,type,name,value,options={}){
    const wrap=node('label','pricing-field');wrap.append(node('span','',label));const input=node('input');input.type=type;input.dataset.field=name;input.value=value??'';
    Object.assign(input,options);wrap.append(input);card.append(wrap);return input;
  }
  function checkbox(card,label,name,checked){const wrap=node('label','pricing-checkbox'),input=node('input');input.type='checkbox';input.dataset.field=name;input.checked=checked;wrap.append(input,document.createTextNode(label));card.append(wrap);return input;}
  function card(p,index){
    const box=node('details','pricing-editor-card');box.dataset.key=p.key;box.open=index===0;
    const summary=node('summary'),heading=node('span'),caption=node('strong','',p.credits?units(p.credits):'Безлимит');
    heading.append(caption,node('small','',rub(p.amount_kopecks)+(kind==='subscriptions'?' / '+days(p.period_days):' · разовая покупка')));
    summary.append(heading,node('span','pricing-summary-state',p.enabled?'В продаже':'Скрыт'));box.append(summary);
    const body=node('div','pricing-editor-body'),grid=node('div','pricing-fields');
    const unlimited=kind==='subscriptions'?checkbox(body,'Безлимитные разборы','unlimited',!p.credits):null;
    const count=field(grid,'Разборов','number','credits',p.credits||1,{min:'1',max:'10000',step:'1',required:true,disabled:!p.credits});
    field(grid,'Обычная цена, ₽','text','amount',String(p.amount_kopecks/100).replace('.',','),{inputMode:'decimal',required:true,pattern:'[0-9]+([.,][0-9]{1,2})?'});
    if(kind==='subscriptions')field(grid,'Списание раз в … дней','number','period_days',p.period_days,{min:'1',max:'365',step:'1',required:true});
    body.append(grid);checkbox(body,'Доступен для новых покупок','enabled',p.enabled);
    if(unlimited)unlimited.addEventListener('change',()=>{count.disabled=unlimited.checked;});
    const sale=node('div','pricing-sale');sale.append(node('strong','',kind==='subscriptions'?'Скидка на первый платёж':'Скидка на разовую покупку'));
    field(sale,'Скидка, % · 0 — без скидки','number','percent',p.discount.percent,{min:'0',max:'95',step:'1',required:true});
    const dates=node('div','pricing-fields');field(dates,'Начало · МСК','datetime-local','starts_at',toInput(p.discount.starts_at));field(dates,'Окончание · МСК','datetime-local','ends_at',toInput(p.discount.ends_at));sale.append(dates,node('p','small muted','Пустые даты — акция без ограничения этого срока. Окончание срока не меняет уже оформленные подписки.'));
    body.append(sale);const remove=node('button','text-button pricing-remove','Удалить из черновика');remove.type='button';remove.addEventListener('click',()=>{capture();draft[kind]=draft[kind].filter(i=>i.key!==p.key);changed();render();});body.append(remove);box.append(body);return box;
  }
  function money(value){const s=String(value).trim().replace(',','.');if(!/^[0-9]{1,7}(\.[0-9]{1,2})?$/.test(s))throw new Error('Укажите цену в рублях, максимум два знака после запятой.');const [r,c='']=s.split('.');return Number(r)*100+Number(c.padEnd(2,'0'));}
  function capture(){
    if(!draft)return;
    draft[kind]=[...$('pricing-cards').querySelectorAll('.pricing-editor-card')].map(box=>{
      const get=name=>box.querySelector(`[data-field="${name}"]`),sale={percent:Number(get('percent').value),starts_at:fromInput(get('starts_at').value),ends_at:fromInput(get('ends_at').value)};
      return {key:box.dataset.key,credits:get('unlimited')?.checked?0:Number(get('credits').value),amount_kopecks:money(get('amount').value),period_days:kind==='subscriptions'?Number(get('period_days').value):null,enabled:get('enabled').checked,discount:sale};
    });
  }
  function render(){
    $('pricing-section-title').textContent=kind==='subscriptions'?'Подписки':'Разовые покупки';
    $('pricing-cards').replaceChildren(...draft[kind].map(card));$('pricing-add').disabled=draft[kind].length>=8;
    document.querySelectorAll('[data-pricing-kind]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.pricingKind===kind)));
    if(!draft[kind].length)$('pricing-cards').append(node('p','pricing-empty','Добавьте тариф или переключитесь в другой раздел.'));
    $('pricing-version').textContent=current.published_at?'Опубликовано '+date(current.published_at):'Исходные тарифы';
    const items=history.map(v=>{const row=node('div','pricing-history-row');row.append(node('span','',date(v.published_at)));const a=node('a','','Сохранённые тарифы');a.href='/legal/versions/'+v.revision+'/pricing';a.target='_blank';a.rel='noopener';row.append(a);return row;});
    $('pricing-history').replaceChildren(...(items.length?items:[node('p','small muted','Публикаций через редактор пока нет.')]));
  }
  async function load(force=false){
    if(dirty&&!force&&loadedOwner===Account.user?.id)return;
    const result=await api('/api/admin/pricing');current=result.current;history=result.history;loadedOwner=Account.user?.id;
    draft=result.draft?.base_revision===current.revision?result.draft.draft:{subscriptions:structuredClone(current.subscriptions),packages:structuredClone(current.packages)};
    dirty=!!result.draft;preview=null;render();$('pricing-dirty').textContent=dirty?'Загружен сохранённый черновик':'Изменений нет';
    if(result.draft&&result.draft.base_revision!==current.revision){dirty=false;message('Сохранённый черновик относится к старой публикации. Загружены актуальные тарифы.',true);}
  }
  function body(){capture();return {base_revision:current.revision,draft};}
  function showDocument(){
    const html=preview.documents[$('pricing-document-select').value],document=new DOMParser().parseFromString(html,'text/html'),main=document.querySelector('main');
    main.querySelectorAll('script').forEach(e=>e.remove());main.querySelectorAll('a').forEach(a=>a.removeAttribute('href'));
    $('pricing-document').replaceChildren(...[...main.childNodes].map(n=>document.importNode(n,true)));
  }
  function showPreview(result){
    preview=result;$('pricing-preview-error').textContent='';
    const plans=[...result.subscriptions.map(p=>({...p,subscription:true})),...result.packages];
    $('pricing-preview-cards').replaceChildren(...plans.map(p=>{const c=node('article','pricing-preview-card');c.append(node('span','small muted',p.subscription?'Подписка':'Разовая покупка'),node('h3','',p.credits?units(p.credits):'Безлимит'),node('strong','',rub(p.amount_kopecks)));
      c.append(node('p','',p.subscription?`Первый платёж. Затем ${rub(p.renewal_amount_kopecks)} раз в ${days(p.period_hours/24)}.`:'Один платёж, без автопродления.'));if(p.discount_percent)c.append(node('span','credit-discount',`Скидка ${p.discount_percent}%${p.subscription?' только на первый платёж':''}`));return c;}));
    $('pricing-document-select').replaceChildren(...Object.keys(result.documents).map(name=>new Option(labels[name]||name,name)));showDocument();$('pricing-preview').showModal();
  }
  async function action(button,fn){button.disabled=true;try{await fn();}catch(e){message(e.message,true);}finally{button.disabled=false;}}
  $('pricing-form').addEventListener('input',changed);$('pricing-form').addEventListener('change',changed);
  document.querySelectorAll('[data-pricing-kind]').forEach(b=>b.addEventListener('click',()=>{try{capture();kind=b.dataset.pricingKind;render();}catch(e){message(e.message,true);}}));
  $('pricing-add').addEventListener('click',()=>{try{capture();draft[kind].push({key:'plan_'+crypto.randomUUID().replaceAll('-','').slice(0,16),credits:1,amount_kopecks:49000,period_days:kind==='subscriptions'?3:null,enabled:true,discount:{percent:0,starts_at:null,ends_at:null}});changed();render();const last=$('pricing-cards').lastElementChild;last.open=true;last.querySelector('input')?.focus();}catch(e){message(e.message,true);}});
  $('pricing-reload').addEventListener('click',()=>action($('pricing-reload'),async()=>{const r=await api('/api/admin/pricing');current=r.current;history=r.history;draft={subscriptions:structuredClone(current.subscriptions),packages:structuredClone(current.packages)};dirty=false;preview=null;render();message('Загружены опубликованные тарифы.');$('pricing-dirty').textContent='Изменений нет';}));
  $('pricing-save').addEventListener('click',()=>action($('pricing-save'),async()=>{if(!$('pricing-form').reportValidity())return;await api('/api/admin/pricing/draft',{method:'POST',body:JSON.stringify(body())});message('Черновик сохранён. Посетители пока видят опубликованные тарифы.');}));
  $('pricing-preview-open').addEventListener('click',()=>action($('pricing-preview-open'),async()=>{if(!$('pricing-form').reportValidity())return;const result=await api('/api/admin/pricing/preview',{method:'POST',body:JSON.stringify(body())});showPreview(result);}));
  $('pricing-preview-close').addEventListener('click',()=>$('pricing-preview').close());$('pricing-document-select').addEventListener('change',showDocument);
  $('pricing-publish').addEventListener('click',()=>action($('pricing-publish'),async()=>{
    try{await Admin.sensitive(async()=>{await api('/api/admin/pricing/publish',{method:'POST',body:JSON.stringify({...body(),preview_hash:preview.preview_hash})});dirty=false;$('pricing-preview').close();await load(true);message('Тарифы, скидки и документы опубликованы. Изменения действуют для новых покупок.');document.dispatchEvent(new CustomEvent('pricingchange'));});}
    catch(e){$('pricing-preview-error').textContent=e.message;}
  }));
  document.addEventListener('accountchange',()=>{if(Account.user?.id!==loadedOwner||Account.user?.role!=='admin'){current=null;draft=null;dirty=false;preview=null;loadedOwner=null;$('pricing-cards').replaceChildren();$('pricing-document').replaceChildren();$('pricing-preview-cards').replaceChildren();$('pricing-document-select').replaceChildren();$('pricing-history').replaceChildren();$('pricing-preview').close();}});
  return {load};
})();
