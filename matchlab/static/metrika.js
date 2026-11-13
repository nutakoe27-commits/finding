"use strict";
// Yandex is loaded only after the visitor has explicitly enabled analytics.
window.ProductAnalytics = (() => {
  const counter = 114139580;
  const goals = new Set(['registration_success','login_success','match_view','ai_intent','ai_request_start','ai_model_result','ai_summary_result','ai_request_error','favorite_add','promo_redeemed','push_subscribed']);
  let started = false, currentRoute = null;
  const fields = root => {
    if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
    if (root.matches?.('input,textarea')) root.classList.add('ym-disable-keys');
    root.querySelectorAll?.('input,textarea').forEach(element=>element.classList.add('ym-disable-keys'));
  };
  function safeUrl(hash=location.hash) {
    const legal=location.pathname.match(/^\/static\/legal\/(terms|privacy|consent|cookies|advertising-consent)\.html$/);
    if(legal) return location.origin+'/legal/'+legal[1];
    const route=(hash || '#matches').split('/')[0];
    const known=new Set(['#matches','#favorites','#archive','#history','#status','#account','#admin','#calculator','#analysis','#match','#stream']);
    return location.origin + '/' + (known.has(route) ? route : '#matches');
  }
  function safeReferrer() {
    try { const url=new URL(document.referrer); return ['http:','https:'].includes(url.protocol) ? url.origin+'/' : ''; }
    catch { return ''; }
  }
  function start(options) {
    if(started || options?.analytics !== true) return;
    fields(document);
    new MutationObserver(records=>records.forEach(record=>record.addedNodes.forEach(fields))).observe(document.documentElement,{childList:true,subtree:true});
    window.dataLayer = window.dataLayer || [];
    window.ym = window.ym || function(){ (window.ym.a=window.ym.a||[]).push(arguments); };
    window.ym.l=Date.now();
    const src='https://mc.yandex.ru/metrika/tag.js?id='+counter;
    if (![...document.scripts].some(script=>script.src===src)) {
      const script=document.createElement('script');script.async=true;script.src=src;
      document.head.appendChild(script);
    }
    started=true;
    currentRoute=location.hash || '#matches';
    window.ym(counter,'init',{ssr:true,webvisor:options.webvisor===true,clickmap:true,ecommerce:'dataLayer',referrer:safeReferrer(),url:safeUrl(),accurateTrackBounce:true,trackLinks:false});
    if(/^#(?:match|stream)\/\d{1,20}$/.test(currentRoute)) goal('match_view');
  }
  function goal(name) {
    if(started && window.Privacy?.analyticsAllowed && goals.has(name)) window.ym(counter,'reachGoal',name);
  }
  function hit(hash) {
    if(!started || !window.Privacy?.analyticsAllowed) return;
    const route=hash || '#matches';
    if(route===currentRoute) return;
    currentRoute=route;window.ym(counter,'hit',safeUrl(route));
    if(/^#(?:match|stream)\/\d{1,20}$/.test(route)) goal('match_view');
  }
  document.addEventListener('click',event=>{
  });
  return {start,goal,hit,get started(){return started;}};
})();
