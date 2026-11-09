"""Read-only adapter to the public site's decoder (window.apiWlb)."""

import base64
import hashlib
import subprocess
from pathlib import Path

SNAPSHOT = r"""options => {
 const a=window.apiWlb;
 if(!a?.getEvents || !a?.getService) throw Error('Winline public SDK unavailable');
 if(a.client.websocket.readyState!==1) throw Error('Winline stream disconnected');
 const svc=a.getService('LineService'),formatter=a.getService('StringFormatService');
 const events=a.getEvents().toArray(),byId=new Map(events.map(e=>[e.id,e]));
 const requested=new Set(options?.event_ids||[]),full=options?.all_lines!==false;
 const cache=window.__displayCache ||= new Map();
 const render=line=>{
   const event=byId.get(line.idEvent),sport=event&&a.getSports().byId(event.idSport);
   const tip=line.tipLine||{},parts=String(line.koef||'').split('/');
   const key=[line.idEvent,line.id,line.isLive].join(':');
   const signature=JSON.stringify([line.idTipMarket,line.koef,tip.freeTextR,tip.R,event?.members,sport?.strings]);
   let display=cache.get(key);
   if(!display || display.signature!==signature){
     const format=str=>{
       str=String(str||'').replace(/\[([a-z])\]/g,(_,letter)=>parts[letter.charCodeAt(0)-97]??'');
       str=formatter.formatGameSegments({sourceStr:str,sport,withAt:true});
       str=formatter.formatString({sourceStr:str,sport,event});
       return formatter.formatClearScore({sourceStr:str,members:event?.members||[],tipMarket:tip});
     };
     try {
       const name=format(tip.freeTextR),labels=(tip.R||[]).map(format);
       display={signature,name,labels,valid:!!name&&!/@|\[[a-z]\]/i.test(name)&&
         labels.slice(0,(line.V||[]).length).every(x=>!!x&&!/@|\[[a-z]\]/i.test(x))};
     }catch{display={signature,name:'',labels:[],valid:false}}
     cache.set(key,display);
   }
   return {...line,_display:{name:display.name,labels:display.labels,valid:display.valid}};
 };
 const raw=[...svc.query({isLive:0,query:{}}),...svc.query({isLive:1,query:{}})];
 const mainByEvent=new Map(events.map(e=>[e.id,svc.getEventMainLine(e.id,e.isLive,e.idSport)]));
 const key=line=>[line.idEvent,line.id,line.isLive].join(':');
 const picked=full?raw:[...new Map([...raw.filter(l=>requested.has(l.idEvent)),...[...mainByEvent.values()].filter(Boolean)].map(l=>[key(l),l])).values()];
 const all=picked.map(render);
 const mapped=new Map(all.map(l=>[[l.idEvent,l.id,l.isLive].join(':'),l]));
 const enriched=events.map(e=>{const main=mainByEvent.get(e.id);
   return {...e,_main_line:main?mapped.get([e.id,main.id,e.isLive].join(':'))||render(main):null}});
 return JSON.stringify({sdkVersion:a.version,events:enriched,sports:a.getSports().serialize(),
   countries:a.getCountries().serialize(),championships:a.getChampionships().serialize(),lines:all},
   (key,value)=>key==='meta'||key==='$loki'?undefined:value);
}"""

REFRESH_WATCHES = """ids=>{
 const a=window.apiWlb,last=window.__watchRefresh ||= new Map();
 for(const id of ids.slice(0,4)){
   const e=a.getEvents().byId(id);
   if(!e || Date.now()-(last.get(id)||0)<20000)continue;
   last.set(id,Date.now());
   if(e.isLive)a.getService('LiveService').getPlus(id);
   else a.getService('EventService').getPluses(id);
 }
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
