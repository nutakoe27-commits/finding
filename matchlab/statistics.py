"""Audited public StatsHub snapshots linked to the bookmaker's own event link."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlsplit

from .bookmakers import instant, iso
from .winline_browser import statistics_id_from_url, event_id_from_url

FORMAT='matchlab.event-statistics.v1'
STATES={'ok','blocked','empty','unavailable'}
PROVIDERS={'statshub.yunai.tech','statshub.sportradar.com'}


def provider_match_id(value: str) -> str | None:
    parsed=urlsplit(value)
    if parsed.scheme!='https' or parsed.hostname not in PROVIDERS or parsed.username or parsed.password:
        return None
    match=re.fullmatch(r'/winline/(?:ru|en)/match/(\d{1,20})/?',parsed.path)
    return match[1] if match else None


def text(value,maximum,optional=False):
    if value is None and optional: return None
    if not isinstance(value,str) or not value.strip() or len(value)>maximum:
        raise ValueError('Некорректные данные статистики.')
    return value.strip()


def validate_statistics(value: dict, event: dict) -> dict:
    if not isinstance(value,dict) or value.get('format')!=FORMAT or value.get('state') not in STATES:
        raise ValueError('Нужен документ статистики события.')
    event_id=value.get('source_event_id')
    if event_id!=event['source_event_id'] or event_id_from_url(value.get('event_url',''))!=event_id:
        raise ValueError('Статистика не привязана к событию.')
    stats_id=statistics_id_from_url(value.get('statistics_url',''))
    expected=statistics_id_from_url(event.get('statistics_url') or '')
    if not expected or stats_id!=expected or event.get('statistics_link_status')!='found':
        raise ValueError('Нет подтверждённой ссылки события на эту статистику.')
    attempted=instant(value.get('attempted_at'))
    if attempted>datetime.now(timezone.utc)+timedelta(minutes=2):
        raise ValueError('Время статистики находится в будущем.')
    result={'format':FORMAT,'source_event_id':event_id,'event_url':event['url'],
            'statistics_url':'https://winline.ru/stats/match/'+stats_id,'statistics_id':stats_id,
            'state':value['state'],'attempted_at':iso(attempted),'observed_at':None,
            'provider_url':None,'provider':'Sportradar / StatsHub','header':None,'sections':[],
            'match_phase':'unknown','binding':'observed_event_link'}
    if value['state']!='ok': return result
    if provider_match_id(value.get('provider_url',''))!=stats_id:
        raise ValueError('Виджет статистики относится к другому событию.')
    observed=instant(value.get('observed_at'))
    if observed>attempted or attempted-observed>timedelta(minutes=5):
        raise ValueError('Некорректное время наблюдения статистики.')
    phase=value.get('match_phase','unknown')
    if phase not in ('prematch','live','finished','unknown'):
        raise ValueError('Неизвестная фаза статистики.')
    header=text(value.get('header'),7000)
    sections=value.get('sections')
    if not isinstance(sections,list) or not 1<=len(sections)<=20:
        raise ValueError('Нет содержательных разделов статистики.')
    clean=[]
    for section in sections:
        if not isinstance(section,dict): raise ValueError('Повреждён раздел статистики.')
        title=text(section.get('title'),300)
        content=text(section.get('text'),7000)
        if re.search(r'access denied|you don.t have permission|проверка безопасности',content,re.I):
            raise ValueError('Страница ограничения доступа не является статистикой.')
        clean.append({'title':title,'text':content})
    parsed=urlsplit(value['provider_url'])
    result.update(observed_at=iso(observed),provider_url=f'https://{parsed.hostname}{parsed.path}',header=header,
                  match_phase=phase,sections=clean)
    if len(header)+sum(len(section['text']) for section in clean)>70000:
        raise ValueError('Снимок статистики слишком большой.')
    return result


STATS_SCRIPT=r'''() => {
    const publicText = element => {
        let result=element ? element.innerText || '' : '';
        for (const form of element ? element.querySelectorAll('form') : []) {
            if (form.innerText) result=result.split(form.innerText).join('');
        }
        return result.trim();
    };
    const header=document.querySelector('[data-testid="match-header"]');
    const sections=[]; let remaining=62000;
    for (const heading of [...document.querySelectorAll('h3')].filter(node=>node.getClientRects().length)) {
        const title=publicText(heading);
        if (!/статистика сезона|таблица|сыгранные матчи|последние встречи|последние матчи|обзор матча|последние игры|последние бои|личные встречи|очные встречи|head.to.head|last matches|season|standings|overall|recent/i.test(title)) continue;
        let container=heading.parentElement;
        for (let parent=container && container.parentElement; parent && parent!==document.body; parent=parent.parentElement) {
            if (parent.querySelectorAll('h3').length!==1 || publicText(parent).length>7000) break;
            container=parent;
        }
        const body=publicText(container);
        if (remaining>100 && body.length>title.length+5) {
            const content=body.slice(0,Math.min(7000,remaining));
            sections.push({title:title.slice(0,300),text:content}); remaining-=content.length;
        }
    }
    return {title:document.title,header:publicText(header).slice(0,7000),sections:sections.slice(0,20),
            body:publicText(document.body).slice(0,1000)};
}'''


async def collect_statistics(page, job: dict, *, wait_seconds=25) -> dict:
    import asyncio
    import time
    attempted=lambda: datetime.now(timezone.utc).isoformat()
    base={'format':FORMAT,'source_event_id':job['source_event_id'],'event_url':job['url'],
          'statistics_url':job['statistics_url'],'state':'unavailable'}
    try:
        response=await page.goto(job['statistics_url'],wait_until='domcontentloaded',timeout=45000)
        if response and response.status in (401,403,429):
            return {**base,'state':'blocked','attempted_at':attempted()}
        deadline=time.monotonic()+wait_seconds
        previous=None; stable=0
        while time.monotonic()<deadline:
            frames=[frame for frame in page.frames if provider_match_id(frame.url)==statistics_id_from_url(job['statistics_url'])]
            for frame in frames:
                try: data=await asyncio.wait_for(frame.evaluate(STATS_SCRIPT),timeout=5)
                except Exception: continue
                if re.search(r'access denied|you don.t have permission|проверка безопасности',data['body'],re.I):
                    return {**base,'state':'blocked','attempted_at':attempted()}
                if data['header'] and data['sections']:
                    signature=[(section['title'],len(section['text'])) for section in data['sections']]
                    stable=stable+1 if previous==signature else 0
                    previous=signature
                    if stable<2:
                        continue
                    header=data['header']
                    phase='finished' if re.search(r'закончен|заверш[её]н|finished|ended',header,re.I) else 'live' if re.search(r'\blive\b|ид[её]т матч|в игре',header,re.I) else 'unknown'
                    now=attempted()
                    return {**base,'state':'ok','observed_at':now,'attempted_at':now,
                            'provider_url':frame.url,'header':header,'match_phase':phase,'sections':data['sections']}
            await asyncio.sleep(.5)
        return {**base,'state':'empty','attempted_at':attempted()}
    except Exception:
        return {**base,'state':'unavailable','attempted_at':attempted()}


def analysis_statistics(snapshot: dict | None) -> dict | None:
    if not snapshot or snapshot.get('state')!='ok': return None
    # Put recent results and H2H ahead of aggregate season data. Preserve exact
    # captions and periods: a provider's 80/100 form score is not a win chance.
    priority=('последние матчи','последние встречи','сыгранные матчи','статистика сезона','таблица','обзор матча')
    ordered=sorted(snapshot['sections'],key=lambda section:next((index for index,label in enumerate(priority) if section['title'].lower().startswith(label)),len(priority)))
    # UI summaries are built only in the browser. Keep every validated source
    # section intact for the model and the audited analysis snapshot.
    sections=[{'title':section['title'],'text':section['text'],'truncated':False} for section in ordered]
    age=max(0,int((datetime.now(timezone.utc)-instant(snapshot['observed_at'])).total_seconds()))
    return {'source':'Sportradar / StatsHub via Winline','statistics_id':snapshot['statistics_id'],
            'statistics_url':snapshot['statistics_url'],'observed_at':snapshot['observed_at'],'age_seconds':age,
            'is_stale':age>3600,'match_phase':snapshot['match_phase'],'header':snapshot['header'],
            'header_truncated':False,
            'sections':sections,'total_sections':len(snapshot['sections']),
            'truncated_sections':sum(section['truncated'] for section in sections),
            'scope':'visible source sections, may include this match and future fixtures'}
