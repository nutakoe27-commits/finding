"use strict";
self.addEventListener("push",event=>{
  let data;
  try{data=event.data.json();}catch{return;}
  const url=new URL(data.url||"/#matches",self.location.origin);
  if(url.origin!==self.location.origin)return;
  event.waitUntil(self.registration.showNotification(String(data.title||"prognozich ai").slice(0,80),{body:String(data.body||"").slice(0,240),icon:"/static/icon.svg",badge:"/static/icon.svg",tag:"campaign-"+String(data.campaign_id||"news"),data:{url:url.href,campaign_id:data.campaign_id}}));
});
self.addEventListener("notificationclick",event=>{
  event.notification.close();const url=new URL(event.notification.data.url,self.location.origin);
  if(url.origin!==self.location.origin)return;
  event.waitUntil(Promise.all([
    fetch("/api/telemetry",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:"push_click",entity:String(event.notification.data.campaign_id||"")})}).catch(()=>{}),
    clients.matchAll({type:"window",includeUncontrolled:true}).then(async windows=>{const existing=windows.find(w=>new URL(w.url).origin===url.origin);if(existing){await existing.navigate(url.href);return existing.focus();}return clients.openWindow(url.href);})
  ]));
});
