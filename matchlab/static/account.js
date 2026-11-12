"use strict";
window.Account = (() => {
  const byId = id => document.getElementById(id);
  let user = null, csrf = null, afterLogin = null, mode = "register", legalVersion = null, legalPrompt = null;
  async function api(url, options={}, legalRetried=false) {
    const response = await fetch(url, {credentials:"same-origin",...options,headers:{"Content-Type":"application/json",...(csrf ? {"X-CSRF-Token":csrf} : {}),...options.headers}});
    let body;
    try { body = await response.json(); } catch { throw new Error(`Сервис вернул HTTP ${response.status}. Попробуйте позже.`); }
    if (!response.ok) {
      const error = new Error(Array.isArray(body.detail) ? body.detail.map(i=>i.msg).join("; ") : body.detail || `Ошибка ${response.status}`);
      if(response.status===401 && (!url.startsWith("/api/auth/") && url!=="/api/admin/reauth" || url==="/api/auth/logout")){user=null;csrf=null;sync();}
      error.status = response.status; error.reauth = response.headers.get("X-Reauth-Required") === "1";
      // This header means the server rejected the action before starting it.
      if(response.status===403 && response.headers.get('X-Legal-Required') === '1' && !legalRetried){
        const accountId=user?.id;
        if(await showLegal(response.headers.get('X-Legal-Version')) && user?.id===accountId) return api(url,options,true);
      }
      throw error;
    }
    return body;
  }
  function sync() {
    if(legalPrompt && legalPrompt.userId!==user?.id) finishLegal(false);
    byId("account-button").textContent = user ? user.display_name||user.username : "Войти";
    byId("admin-link").hidden = user?.role !== "admin";
    document.dispatchEvent(new Event("accountchange"));
  }
  async function refresh() {
    try { const result = await api("/api/auth/me"); user = result.user; csrf = result.csrf; legalVersion=result.legal_version; sync(); }
    catch { user = null; csrf = null; sync(); }
  }
  const ready = refresh();
  function showLegal(version) {
    if(!legalPrompt){
      let resolve;const promise=new Promise(done=>{resolve=done;});
      legalPrompt={promise,resolve,version:version||legalVersion,userId:user?.id};
      byId('legal-form').reset();byId('legal-error').textContent='';
      if(!byId('legal-renewal').open) byId('legal-renewal').showModal();
    }
    return legalPrompt.promise;
  }
  function finishLegal(accepted){
    const pending=legalPrompt;legalPrompt=null;
    if(byId('legal-renewal').open) byId('legal-renewal').close();
    pending?.resolve(accepted);
  }
  function show(nextMode="register", callback=null, native=false) {
    afterLogin = callback; mode = nextMode;
    byId("auth-title").textContent = mode === "register" ? "Ваш личный аналитик" : "С возвращением";
    byId("auth-description").textContent = mode === "register" ? "Создайте аккаунт, чтобы получать ИИ-разборы и сохранять их в личной истории." : "Войдите, чтобы продолжить разбор выбранного матча.";
    byId("auth-submit").textContent = mode === "register" ? "Создать аккаунт" : "Войти";
    byId("auth-switch").textContent = mode === "register" ? "Уже есть аккаунт? Войти" : "Нет аккаунта? Зарегистрироваться";
    byId("auth-password").autocomplete = mode === "register" ? "new-password" : "current-password";
    byId("auth-legal").hidden=mode !== "register";
    for (const id of ["auth-terms","auth-personal-data","auth-age"]) { byId(id).required=mode === "register"; byId(id).checked=false; }
    byId("auth-error").textContent = ""; byId("auth-password").value = "";
    byId('auth-form').hidden=!native;byId('auth-switch').hidden=!native;
    byId('auth-native-toggle').textContent=native?'Скрыть форму':mode==='register'?'Зарегистрироваться по логину':'Войти по логину и паролю';
    byId('auth-native-toggle').setAttribute('aria-expanded',String(native));
    window.SocialLogin?.show();
    byId("auth-password").type="password";byId("auth-password-toggle").textContent="Показать";byId("auth-password-toggle").setAttribute("aria-pressed","false");
    if (!byId("auth-dialog").open) byId("auth-dialog").showModal();
    if(native)byId("auth-username").focus();else (byId('auth-dialog').querySelector('[data-social]:not([hidden])')||byId('auth-native-toggle')).focus();
  }
  async function ensure(callback) { await ready; if (user) return true; show("register",callback); return false; }
  byId("account-button").addEventListener("click",()=>{ if (user) location.hash="#account"; else show("login"); });
  byId("auth-close").addEventListener("click",()=>{ afterLogin=null; byId("auth-dialog").close(); });
  byId("auth-dialog").addEventListener("cancel",()=>{ afterLogin=null; });
  byId("auth-switch").addEventListener("click",()=>show(mode === "register" ? "login" : "register",afterLogin,true));
  byId('auth-native-toggle').addEventListener('click',()=>show(mode,afterLogin,byId('auth-form').hidden));
  byId('legal-close').addEventListener('click',()=>finishLegal(false));
  byId('legal-renewal').addEventListener('cancel',()=>finishLegal(false));
  byId('legal-renewal').addEventListener('close',()=>finishLegal(false));
  byId('legal-form').addEventListener('submit',async event=>{
    event.preventDefault();const button=byId('legal-submit');button.disabled=true;byId('legal-error').textContent='';
    try {
      const version=legalPrompt?.version;
      if(!version) throw new Error('Не удалось загрузить редакцию документов. Обновите страницу и повторите действие.');
      await api('/api/auth/confirm-documents',{method:'POST',body:JSON.stringify({terms_accepted:byId('legal-terms').checked,personal_data_consent:byId('legal-data').checked,age_confirmed:byId('legal-age').checked,legal_version:version})});
      legalVersion=version;finishLegal(true);
    }
    catch(error){byId('legal-error').textContent=error.message;}finally{button.disabled=false;}
  });
  byId("auth-form").addEventListener("submit",async event=>{
    event.preventDefault(); const button=byId("auth-submit"); button.disabled=true; byId("auth-error").textContent="";
    try {
      const submittedMode=mode;
      const credentials={username:byId("auth-username").value,password:byId("auth-password").value};
      if(submittedMode === "register") {
        await ready;
        if(!legalVersion) throw new Error('Не удалось загрузить редакцию документов. Обновите страницу и повторите регистрацию.');
        Object.assign(credentials,{terms_accepted:byId("auth-terms").checked,personal_data_consent:byId("auth-personal-data").checked,age_confirmed:byId("auth-age").checked,legal_version:legalVersion});
      }
      const result=await api("/api/auth/"+(submittedMode === "register" ? "register" : "login"),{method:"POST",body:JSON.stringify(credentials)});
      window.ProductAnalytics?.goal(submittedMode === 'register' ? 'registration_success' : 'login_success');
      user=result.user;csrf=result.csrf;byId("auth-password").value="";sync();
      const callback=afterLogin;afterLogin=null;byId("auth-dialog").close();
      if(callback)callback(); else if(location.hash==="#account") renderPage(); else if(location.hash==="#admin") window.Admin?.open();
    } catch(error) { byId("auth-error").textContent=error.message; }
    finally { button.disabled=false; }
  });
  async function renderPage() {
    await ready;
    byId("account-guest").hidden=!!user;byId("account-content").hidden=!user;
    if(!user)return;
    byId("account-name").textContent=user.display_name||user.username;
    window.Profile?.identity(user);
    byId('account-password-card').hidden=!user.local_login_available;
    await window.SocialLogin?.connections();
    try {
      const [usage,push]=await Promise.all([api("/api/account/usage"),api("/api/push/config")]);
      byId("account-usage").textContent=`Разборов сегодня: ${usage.today}${usage.daily_limit ? " из " + usage.daily_limit : ""}`;
      byId("account-bonus").textContent=`Бонусных разборов: ${usage.bonus_analyses}`;
      byId("account-bonus-note").textContent=usage.daily_limit ? "Бонусы используются после дневного лимита." : "Сейчас разборы доступны без дневного лимита. Бонусы сохраняются на аккаунте.";
      if(window.Credits?.enabled){byId('account-usage').textContent=`Запросов к ИИ сегодня: ${usage.today}${usage.daily_limit?' из '+usage.daily_limit:''}`;byId('account-bonus-note').textContent='Бонусы входят в баланс разборов и используются при разблокировке ответа.';await Credits.refresh();}
      const supported="serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
      byId("push-enable").disabled=!push.enabled || !supported;
      byId("push-note").textContent=!push.enabled ? "Уведомления скоро появятся — сервис готовит подключение." : !window.isSecureContext ? "Уведомления доступны через защищённое подключение HTTPS." : !supported ? "Этот браузер пока не поддерживает уведомления. На iPhone добавьте сайт на главный экран и откройте его оттуда." : usage.push_devices ? `Подключённых устройств: ${usage.push_devices}. Разрешение на этом устройстве можно изменить в браузере.` : "Получайте новости сервиса и подборки матчей. Подписка добровольная, отключить можно в любой момент.";
      const registration=await navigator.serviceWorker?.getRegistration("/");const subscription=await registration?.pushManager.getSubscription();
      byId("push-disable").hidden=false;
    }catch(error){byId("account-message").textContent=error.message;}
  }
  byId("account-login").addEventListener("click",()=>show("login",renderPage));
  byId("account-logout").addEventListener("click",async()=>{
    try {await api("/api/auth/logout",{method:"POST",body:"{}"});user=null;csrf=null;sync();location.hash="#matches";}
    catch(error){byId("account-message").textContent=error.message;}
  });
  byId("account-password-form").addEventListener("submit",async event=>{
    event.preventDefault();const form=event.currentTarget,button=form.querySelector("button");button.disabled=true;byId("profile-password-feedback").textContent="";
    try {
      const result=await api("/api/auth/password",{method:"POST",body:JSON.stringify({password:byId("account-old-password").value,new_password:byId("account-new-password").value})});
      user=result.user;csrf=result.csrf;form.reset();sync();byId("profile-password-feedback").textContent="Пароль изменён. На других устройствах потребуется войти заново.";byId("profile-password-feedback").classList.remove("is-error");
    }catch(error){byId("profile-password-feedback").textContent=error.message;byId("profile-password-feedback").classList.add("is-error");}finally{button.disabled=false;}
  });
  byId("promo-redeem-form").addEventListener("submit",async event=>{
    event.preventDefault();const form=event.currentTarget,button=form.querySelector("button");button.disabled=true;
    try{const result=await api("/api/account/promo",{method:"POST",body:JSON.stringify({code:byId("promo-code").value})});window.ProductAnalytics?.goal('promo_redeemed');byId("profile-promo-feedback").textContent=`Промокод применён. +${result.bonus} разборов на баланс.`;byId("profile-promo-feedback").classList.remove("is-error");form.reset();await refresh();await renderPage();}
    catch(error){byId("profile-promo-feedback").textContent=error.message;byId("profile-promo-feedback").classList.add("is-error");}finally{button.disabled=false;}
  });
  function bytes(value){const raw=atob(value.replace(/-/g,"+").replace(/_/g,"/")+"=".repeat((4-value.length%4)%4));return Uint8Array.from(raw,c=>c.charCodeAt(0));}
  byId("push-enable").addEventListener("click",async()=>{
    if(!byId('push-ad-consent').checked){byId('push-note').textContent='Чтобы подписаться, подтвердите отдельное согласие на уведомления.';return;}
    const button=byId("push-enable");button.disabled=true;
    try{
      const permissionRequest=Notification.requestPermission();
      const config=await api("/api/push/config");
      const registration=await navigator.serviceWorker.register("/sw.js",{scope:"/"});await navigator.serviceWorker.ready;
      const permission=await permissionRequest;if(permission!=="granted")throw new Error("Разрешение на уведомления не получено. Его можно изменить в настройках браузера.");
      const subscription=await registration.pushManager.getSubscription() || await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:bytes(config.public_key)});
      await api("/api/push/subscribe",{method:"POST",body:JSON.stringify({...subscription.toJSON(),advertising_consent:true,consent_version:'2026-11-09.v2'})});window.ProductAnalytics?.goal('push_subscribed');await renderPage();
    }catch(error){byId("push-note").textContent=error.message;}finally{button.disabled=false;}
  });
  byId("push-disable").addEventListener("click",async()=>{
    try{await api('/api/push/unsubscribe-all',{method:'POST',body:'{}'});const registration=await navigator.serviceWorker?.getRegistration("/");const subscription=await registration?.pushManager.getSubscription();if(subscription)await subscription.unsubscribe();byId('push-ad-consent').checked=false;await renderPage();byId('push-note').textContent='Согласие отозвано, отправка на все устройства отключена.';}
    catch(error){byId("push-note").textContent=error.message;}
  });
  function track(name,entity=""){if(window.Privacy?.analyticsAllowed)api("/api/telemetry",{method:"POST",body:JSON.stringify({name,entity})}).catch(()=>{});}
  return {get user(){return user},get csrf(){return csrf},ready,api,show,ensure,refresh,renderPage,track};
})();
