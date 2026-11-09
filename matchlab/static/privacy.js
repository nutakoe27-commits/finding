"use strict";
window.Privacy = (() => {
  let allowed = false, replay = false;
  const banner = document.getElementById('privacy-banner');
  const status = document.getElementById('privacy-message');
  const replayControl = document.getElementById('privacy-webvisor');
  async function save(analytics) {
    status.textContent = '';
    const webvisor=analytics && replayControl.checked;
    const reload=window.ProductAnalytics?.started && (!analytics || replay!==webvisor);
    if(reload && (document.getElementById('notes')?.value.trim() || document.getElementById('explanation')?.getAttribute('aria-busy')==='true') && !window.confirm('Для изменения настроек страница обновится. Несохранённые заметки и текущий запрос могут быть потеряны. Продолжить?')) return;
    try {
      const response = await fetch('/api/privacy/preferences', {method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({analytics,webvisor})});
      if (!response.ok) throw new Error('Не удалось сохранить настройку. Попробуйте ещё раз.');
      const wasAllowed=allowed;allowed=analytics;replay=webvisor;
      banner.hidden = true;
      if(reload) {
        if(!analytics) for(const part of document.cookie.split(';')) {
          const name=part.trim().split('=')[0];
          if(name.startsWith('_ym_')) {
            document.cookie=name+'=; Max-Age=0; path=/; SameSite=Lax';
            document.cookie=name+'=; Max-Age=0; path=/; domain='+location.hostname+'; SameSite=Lax';
          }
        }
        location.reload();return;
      }
      if(allowed && !wasAllowed) { window.ProductAnalytics?.start({analytics:true,webvisor});window.Account?.track('page_view',location.hash || '#matches'); }
    } catch (error) { status.textContent = error.message; }
  }
  document.getElementById('privacy-allow').addEventListener('click', () => save(true));
  document.getElementById('privacy-reject').addEventListener('click', () => save(false));
  document.getElementById('privacy-settings').addEventListener('click', () => { replayControl.checked=replay;banner.hidden = false; document.getElementById('privacy-reject').focus(); });
  fetch('/api/privacy/preferences',{credentials:'same-origin'}).then(r=>r.ok?r.json():Promise.reject()).then(data=>{
    allowed = data.analytics === true;
    replay=allowed && data.webvisor===true;replayControl.checked=replay;
    if (data.analytics === null) banner.hidden = false;
    if (allowed) { window.ProductAnalytics?.start({analytics:true,webvisor:replay});window.Account?.track('page_view',location.hash || '#matches'); }
  }).catch(()=>{ banner.hidden = false; });
  const ageGate=document.getElementById('age-gate');
  let confirmed=false;
  try { confirmed=localStorage.getItem('matchlab:adult:v1')==='yes'; } catch { /* Ask again in this session. */ }
  if (!confirmed) ageGate.showModal();
  ageGate.addEventListener('cancel',event=>event.preventDefault());
  document.getElementById('age-accept').addEventListener('click',()=>{
    try { localStorage.setItem('matchlab:adult:v1','yes'); } catch { /* The confirmation lasts for this page. */ }
    ageGate.close();
  });
  document.getElementById('age-decline').addEventListener('click',()=>{
    document.getElementById('age-message').hidden=false;
  });
  return {get analyticsAllowed(){return allowed;}};
})();
