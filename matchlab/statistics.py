"""Audited public StatsHub snapshots linked to the bookmaker's own event link."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import re
from urllib.parse import urljoin, urlsplit

from .bookmakers import instant, iso
from .winline_browser import statistics_id_from_url, event_id_from_url

FORMAT='matchlab.event-statistics.v1'
STATES={'ok','blocked','empty','unavailable'}
PROVIDERS={'statshub.yunai.tech','statshub.sportradar.com'}
VIEWS={'overview', 'report', 'statistics'}
LOG=logging.getLogger(__name__)


def validate_structured_feed(value, statistics_id, attempted):
    from .statistics_feed import extract_passive_feed, MAX_TOTAL_BYTES
    if not isinstance(value,dict) or not isinstance(value.get('match'),dict) or not isinstance(value.get('participants'),dict):
        raise ValueError('Некорректные структурированные данные статистики.')
    if type(value.get('truncated')) is not bool:
        raise ValueError('Неизвестна полнота структурированных данных.')
    feed_at=instant(value.get('observed_at'))
    if feed_at>attempted or attempted-feed_at>timedelta(minutes=5):
        raise ValueError('Некорректное время структурированных данных.')
    if len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())>MAX_TOTAL_BYTES:
        raise ValueError('Структурированные данные слишком велики.')
    documents=[{'match':value['match']}]
    for side in ('home','away'):
        lists=value['participants'].get(side)
        if not isinstance(lists,list) or len(lists)>4:
            raise ValueError('Некорректные списки матчей участников.')
        for item in lists:
            if not isinstance(item,dict) or not isinstance(item.get('team'),dict) or not isinstance(item.get('matches'),list):
                raise ValueError('Некорректный список матчей участника.')
            documents.append({'team':item['team'],'matches':item['matches']})
    rebuilt=extract_passive_feed(documents,statistics_id)
    if not rebuilt or not all(rebuilt['participants'][side] for side in ('home','away')):
        raise ValueError('Не удалось проверить участников структурированных данных.')
    rebuilt['truncated']=rebuilt['truncated'] or value['truncated']
    rebuilt['observed_at']=iso(feed_at)
    return rebuilt


def provider_match_id(value: str) -> str | None:
    parsed=urlsplit(value)
    if parsed.scheme!='https' or parsed.hostname not in PROVIDERS or parsed.username or parsed.password:
        return None
    match=re.fullmatch(r'/winline/(?:ru|en)/match/(\d{1,20})(?:/(?:report|statistics))?/?',parsed.path)
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
    capture_kind=value.get('capture_kind','detailed')
    if capture_kind not in ('fast','detailed'): raise ValueError('Неизвестный вид снимка статистики.')
    result['capture_kind']=capture_kind
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
        section_result={'title':title,'text':content}
        if 'truncated' in section:
            if type(section['truncated']) is not bool: raise ValueError('Некорректная отметка обрезки.')
            section_result['truncated']=section['truncated']
        if 'view' in section:
            if not isinstance(section['view'],str) or section['view'] not in VIEWS: raise ValueError('Неизвестная вкладка статистики.')
            section_result['view']=section['view']
        if 'filters' in section:
            filters=section['filters']
            if (not isinstance(filters,list) or not 1<=len(filters)<=3 or
                any(not isinstance(item,str) or not item.strip() or len(item)>100 for item in filters)):
                raise ValueError('Некорректные фильтры раздела.')
            section_result['filters']=[item.strip() for item in filters]
        if 'observed_at' in section:
            section_at=instant(section['observed_at'])
            if section_at>attempted or attempted-section_at>timedelta(minutes=5):
                raise ValueError('Некорректное время раздела статистики.')
            section_result['observed_at']=iso(section_at)
        clean.append(section_result)
    parsed=urlsplit(value['provider_url'])
    result.update(observed_at=iso(observed),provider_url=f'https://{parsed.hostname}{parsed.path}',header=header,
                  match_phase=phase,sections=clean)
    if 'observed_views' in value:
        views=value['observed_views']
        if not isinstance(views,list) or not views or any(not isinstance(view,str) or view not in VIEWS for view in views) or len(views)!=len(set(views)):
            raise ValueError('Некорректный список вкладок статистики.')
        if any(section.get('view') not in views for section in clean if 'view' in section):
            raise ValueError('Раздел относится к непрочитанной вкладке.')
        result['observed_views']=views
    if 'sections_truncated' in value:
        if type(value['sections_truncated']) is not bool: raise ValueError('Некорректная полнота статистики.')
        result['sections_truncated']=value['sections_truncated']
    if 'season_filters_complete' in value:
        if type(value['season_filters_complete']) is not bool: raise ValueError('Некорректное состояние фильтров.')
        result['season_filters_complete']=value['season_filters_complete']
    if 'structured_feed' in value:
        result['structured_feed']=validate_structured_feed(value['structured_feed'],stats_id,attempted)
        if capture_kind=='fast':
            from .statistics_feed import feed_has_other_matches
            if not feed_has_other_matches(result['structured_feed']):
                raise ValueError('В предварительном снимке нет других событий обоих участников.')
    elif capture_kind=='fast':
        raise ValueError('В предварительном снимке нет подтверждённых структурированных данных.')
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
    const sections=[]; let remaining=62000; let limited=false;
    for (const heading of [...document.querySelectorAll('h3')].filter(node=>node.getClientRects().length)) {
        const title=publicText(heading);
        if (!/статистика матча|статистика игры|статистика встречи|события матча|ход матча|составы команд|match statistics|match stats|match events|lineups|лучшие игроки|top players|статистика сезона|сезонная форма|форма команды|таблица|сыгранные матчи|последние встречи|последние матчи|обзор матча|последние игры|последние бои|личные встречи|очные встречи|head.to.head|last matches|season|standings|overall|recent/i.test(title)) continue;
        let container=heading.parentElement;
        for (let parent=container && container.parentElement; parent && parent!==document.body; parent=parent.parentElement) {
            if (parent.querySelectorAll('h3').length!==1 || publicText(parent).length>7000) break;
            container=parent;
        }
        const body=publicText(container);
        if (remaining>100 && body.length>title.length+5) {
            const content=body.slice(0,Math.min(7000,remaining));
            const truncated=content.length<body.length;
            limited ||= truncated;
            sections.push({title:title.slice(0,300),text:content,truncated}); remaining-=content.length;
        } else if (body.length>title.length+5) limited=true;
    }
    return {title:document.title,header:publicText(header).slice(0,7000),sections:sections.slice(0,20),
            truncated:limited || sections.length>20,body:publicText(document.body).slice(0,1000)};
}'''

# Scope all clicks to a single season statistics card and require the source's
# own aria-pressed state. The labels were observed on the Winline widget.
SEASON_FILTER_SCRIPT=r'''({mode, scope, action}) => {
    const visible=e=>e.getClientRects().length>0;
    const headings=[...document.querySelectorAll('h3')].filter(e=>visible(e) && (e.innerText || '').trim().startsWith('Статистика сезона'));
    if (headings.length!==1) return {ok:false};
    let card=headings[0].parentElement;
    for (let p=card && card.parentElement; p && p!==document.body; p=p.parentElement) {
        if (p.querySelectorAll('h3').length!==1 || (p.innerText || '').length>7000) break;
        card=p;
    }
    const groups=[['Итоги','За матч','Серия'],['Общее','Хозяева / Гости']];
    const labels=[mode,scope];
    const buttons=[...card.querySelectorAll('button')].filter(visible);
    const selected=[];
    for (let i=0;i<groups.length;i++) {
        const entries=groups[i].map(label=>buttons.filter(b=>(b.innerText || '').trim()===label));
        if (entries.some(group=>group.length!==1)) return {ok:false};
        const group=entries.map(items=>items[0]);
        if (group.some(b=>!['true','false'].includes(b.getAttribute('aria-pressed'))) ||
            group.filter(b=>b.getAttribute('aria-pressed')==='true').length!==1) return {ok:false};
        const target=group[groups[i].indexOf(labels[i])];
        if (!target || target.disabled) return {ok:false};
        selected.push(target.getAttribute('aria-pressed')==='true');
        if (action==='click' && !selected[i]) {target.click(); return {ok:true,clicked:true};}
    }
    return {ok:true,selected:selected.every(Boolean),clicked:false};
}'''


async def _collect_statistics_dom(page, job: dict, *, wait_seconds=25) -> dict:
    import asyncio
    import time
    attempted=lambda: datetime.now(timezone.utc).isoformat()
    base={'format':FORMAT,'source_event_id':job['source_event_id'],'event_url':job['url'],
          'statistics_url':job['statistics_url'],'state':'unavailable'}
    diagnostic={'provider_frames':0,'matching_frames':0,'samples':0,'header_samples':0,
                'section_samples':0,'valid_samples':0,'changes':0,'evaluation_errors':0}
    try:
        response=await page.goto(job['statistics_url'],wait_until='domcontentloaded',timeout=45000)
        if response and response.status in (401,403,429):
            return {**base,'state':'blocked','attempted_at':attempted()}
        stats_id=statistics_id_from_url(job['statistics_url'])
        async def read_stable(frame, seconds, require_sections, *, allow_grace=False):
            deadline=time.monotonic()+seconds; previous=None; stable=0; grace_used=False
            while time.monotonic()<deadline:
                try: data=await asyncio.wait_for(frame.evaluate(STATS_SCRIPT),timeout=5)
                except Exception:
                    diagnostic['evaluation_errors']+=1
                    await asyncio.sleep(.5); continue
                if re.search(r'access denied|you don.t have permission|проверка безопасности',data['body'],re.I):
                    return 'blocked', None
                diagnostic['samples']+=1
                diagnostic['header_samples']+=bool(data['header'])
                diagnostic['section_samples']+=bool(data['sections'])
                if data['header'] and (data['sections'] or not require_sections):
                    diagnostic['valid_samples']+=1
                    # If content appears just before the deadline, give it a
                    # bounded chance to be confirmed instead of dropping it.
                    if allow_grace and not grace_used:
                        deadline=max(deadline,time.monotonic()+10)
                        grace_used=True
                    # The surrounding widget may contain a ticking clock or
                    # unrelated dynamic text. Only compare what is saved.
                    signature=(data['header'],[(s['title'],s['text']) for s in data['sections']])
                    if previous is not None and previous!=signature: diagnostic['changes']+=1
                    stable=stable+1 if previous==signature else 0; previous=signature
                    if stable>=2: return 'ok', data
                await asyncio.sleep(.5)
            return 'empty', None

        deadline=time.monotonic()+wait_seconds
        while time.monotonic()<deadline:
            diagnostic['provider_frames']=max(diagnostic['provider_frames'],sum(urlsplit(frame.url).hostname in PROVIDERS for frame in page.frames))
            frames=[frame for frame in page.frames if provider_match_id(frame.url)==stats_id and
                    re.fullmatch(r'/winline/(?:ru|en)/match/\d{1,20}/?',urlsplit(frame.url).path)]
            diagnostic['matching_frames']=max(diagnostic['matching_frames'],len(frames))
            for frame in frames:
                status,data=await read_stable(frame,max(1,deadline-time.monotonic()),True,allow_grace=True)
                if status=='blocked': return {**base,'state':'blocked','attempted_at':attempted()}
                if status!='ok': continue
                header=data['header']; first_at=attempted(); provider_url=frame.url
                phase='finished' if re.search(r'закончен|заверш[её]н|finished|ended',header,re.I) else 'live' if re.search(r'\blive\b|ид[её]т матч|в игре',header,re.I) else 'unknown'
                sections=[{**section,'view':'overview','observed_at':first_at} for section in data['sections']]
                views=['overview']; limited=data.get('truncated',False)
                season_filters_complete=None
                season=next((s for s in data['sections'] if s['title'].startswith('Статистика сезона')),None)
                if season:
                    variants=[('Итоги','Общее'),('Итоги','Хозяева / Гости'),('За матч','Хозяева / Гости'),
                              ('За матч','Общее'),('Серия','Общее'),('Серия','Хозяева / Гости')]
                    recorded=0; previous_text=season['text']
                    try:
                        supported=await frame.evaluate(SEASON_FILTER_SCRIPT,{'mode':variants[0][0],'scope':variants[0][1],'action':'check'})
                        if supported.get('ok'):
                            season_filters_complete=False
                            for mode,scope in variants:
                                changed=False; valid=True
                                for _ in range(2):
                                    state=await frame.evaluate(SEASON_FILTER_SCRIPT,{'mode':mode,'scope':scope,'action':'click'})
                                    if not state.get('ok'): valid=False; break
                                    if state.get('selected'): break
                                    changed=changed or bool(state.get('clicked'))
                                    await asyncio.sleep(.35)
                                else:
                                    state=await frame.evaluate(SEASON_FILTER_SCRIPT,{'mode':mode,'scope':scope,'action':'check'})
                                    valid=bool(state.get('ok') and state.get('selected'))
                                if not valid: break
                                # A changed caption alone does not prove the underlying
                                # figures refreshed. Wait for changed section text.
                                tab_status,tab=await read_stable(frame,5,True)
                                if tab_status=='blocked': return {**base,'state':'blocked','attempted_at':attempted()}
                                if tab_status!='ok': break
                                selected=await frame.evaluate(SEASON_FILTER_SCRIPT,{'mode':mode,'scope':scope,'action':'check'})
                                match=next((s for s in tab['sections'] if s['title']==season['title']),None)
                                if not selected.get('selected') or not match: break
                                current_text=match['text']
                                if changed and current_text==previous_text:
                                    # Allow asynchronous data to arrive, but stop if the
                                    # change cannot be distinguished from a stale view.
                                    wait_until=time.monotonic()+3
                                    while current_text==previous_text and time.monotonic()<wait_until:
                                        await asyncio.sleep(.4)
                                        check=await frame.evaluate(SEASON_FILTER_SCRIPT,{'mode':mode,'scope':scope,'action':'check'})
                                        if not check.get('selected'): break
                                        status_retry,retry=await read_stable(frame,2,True)
                                        if status_retry=='blocked': return {**base,'state':'blocked','attempted_at':attempted()}
                                        if status_retry!='ok': break
                                        match=next((s for s in retry['sections'] if s['title']==season['title']),None)
                                        if not match: break
                                        current_text=match['text']
                                    if current_text==previous_text: break
                                previous_text=current_text
                                sections.append({**match,'view':'overview','observed_at':attempted(),'filters':[mode,scope]})
                                recorded+=1
                            season_filters_complete=recorded==len(variants)
                    except Exception:
                        season_filters_complete=False
                # Only follow the actual same-provider, same-match links present in
                # the widget. Never construct IDs or follow arbitrary URLs.
                for view in ('report','statistics'):
                    try:
                        link=frame.locator(f'a[href$="/match/{stats_id}/{view}"]')
                        if await link.count()!=1: continue
                        target=urljoin(frame.url,await link.first.get_attribute('href'))
                        parsed=urlsplit(target); initial=urlsplit(provider_url)
                        if (parsed.scheme!='https' or parsed.hostname!=initial.hostname or
                            provider_match_id(target)!=stats_id or not parsed.path.endswith('/'+view)):
                            continue
                        tab_response=await frame.goto(target,wait_until='domcontentloaded',timeout=12000)
                        if tab_response and tab_response.status in (401,403,429):
                            return {**base,'state':'blocked','attempted_at':attempted()}
                        if tab_response and tab_response.status>=400: continue
                        if provider_match_id(frame.url)!=stats_id or not urlsplit(frame.url).path.rstrip('/').endswith('/'+view): continue
                        # The pre-match report can legitimately be empty; the
                        # statistics tab must expose a section before it counts.
                        tab_status,tab=await read_stable(frame,8,view=='statistics')
                        if tab_status=='blocked': return {**base,'state':'blocked','attempted_at':attempted()}
                        if tab_status!='ok': continue
                        views.append(view); tab_at=attempted()
                        limited=limited or tab.get('truncated',False)
                        sections.extend({**section,'view':view,'observed_at':tab_at} for section in tab['sections'])
                    except Exception:
                        continue
                bounded=[]; remaining=62000
                for section in sections:
                    if len(bounded)>=20 or remaining<=100:
                        limited=True; break
                    content=section['text'][:remaining]
                    if not content.strip(): continue
                    truncated=section.get('truncated',False) or len(content)<len(section['text'])
                    limited=limited or truncated
                    bounded.append({**section,'text':content,'truncated':truncated}); remaining-=len(content)
                now=attempted()
                if season_filters_complete:
                    season_filters_complete=all(any(s.get('filters')==list(pair) for s in bounded) for pair in variants)
                result={**base,'state':'ok','observed_at':first_at,'attempted_at':now,
                        'provider_url':provider_url,'header':header,'match_phase':phase,
                        'observed_views':views,'sections_truncated':limited,'sections':bounded}
                if season_filters_complete is not None: result['season_filters_complete']=season_filters_complete
                return result
            await asyncio.sleep(.5)
        LOG.warning('Statistics empty for event %s: provider_frames=%d matching_frames=%d samples=%d header_samples=%d section_samples=%d valid_samples=%d changes=%d evaluation_errors=%d',
                    job['source_event_id'],diagnostic['provider_frames'],diagnostic['matching_frames'],
                    diagnostic['samples'],diagnostic['header_samples'],diagnostic['section_samples'],
                    diagnostic['valid_samples'],diagnostic['changes'],diagnostic['evaluation_errors'])
        return {**base,'state':'empty','attempted_at':attempted()}
    except Exception:
        return {**base,'state':'unavailable','attempted_at':attempted()}


async def collect_statistics(page, job: dict, *, wait_seconds=25, on_fast=None, enable_feed=False) -> dict:
    """Observe the widget's own JSON and retain the existing detailed DOM capture."""
    if not enable_feed:
        return await _collect_statistics_dom(page,job,wait_seconds=wait_seconds)
    import asyncio
    import time
    from .statistics_feed import PassiveFeedObserver
    stats_id=statistics_id_from_url(job['statistics_url'])
    observer=PassiveFeedObserver(page,stats_id)
    observer.start()
    fast_snapshot=None

    async def deliver_fast():
        nonlocal fast_snapshot
        await observer.ready.wait()
        if observer.blocked or not observer.feed: return
        try:
            deadline=time.monotonic()+10; header=''; frame=None
            while time.monotonic()<deadline and not observer.blocked:
                frames=[candidate for candidate in page.frames if provider_match_id(candidate.url)==stats_id and
                        re.fullmatch(r'/winline/(?:ru|en)/match/\d{1,20}/?',urlsplit(candidate.url).path)]
                if len(frames)==1:
                    frame=frames[0]
                    try: data=await asyncio.wait_for(frame.evaluate(STATS_SCRIPT),timeout=3)
                    except Exception: data={}
                    if re.search(r'access denied|you don.t have permission|проверка безопасности',data.get('body',''),re.I): return
                    header=data.get('header','')
                    if header: break
                await asyncio.sleep(.5)
            if not header or observer.blocked or not frame or not re.fullmatch(r'/winline/(?:ru|en)/match/\d{1,20}/?',urlsplit(frame.url).path): return
            now=datetime.now(timezone.utc).isoformat()
            phase='finished' if re.search(r'закончен|заверш[её]н|finished|ended',header,re.I) else 'live' if re.search(r'\blive\b|ид[её]т матч|в игре',header,re.I) else 'unknown'
            feed=observer.feed
            sizes={side:', '.join(str(len(item['matches'])) for item in feed['participants'][side]) for side in ('home','away')}
            has_unverified=any(item.get('unverified_participant_count',0) for side in ('home','away') for item in feed['participants'][side])
            fast_snapshot={'format':FORMAT,'source_event_id':job['source_event_id'],'event_url':job['url'],
                'statistics_url':job['statistics_url'],'state':'ok','observed_at':now,'attempted_at':now,
                'provider_url':frame.url,'header':header,'match_phase':phase,'observed_views':['overview'],'capture_kind':'fast',
                'sections_truncated':True,'sections':[{'title':'Матчи участников',
                    'text':f"Размеры списков: хозяева — {sizes['home']}; гости — {sizes['away']}. Списки могут включать текущие и будущие события." + (" Часть встреч требует дополнительной проверки участника." if has_unverified else ''),
                    'view':'overview','observed_at':now}], 'structured_feed':feed}
            if on_fast:
                try: await on_fast(fast_snapshot)
                except Exception as error: LOG.warning('Fast statistics delivery failed (%s)',type(error).__name__)
        except Exception:
            return

    task=asyncio.create_task(deliver_fast())
    dom_task=asyncio.create_task(_collect_statistics_dom(page,job,wait_seconds=wait_seconds))
    blocked_task=asyncio.create_task(observer.blocked_event.wait())
    try:
        await asyncio.wait((dom_task,blocked_task),return_when=asyncio.FIRST_COMPLETED)
        if observer.blocked:
            dom_task.cancel()
            await asyncio.gather(dom_task,return_exceptions=True)
            snapshot={'state':'blocked'}
        else:
            snapshot=await dom_task
        if snapshot['state']=='blocked' or observer.blocked:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            try: await page.goto('about:blank',wait_until='commit',timeout=5000)
            except Exception: pass
            return {'format':FORMAT,'source_event_id':job['source_event_id'],'event_url':job['url'],
                    'statistics_url':job['statistics_url'],'state':'blocked','attempted_at':datetime.now(timezone.utc).isoformat()}
        if not task.done() and observer.ready.is_set():
            try: await asyncio.wait_for(task,timeout=10)
            except Exception: pass
        if observer.blocked:
            try: await page.goto('about:blank',wait_until='commit',timeout=5000)
            except Exception: pass
            return {'format':FORMAT,'source_event_id':job['source_event_id'],'event_url':job['url'],
                    'statistics_url':job['statistics_url'],'state':'blocked','attempted_at':datetime.now(timezone.utc).isoformat()}
        if snapshot['state']=='ok' and observer.feed:
            snapshot['structured_feed']=observer.feed
            if snapshot['attempted_at']<observer.feed['observed_at']:
                snapshot['attempted_at']=observer.feed['observed_at']
            if fast_snapshot and snapshot['attempted_at']<fast_snapshot['attempted_at']:
                snapshot['attempted_at']=fast_snapshot['attempted_at']
        elif snapshot['state'] in ('empty','unavailable') and fast_snapshot:
            snapshot=fast_snapshot
        return snapshot
    finally:
        if not dom_task.done(): dom_task.cancel()
        if not blocked_task.done(): blocked_task.cancel()
        if not task.done(): task.cancel()
        await asyncio.gather(task,dom_task,blocked_task,return_exceptions=True)
        await observer.close()


def analysis_statistics(snapshot: dict | None) -> dict | None:
    if not snapshot or snapshot.get('state')!='ok': return None
    # Put recent results and H2H ahead of aggregate season data. Preserve exact
    # captions and periods: a provider's 80/100 form score is not a win chance.
    priority=('статистика матча','статистика игры','статистика встречи','match statistics','match stats','последние матчи','последние встречи','сыгранные матчи','статистика сезона','сезонная форма','форма команды','таблица','лучшие игроки','top players','обзор матча')
    ordered=sorted(snapshot['sections'],key=lambda section:next((index for index,label in enumerate(priority) if section['title'].lower().startswith(label)),len(priority)))
    # UI summaries are built only in the browser. Keep every validated source
    # section intact for the model and the audited analysis snapshot.
    sections=[{'title':section['title'],'text':section['text'],'truncated':section.get('truncated',False),**{key:section[key] for key in ('view','observed_at','filters') if key in section}} for section in ordered]
    age=max(0,int((datetime.now(timezone.utc)-instant(snapshot['observed_at'])).total_seconds()))
    result={'source':'Sportradar / StatsHub via Winline','statistics_id':snapshot['statistics_id'],
            'statistics_url':snapshot['statistics_url'],'observed_at':snapshot['observed_at'],'age_seconds':age,
            'is_stale':age>3600,'match_phase':snapshot['match_phase'],'header':snapshot['header'],
            'capture_kind':snapshot.get('capture_kind','detailed'),
            'header_truncated':False,
            'sections':sections,'total_sections':len(snapshot['sections']),
            'truncated_sections':sum(section['truncated'] for section in sections),
            'observed_views':snapshot.get('observed_views'),
            'sections_truncated':snapshot.get('sections_truncated',False),
            'season_filters_complete':snapshot.get('season_filters_complete'),
            'scope':'visible source sections and current filters only; may include this match and future fixtures'}
    if snapshot.get('structured_feed'):
        # Keep every source record, but separate unverified participant
        # identities explicitly rather than asking the model to count indices.
        import copy
        feed=copy.deepcopy(snapshot['structured_feed'])
        for side in ('home','away'):
            for item in feed['participants'][side]:
                matches=item.pop('matches')
                verified=set(item.pop('verified_participant_indices',[]))
                item['verified_matches']=[row for index,row in enumerate(matches) if index in verified]
                item['unverified_matches']=[row for index,row in enumerate(matches) if index not in verified]
        feed['scope']='verified_matches are participant-linked; unverified_matches are retained but cannot establish participant history; either may contain current or future fixtures'
        result['structured_feed']=feed
        result['scope']='sections are visible source content; structured_feed is from observed widget responses and may include current or future fixtures'
    return result
