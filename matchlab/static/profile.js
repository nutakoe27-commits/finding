"use strict";
window.Profile=(()=>{
  const $=id=>document.getElementById(id),root=$('view-account'),tabs=[...root.querySelectorAll('[data-profile-tab]')];
  const labels={local:'Вход по логину и паролю',yandex:'Вход через Яндекс',vk:'Вход через ВКонтакте',ok:'Вход через Одноклассники',mail:'Вход через Mail.ru'};
  let owner;
  function select(name,focus=false,remember=true){
    if(!tabs.some(b=>b.dataset.profileTab===name))name='analyses';
    for(const b of tabs){const active=b.dataset.profileTab===name;b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1;$('profile-panel-'+b.dataset.profileTab).hidden=!active;if(active&&focus)b.focus();}
    if(remember&&Account.user)try{sessionStorage.setItem('matchlab:profile-view',JSON.stringify({owner:Account.user.id,tab:name}));}catch{}
  }
  function identity(user){
    const name=user?.display_name||user?.username||'Профиль';
    $('profile-avatar').textContent=name.split(/[\s_-]+/u).filter(Boolean).slice(0,2).map(x=>Array.from(x)[0]).join('').toLocaleUpperCase('ru-RU');
    $('account-name').title=name;$('profile-login-method').textContent=labels[user?.login_provider]||'Личный аккаунт';$('account-usage').hidden=user?.role!=='admin';
    if(owner!==user?.id){
      owner=user?.id;let chosen='analyses';
      if(user)try{const saved=JSON.parse(sessionStorage.getItem('matchlab:profile-view')||'null');if(saved?.owner===user.id)chosen=saved.tab;}catch{}
      if(new URLSearchParams(location.search).get('billing_demo')==='1'&&user?.role==='admin')chosen='analyses';
      select(chosen,false,false);
      for(const d of root.querySelectorAll('.profile-setting,#profile-promo'))d.open=false;
      for(const id of ['profile-password-feedback','profile-promo-feedback']){const p=$(id);if(p)p.textContent='';}
      if(!user){$('account-old-password').value='';$('account-new-password').value='';$('promo-code').value='';}
    }
  }
  for(const [i,b] of tabs.entries()){
    b.addEventListener('click',()=>select(b.dataset.profileTab));
    b.addEventListener('keydown',e=>{let next;if(e.key==='ArrowRight')next=(i+1)%tabs.length;else if(e.key==='ArrowLeft')next=(i+tabs.length-1)%tabs.length;else if(e.key==='Home')next=0;else if(e.key==='End')next=tabs.length-1;else return;e.preventDefault();select(tabs[next].dataset.profileTab,true);});
  }
  document.addEventListener('accountchange',()=>identity(Account.user));
  Account.ready.then(()=>identity(Account.user));
  return {select,identity};
})();
