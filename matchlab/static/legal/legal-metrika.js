"use strict";
// Legal documents remain readable without making a cookie decision.
window.Privacy={analyticsAllowed:false};
fetch('/api/privacy/preferences',{credentials:'same-origin'}).then(response=>response.ok?response.json():Promise.reject()).then(data=>{
  if(data.analytics===true) {
    window.Privacy.analyticsAllowed=true;
    // Webvisor is intentionally limited to the public match list in the app.
    window.ProductAnalytics?.start({analytics:true,webvisor:false});
  }
}).catch(()=>{});
