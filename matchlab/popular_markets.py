"""Read the source's displayed popular cards, without clicking betting controls."""
from datetime import datetime, timezone
import re


POPULAR_SCRIPT=r"""() => {
 const root=document.querySelector('ww-fast-bets-block-dsk');
 if(!root)return {markets:[],state:'missing'};
 const text=e=>(e?.innerText||'').trim().replace(/\s+/g,' ');
 const markets=[],unsupported=[];
 for(const wrapper of root.querySelectorAll('.fast-bets__wrapper')) {
  const group=text(wrapper.querySelector('h5'));
  if(!/^Популярные на (матч|тайм)$/i.test(group))continue;
  for(const card of wrapper.querySelectorAll('.bet-line')) {
   const name=text(card.querySelector('.bet-line__market-name'));
   const period=text(card.querySelector('.bet-line__period'));
   const buttons=[...card.querySelectorAll('.odd-btn')].sort((a,b)=>Number(getComputedStyle(a).order)-Number(getComputedStyle(b).order));
   const line=text(card.querySelector('.coef-btn')).replace(/−/g,'-');
   let labels=[],parameters=[],parameter=null;
   if(/^Исход\s+1[XХ]2$/i.test(name)&&buttons.length===3)labels=['П1','X','П2'];
   else if(/^Фора$/i.test(name)&&buttons.length===2) {
    const m=line.match(/^([+-])\s*(\d+(?:[.,]\d+)?)\s*([+-])$/);
    if(m&&m[1]!==m[3]){labels=['П1','П2'];parameters=[m[1]+m[2].replace(',','.'),m[3]+m[2].replace(',','.')];}
    else if(/^0(?:[.,]0+)?$/.test(line)){labels=['П1','П2'];parameters=['0','0'];}
   } else if(/^Тотал$/i.test(name)&&buttons.length===2) {
    const m=line.match(/^([МБ])\s*(\d+(?:[.,]\d+)?)\s*([МБ])$/i);
    if(m&&m[1].toUpperCase()!==m[3].toUpperCase()) {
     parameter=m[2].replace(',','.');labels=[m[1],m[3]].map(x=>x.toUpperCase()==='М'?'Меньше':'Больше');
    }
   } else if(/^(?:Обе забьют|Ч[её]т\/Неч[её]т)$/i.test(name)&&buttons.length===2)labels=buttons.map(b=>text(b.querySelector('.odd-btn__label')));
   if(!period||labels.length!==buttons.length||labels.some(x=>!x)||new Set(labels).size!==labels.length){unsupported.push({name,period,group});continue;}
   markets.push({name,period,group,parameter,outcomes:buttons.map((b,i)=>{
    const spans=[...b.children].filter(c=>c.tagName==='SPAN'&&!c.classList.contains('odd-btn__label'));
    const raw=text(spans.at(-1)),number=/^\d+(?:[.,]\d+)?$/.test(raw)?Number(raw.replace(',','.')):null;
    const active=number!==null&&number>1&&!b.classList.contains('odd-btn--disabled')&&b.getAttribute('aria-disabled')!=='true';
    return {name:labels[i],parameter:parameters[i]??null,odds:active?number:null,state:active?'open':'unavailable'};
   })});
  }
 }
 return {markets,unsupported,complete:markets.length>0&&unsupported.length===0,state:markets.length?'observed':'empty'};
}"""


def attach_observation(snapshot,observation):
    snapshot['popular_markets']=observation.get('markets',[])
    snapshot['popular_observed_at']=observation.get('observed_at') or datetime.now(timezone.utc).isoformat()
    snapshot['popular_capture_state']=observation.get('state','missing')
    snapshot['popular_complete']=observation.get('complete') is True


async def collect_popular(page,snapshot):
    observation=await page.evaluate(POPULAR_SCRIPT)
    observation['observed_at']=datetime.now(timezone.utc).isoformat()
    attach_observation(snapshot,observation)


def companion(document,legacy):
    """Bind popular cards from the normal page capture to the exact stream fixture."""
    from .bookmakers import instant
    from .freshness import quote_ttl
    from .chances import norm
    if not legacy or not legacy.get('popular_markets'):return []
    if any(norm(document.get(k))!=norm(legacy.get(k)) for k in ('home','away','sport')):return []
    if document.get('source_event_id')!=legacy.get('source_event_id'):return []
    if legacy.get('popular_phase',legacy.get('phase'))!=document.get('phase'):return []
    a,b=document.get('kickoff_at'),legacy.get('kickoff_at')
    if not a or not b or abs((instant(a)-instant(b)).total_seconds())>120:return []
    at=legacy.get('popular_observed_at') or legacy['observed_at']
    stale=(datetime.now(timezone.utc)-instant(at)).total_seconds()>quote_ttl(document)
    return [{**m,'observed_at':at,'is_stale':stale,'outcomes_complete':True} for m in legacy['popular_markets']]
