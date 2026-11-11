"use strict";
// Native dialog provides focus containment, inert background and Escape support.
window.AnalysisDialog = (() => {
  const dialog=document.getElementById('ai-dialog');
  let returnFocus=null,backdropPressed=false;
  const outside=event=>{const box=dialog.getBoundingClientRect();return event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom;};
  function open({loading=false}={}) {
    document.getElementById('ai-context-loading').hidden=!loading;
    if(dialog.open)return;
    returnFocus=document.activeElement;
    dialog.showModal();document.documentElement.classList.add('ai-dialog-open');
    document.getElementById('ai-dialog-close').focus({preventScroll:true});
  }
  function close(){if(dialog.open)dialog.close();}
  dialog.addEventListener('close',()=>{
    document.documentElement.classList.remove('ai-dialog-open');
    const target=returnFocus?.isConnected&&!returnFocus.closest('[hidden]')&&!returnFocus.disabled?returnFocus:document.getElementById('event-ai-button');
    if(target&&!target.closest('[hidden]')&&!target.disabled)target.focus({preventScroll:true});
  });
  dialog.addEventListener('pointerdown',event=>{backdropPressed=event.target===dialog&&outside(event);});
  dialog.addEventListener('click',event=>{if(backdropPressed&&event.target===dialog&&outside(event))close();backdropPressed=false;});
  document.getElementById('ai-dialog-close').addEventListener('click',close);
  document.getElementById('ai-dialog-dismiss').addEventListener('click',close);
  return {open,close,ready:()=>{document.getElementById('ai-context-loading').hidden=true;},
    state:value=>{dialog.dataset.state=value;},get isOpen(){return dialog.open;}};
})();
