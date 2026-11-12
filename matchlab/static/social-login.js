"use strict";
window.SocialLogin=(()=>{
  const $=id=>document.getElementById(id),dialog=$('auth-dialog');
  const node=(tag,cls='',text='')=>{const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;};
  let providers=[],completing=false,pendingVersion=null,busy=false;
  const ready=Account.api('/api/auth/social/providers').then(data=>{providers=data.providers;show();}).catch(()=>{providers=[];show();});
  function show(){
    if(completing)return;
    $('social-completion').hidden=true;$('auth-native-area').hidden=false;
    const enabled=new Set(providers.filter(p=>p.enabled).map(p=>p.id));
    for(const button of dialog.querySelectorAll('[data-social]'))button.hidden=!enabled.has(button.dataset.social);
    $('auth-social').hidden=!enabled.size;$('social-icon-row').hidden=!['vk','ok','mail'].some(p=>enabled.has(p));
  }
  function reset(){completing=false;show();}
  async function begin(provider,action='login'){
    if(busy)return;busy=true;
    const target=action==='link'?$('social-link-error'):$('auth-error');target.textContent='';
    const buttons=action==='link'?$('account-social-connections').querySelectorAll('button'):dialog.querySelectorAll('[data-social]');
    for(const button of buttons)button.disabled=true;
    try{
      const password=action==='link'?$('social-link-password').value:undefined;
      const data=await Account.api('/api/auth/social/'+provider+'/start',{method:'POST',body:JSON.stringify({
        action,return_to:location.hash||'#matches',password})});
      $('social-link-password').value='';
      // Full-page HTTPS redirects retain the browser's existing provider SSO
      // and let its supported app/universal-link flow handle mobile handoff.
      const url=new URL(data.authorization_url);
      if(url.protocol!=='https:'||!['oauth.yandex.ru','id.vk.ru'].includes(url.hostname))throw new Error('Не удалось открыть страницу входа. Повторите попытку.');
      location.assign(url.href);
    }catch(error){target.textContent=error.message;for(const button of buttons)button.disabled=false;busy=false;}
  }
  for(const button of dialog.querySelectorAll('[data-social]'))button.addEventListener('click',()=>begin(button.dataset.social));
  $('social-existing-login').addEventListener('click',()=>{reset();Account.show('login',null,true);});
  dialog.addEventListener('close',reset);
  $('auth-native-toggle').addEventListener('click',reset);
  $('social-complete-form').addEventListener('submit',async event=>{
    event.preventDefault();const button=$('social-complete-submit');button.disabled=true;$('auth-error').textContent='';
    try{
      await Account.api('/api/auth/social/complete',{method:'POST',body:JSON.stringify({terms_accepted:$('social-terms').checked,
        personal_data_consent:$('social-data').checked,age_confirmed:$('social-age').checked,legal_version:pendingVersion})});
      await Account.refresh();dialog.close();window.ProductAnalytics?.goal('registration_success');
      dispatchEvent(new HashChangeEvent('hashchange'));
    }catch(error){
      if(error.status===409){
        await Account.refresh();
        if(Account.user){dialog.close();dispatchEvent(new HashChangeEvent('hashchange'));return;}
        reset();Account.show('register');
      }
      $('auth-error').textContent=error.message;
    }finally{button.disabled=false;}
  });
  async function connections(){
    await ready;
    const box=$('account-social-connections');box.replaceChildren();
    if(!Account.user){$('account-social-card').hidden=true;return;}
    const owner=Account.user.id;
    try{
      const data=await Account.api('/api/auth/social/connections');if(Account.user?.id!==owner)return;
      const visible=data.connections.filter(p=>p.enabled||p.linked);$('account-social-card').hidden=!visible.length;
      $('social-link-password-row').hidden=!Account.user.local_login_available;
      for(const p of visible){
        const row=node('div','social-connection');const label=node('span','',p.label);row.append(label);
        if(p.linked)row.append(node('span','social-linked','Подключён'));
        else{const button=node('button','secondary-button','Подключить');button.type='button';button.setAttribute('aria-label','Подключить '+p.label);button.addEventListener('click',()=>begin(p.id,'link'));row.append(button);}
        box.append(row);
      }
    }catch{$('account-social-card').hidden=true;}
  }
  document.addEventListener('accountchange',()=>{
    $('social-link-password').value='';$('social-link-error').textContent='';
    if(!Account.user){$('account-social-connections').replaceChildren();$('account-social-card').hidden=true;}
  });
  Promise.all([ready,Account.ready]).then(async()=>{
    const status=new URLSearchParams(location.search).get('social');if(!status)return;
    history.replaceState(null,'',location.pathname+location.hash);
    if(status==='success'){
      if(location.hash==='#account')await Account.renderPage();
      dispatchEvent(new HashChangeEvent('hashchange'));return;
    }
    if(status==='complete'){
      try{
        const data=await Account.api('/api/auth/social/pending');
        Account.show('register');completing=true;pendingVersion=data.legal_version;
        $('auth-title').textContent='Остался один шаг';$('auth-description').textContent='Подтвердите условия, чтобы создать аккаунт.';
        $('social-completion-name').textContent=data.display_name+' · '+data.label;
        $('social-complete-form').reset();$('social-completion').hidden=false;$('auth-social').hidden=true;$('auth-native-area').hidden=true;
        $('social-terms').focus();
      }catch(error){Account.show('register');$('auth-error').textContent=error.message;}
      return;
    }
    const messages={cancelled:'Вход отменён. Вы можете выбрать другой способ или попробовать снова.',
      expired:'Время входа истекло. Нажмите на выбранный сервис ещё раз.',
      session_changed:'Сеанс изменился. Повторите вход из текущей вкладки.',
      identity_in_use:'Этот способ уже связан с другим аккаунтом. Войдите в него или обратитесь в поддержку.',
      account_disabled:'Доступ к аккаунту ограничен. Обратитесь в поддержку.'};
    const message=messages[status]||'Не удалось завершить вход. Повторите попытку или выберите другой способ.';
    if(Account.user&&location.hash==='#account'){$('account-message').textContent=message;}
    else{Account.show('login');$('auth-error').textContent=message;}
  });
  return {show,connections};
})();
