"""Read-only adapter to the public site's decoder (window.apiWlb)."""

import base64
import hashlib
import subprocess
from pathlib import Path

SNAPSHOT = """() => {
 const a=window.apiWlb;
 if (!a?.getEvents || !a?.getService) throw Error('Winline public SDK is unavailable');
 const lines=a.getService('LineService');
 const raw={sdkVersion:a.version,events:a.getEvents().toArray(),
   sports:a.getSports().serialize(),countries:a.getCountries().serialize(),
   championships:a.getChampionships().serialize(),
   lines:[...lines.query({isLive:0,query:{}}),...lines.query({isLive:1,query:{}})]};
 return JSON.stringify(raw,(k,v)=>k==='meta'||k==='$loki'?undefined:v);
}"""

INSTALL = """() => {
 const a=window.apiWlb;
 window.__winlineReader={pre:null,pending:null,pluses:new Map()};
 const s=window.__winlineReader;
 a.client.addEventListener(data=>{s.pre=data},a.API_PARAMETS.CLIENT_EVENTS.GET_RESULT_PREDATA);
 a.client.addEventListener(data=>{if(s.pending){s.pending.resolve(data);s.pending=null}},a.API_PARAMETS.CLIENT_EVENTS.GET_RESULT);
 a.client.addEventListener(data=>{
   if(!Array.isArray(data)||!data.length) return;
   const ids=new Set(data.map(x=>x.idEvent));
   if(ids.size!==1) return;
   const id=data[0].idEvent;
   const pending=s.pluses.get(id);
   if(pending){pending(data.length);s.pluses.delete(id)}
 },a.API_PARAMETS.CLIENT_EVENTS.GET_NEW_PLUSES);
 // The SDK emits [] for an empty market response, dropping the event ID.
 // Read only this packet's header so concurrent requests remain correlated.
 a.client.websocket.addEventListener('message',async message=>{
   if(typeof message.data==='string') return;
   try {
     let bytes=new Uint8Array(await new Response(message.data).arrayBuffer());
     if(bytes[0]===31 && bytes[1]===139){
       const stream=new Response(bytes).body.pipeThrough(new DecompressionStream('gzip'));
       bytes=new Uint8Array(await new Response(stream).arrayBuffer());
     }
     if(bytes.length<6) return;
     const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
     const step=view.getUint16(0,true),steps=a.API_PARAMETS.STEPS;
     if((step===steps.GET_EVENT_FILL && bytes.length===6) ||
        (step===steps.GET_EVENT_FILL_NEW && bytes.length===14)) {
       const id=view.getInt32(2,true),pending=s.pluses.get(id);
       if(pending){pending(0);s.pluses.delete(id)}
     }
   } catch { /* Other messages are decoded by the site's own client. */ }
 });

}"""

RESULT_REQUEST = """({sport,day,timeout}) => new Promise((resolve,reject)=>{
 const s=window.__winlineReader;
 if(s.pending) return reject(Error('Another result request is pending'));
 const timer=setTimeout(()=>{s.pending=null;reject(Error('Result request timed out'))},timeout);
 s.pending={resolve:data=>{clearTimeout(timer);resolve(JSON.parse(JSON.stringify(data)))}};
 window.apiWlb.getService('ResultService').setPreData(sport,day);
})"""

PLUS_BATCH = """async ({events,timeout,delay}) => {
 const s=window.__winlineReader;
 return await Promise.all(events.map((event,index)=>new Promise(resolve=>{
   setTimeout(()=>{
     const timer=setTimeout(()=>{
       s.pluses.delete(event);resolve({event,error:'Market response empty or timed out'});
     },timeout);
     s.pluses.set(event,count=>{
       clearTimeout(timer);resolve({event,count});
     });
     window.apiWlb.getService('EventService').getPluses(event);
   },index*delay);
 })));
}"""


def proxy_ca_args(ca_path):
    """Trust only this configured proxy CA key in Chromium's isolated process."""
    if not ca_path:
        return []
    path = Path(ca_path)
    if not path.is_file():
        raise ValueError(f"CA file not found: {path}")
    public = subprocess.check_output(
        ["openssl", "x509", "-in", str(path), "-pubkey", "-noout"],
        stderr=subprocess.PIPE,
    )
    der = subprocess.check_output(
        ["openssl", "pkey", "-pubin", "-outform", "der"],
        input=public,
        stderr=subprocess.PIPE,
    )
    pin = base64.b64encode(hashlib.sha256(der).digest()).decode()
    return ["--ignore-certificate-errors-spki-list=" + pin]
