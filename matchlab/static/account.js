"use strict";
window.Account = (() => {
  const byId = id => document.getElementById(id);
  let user = null, csrf = null, afterLogin = null, mode = "register";
  async function api(url, options={}) {
    const response = await fetch(url, {credentials:"same-origin",...options,headers:{"Content-Type":"application/json",...(csrf ? {"X-CSRF-Token":csrf} : {}),...options.headers}});
    let body;
    try { body = await response.json(); } catch { throw new Error(`Сервис вернул HTTP ${response.status}. Попробуйте позже.`); }
    if (!response.ok) {
      const error = new Error(Array.isArray(body.detail) ? body.detail.map(i=>i.msg).join("; ") : body.detail || `Ошибка ${response.status}`);
      if(response.status===401 && (!url.startsWith("/api/auth/") && url!=="/api/admin/reauth" || url==="/api/auth/logout")){user=null;csrf=null;sync();}
      error.status = response.status; error.reauth = response.headers.get("X-Reauth-Required") === "1"; throw error;
    }
    return body;
  }
  function sync() {
    byId("account-button").textContent = user ? user.username : "Войти";
    byId("admin-link").hidden = user?.role !== "admin";
    document.dispatchEvent(new Event("accountchange"));
  }
  async function refresh() {
    try { const result = await api("/api/auth/me"); user = result.user; csrf = result.csrf; sync(); }
    catch { user = null; csrf = null; sync(); }
  }
  const ready = refresh();
  function show(nextMode="register", callback=null) {
    afterLogin = callback; mode = nextMode;
    byId("auth-title").textContent = mode === "register" ? "Ваш личный аналитик" : "С возвращением";
    byId("auth-description").textContent = mode === "register" ? "Создайте аккаунт, чтобы получать ИИ-разборы и сохранять их в личной истории." : "Войдите, чтобы продолжить разбор выбранного матча.";
    byId("auth-submit").textContent = mode === "register" ? "Создать аккаунт" : "Войти";
    byId("auth-switch").textContent = mode === "register" ? "Уже есть аккаунт? Войти" : "Нет аккаунта? Зарегистрироваться";
    byId("auth-password").autocomplete = mode === "register" ? "new-password" : "current-password";
    byId("auth-error").textContent = ""; byId("auth-password").value = "";
    if (!byId("auth-dialog").open) byId("auth-dialog").showModal();
    byId("auth-username").focus();
  }
  async function ensure(callback) { await ready; if (user) return true; show("register",callback); return false; }
  byId("account-button").addEventListener("click",()=>{ if (user) location.hash="#account"; else show("login"); });
  byId("auth-close").addEventListener("click",()=>{ afterLogin=null; byId("auth-dialog").close(); });
  byId("auth-dialog").addEventListener("cancel",()=>{ afterLogin=null; });
  byId("auth-switch").addEventListener("click",()=>show(mode === "register" ? "login" : "register",afterLogin));
  byId("auth-form").addEventListener("submit",async event=>{
    event.preventDefault(); const button=byId("auth-submit"); button.disabled=true; byId("auth-error").textContent="";
    try {
      const result=await api("/api/auth/"+(mode === "register" ? "register" : "login"),{method:"POST",body:JSON.stringify({username:byId("auth-username").value,password:byId("auth-password").value})});
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
    byId("account-name").textContent=user.username;
    try {
      const [usage,push]=await Promise.all([api("/api/account/usage"),api("/api/push/config")]);
      byId("account-usage").textContent=`Разборов сегодня: ${usage.today}${usage.daily_limit ? " из " + usage.daily_limit : ""}`;
      byId("account-bonus").textContent=`Бонусных разборов: ${usage.bonus_analyses}`;
      byId("account-bonus-note").textContent=usage.daily_limit ? "Бонусы используются после дневного лимита." : "Сейчас разборы доступны без дневного лимита. Бонусы сохраняются на аккаунте.";
      const supported="serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
      byId("push-enable").disabled=!push.enabled || !supported;
      byId("push-note").textContent=!push.enabled ? "Уведомления скоро появятся — сервис готовит подключение." : !window.isSecureContext ? "Уведомления доступны через защищённое подключение HTTPS." : !supported ? "Этот браузер пока не поддерживает уведомления. На iPhone добавьте сайт на главный экран и откройте его оттуда." : usage.push_devices ? `Подключённых устройств: ${usage.push_devices}. Разрешение на этом устройстве можно изменить в браузере.` : "Получайте новости сервиса и подборки матчей. Подписка добровольная, отключить можно в любой момент.";
      const registration=await navigator.serviceWorker?.getRegistration("/");const subscription=await registration?.pushManager.getSubscription();
      byId("push-disable").hidden=!subscription;
    }catch(error){byId("account-message").textContent=error.message;}
  }
  byId("account-login").addEventListener("click",()=>show("login",renderPage));
  byId("account-logout").addEventListener("click",async()=>{
    try {await api("/api/auth/logout",{method:"POST",body:"{}"});user=null;csrf=null;sync();location.hash="#matches";}
    catch(error){byId("account-message").textContent=error.message;}
  });
  byId("account-password-form").addEventListener("submit",async event=>{
    event.preventDefault();const form=event.currentTarget,button=form.querySelector("button");button.disabled=true;
    try {
      const result=await api("/api/auth/password",{method:"POST",body:JSON.stringify({password:byId("account-old-password").value,new_password:byId("account-new-password").value})});
      user=result.user;csrf=result.csrf;form.reset();sync();byId("account-message").textContent="Пароль изменён. Сеансы на других устройствах завершены.";
    }catch(error){byId("account-message").textContent=error.message;}finally{button.disabled=false;}
  });
  byId("promo-redeem-form").addEventListener("submit",async event=>{
    event.preventDefault();const form=event.currentTarget,button=form.querySelector("button");button.disabled=true;
    try{const result=await api("/api/account/promo",{method:"POST",body:JSON.stringify({code:byId("promo-code").value})});byId("account-message").textContent=`Промокод активирован. Начислено бонусных разборов: ${result.bonus}.`;form.reset();await refresh();await renderPage();}
    catch(error){byId("account-message").textContent=error.message;}finally{button.disabled=false;}
  });
  function bytes(value){const raw=atob(value.replace(/-/g,"+").replace(/_/g,"/")+"=".repeat((4-value.length%4)%4));return Uint8Array.from(raw,c=>c.charCodeAt(0));}
  byId("push-enable").addEventListener("click",async()=>{
    const button=byId("push-enable");button.disabled=true;
    try{
      const permissionRequest=Notification.requestPermission();
      const config=await api("/api/push/config");
      const registration=await navigator.serviceWorker.register("/sw.js",{scope:"/"});await navigator.serviceWorker.ready;
      const permission=await permissionRequest;if(permission!=="granted")throw new Error("Разрешение на уведомления не получено. Его можно изменить в настройках браузера.");
      const subscription=await registration.pushManager.getSubscription() || await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:bytes(config.public_key)});
      await api("/api/push/subscribe",{method:"POST",body:JSON.stringify(subscription.toJSON())});await renderPage();
    }catch(error){byId("push-note").textContent=error.message;}finally{button.disabled=false;}
  });
  byId("push-disable").addEventListener("click",async()=>{
    try{const registration=await navigator.serviceWorker.getRegistration("/");const subscription=await registration?.pushManager.getSubscription();if(subscription){await api("/api/push/unsubscribe",{method:"POST",body:JSON.stringify({endpoint:subscription.endpoint})});await subscription.unsubscribe();}await renderPage();}
    catch(error){byId("push-note").textContent=error.message;}
  });
  function track(name,entity=""){api("/api/telemetry",{method:"POST",body:JSON.stringify({name,entity})}).catch(()=>{});}
  return {get user(){return user},get csrf(){return csrf},ready,api,show,ensure,refresh,renderPage,track};
})();
