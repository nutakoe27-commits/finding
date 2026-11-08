"""Public Winline marketbook extraction, separate from Scrapy scheduling."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
from urllib.parse import urlsplit

from .bookmakers import DATE_RE, EPL, FORMAT as QUOTE_FORMAT, instant, iso, kickoff, lines, make_snapshot
from .markets import FORMAT, identity, label, validate_event_markets
from .winline_browser import event_id_from_url, open_event_link, public_event_id, event_statistics_link, statistics_id_from_url
from .sports import sport_from_url, sport_label

PARSER_VERSION = "winline-marketbook-dom-v3"

# Bounded public DOM context for ambiguous outcomes. No HTML, URLs, inputs,
# form content or browser state are collected.
MARKET_CONTEXT_SCRIPT = r"""({names,labels}) => {
    const root=document.querySelector('ww-feature-marketbook-dsk');
    if (!root) return [];
    const text=el=>{
        let value=(el && el.innerText || '');
        for (const form of el ? el.querySelectorAll('form') : []) if (form.innerText) value=value.split(form.innerText).join('');
        return value.trim().slice(0,200);
    };
    const describe=el=>({tag:el.tagName.toLowerCase(),class:(el.getAttribute('class') || '').slice(0,160),text:text(el)});
    return [...root.querySelectorAll('button.market-header')].filter(header=>names.includes(text(header))).slice(0,2).map(header=>{
        let container=header.parentElement;
        for (let parent=container; parent && root.contains(parent); parent=parent.parentElement) {
            if (parent.querySelectorAll('button.market-header').length!==1) break;
            if (parent.querySelector('button.market__row-btn')) container=parent;
            if (parent===root) break;
        }
        const buttons=[...container.querySelectorAll('button.market__row-btn')].filter(el=>!el.closest('form'));
        const relevant=buttons.filter(button=>labels.includes(text(button).split('\n').slice(0,-1).join(' ').trim()));
        return {market:text(header),container:describe(container),
            children:[...container.children].filter(el=>!el.closest('form')).slice(0,5).map(describe),
            outcomes:(relevant.length ? relevant : buttons).slice(0,6).map(button=>{
                const ancestors=[];
                for (let parent=button.parentElement; parent && parent!==container && ancestors.length<2; parent=parent.parentElement) {
                    ancestors.push({...describe(parent),children:[...parent.children].filter(el=>!el.closest('form')).slice(0,5).map(describe)});
                }
                return {button:describe(button),ancestors};
            })};
    });
}"""

MARKETS_SCRIPT = r"""() => {
    const root = document.querySelector('ww-feature-marketbook-dsk');
    if (!root) return {ready: false};
    const text = el => (el && el.innerText || '').trim();
    const visible = el => !!el.getClientRects().length && !el.closest('form');
    const headers = [...root.querySelectorAll('button.market-header')];
    const all = [...root.querySelectorAll('.marketbook__filter .ww-tabs__item')].find(el => text(el) === 'Все');
    const allSelected = !!all && all.classList.contains('ww-tabs__item--active');
    const attribute = (node, key) => (node.getAttribute(key) || '').trim() || null;
    const top = document.querySelector('.marketbook-header--prematch');
    const home = top && top.querySelector('.prematch__competitor-left');
    const away = top && top.querySelector('.prematch__competitor-right');
    const names = el => el && (el.querySelector('h1,h2') || el);
    const titleText = el => text(names(el)).split('\n')[0].trim();
    const dateText = text(top && top.querySelector('.prematch-info'));
    const groups = [...root.querySelectorAll('h2,h3')].filter(el => !el.closest('.market-header'));
    const isPeriod = value => /^(?:осн\.?\s*время|основное\s*время|матч|бой|гонка|заезд|встреча|с\s*(?:уч[её]том\s*)?(?:ОТ|овертайм|буллит|тайбрейк).*|\d+(?:-?й)?\s*(?:тайм|период|четверть|сет|партия|гейм|раунд|иннинг)|(?:сет|гейм|раунд|период|четверть|иннинг)\s*\d+|\d+[-–]\d+\s*мин\.?)$/i.test(value);
    const inTitle = value => (value.match(/осн\.?\s*время|\d+(?:-?й)?\s*(?:тайм|период|четверть|сет|партия|гейм|раунд|иннинг)|с\s*(?:уч[её]том\s*)?ОТ(?:\s*и\s*Б)?/i) || [null])[0];
    const titleRoot = document.querySelector('.ww-markets__stickybar,.marketbook-header--prematch,.marketbook-header--live');
    const eventTitle = !home && !away && titleRoot && titleRoot.querySelector('h1,h2');
    const eventRoot = document.querySelector('.ww-markets');
    const statisticRoots = eventRoot ? [eventRoot] : [...document.querySelectorAll('.marketbook-header--prematch,.marketbook-header--live')];
    const statisticsLinks = [...new Set(statisticRoots.flatMap(root=>[...root.querySelectorAll('a[href*="/stats/match/"]')].map(link=>link.href)))].slice(0,20);
    const variants = new Map();
    const unsupported = [];
    let hidden = 0;
    const seenButtons = new Set();
    for (const [scope,header] of headers.entries()) {
        let container = null;
        for (let parent = header.parentElement; parent && root.contains(parent); parent = parent.parentElement) {
            if (parent.querySelectorAll('button.market-header').length !== 1) break;
            if (parent.querySelector('button.market__row-btn')) container = parent;
            if (parent === root) break;
        }
        if (!container) { hidden++; continue; }
        const buttons = [...container.querySelectorAll('button.market__row-btn')];
        if (!buttons.some(visible)) { hidden++; continue; }
        let group = null;
        for (const heading of groups) {
            if (heading.compareDocumentPosition(header) & Node.DOCUMENT_POSITION_FOLLOWING) group = text(heading) || null;
        }
        const name = text(header);
        const sourceMarket = attribute(container,'data-market-id') || attribute(header,'data-market-id');
        let contextLabels = null;
        for (const button of buttons) {
            if (seenButtons.has(button) || !visible(button)) continue;
            seenButtons.add(button);
            const tokens = text(button).split('\n').map(value => value.trim()).filter(Boolean);
            const last = tokens[tokens.length - 1] || '';
            const numeric = /^\d+(?:[.,]\d+)?$/.test(last);
            const suspended = button.disabled || button.getAttribute('aria-disabled') === 'true' || /(?:^|[\s_-])(?:disabled|suspended|locked|blocked)(?:$|[\s_-])/.test(button.className);
            let price = numeric ? Number(last.replace(',', '.')) : null;
            let labelTokens = numeric ? tokens.slice(0, -1) : tokens.filter(value => !/^[-—–]+$/.test(value));
            let outcomeName = labelTokens.join(' ').trim();
            if (!outcomeName) outcomeName = button.getAttribute('aria-label') || button.getAttribute('title') || '';
            if (!outcomeName || (!numeric && !suspended && tokens.length)) {
                unsupported.push({market: name, text: text(button).slice(0, 200)});
                continue;
            }
            let context = '';
            for (let parent = button.parentElement; parent && parent !== container; parent = parent.parentElement) {
                const siblings = [...parent.querySelectorAll('button.market__row-btn')];
                if (siblings.length < 2) continue;
                let prefix = text(parent);
                for (const sibling of siblings) prefix = prefix.split(text(sibling)).join('');
                for (const heading of parent.querySelectorAll('button.market-header')) prefix = prefix.split(text(heading)).join('');
                const parts = prefix.split('\n').map(value => value.trim()).filter(Boolean);
                if (parts.length === 1 && parts[0].length < 150) { context = parts[0]; break; }
            }
            if (!context) {
                if (contextLabels === null) {
                    contextLabels = [];
                    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
                    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
                        const value = (node.textContent || '').trim();
                        if (value && value.length < 150 && node.parentElement && visible(node.parentElement) &&
                            !node.parentElement.closest('button.market__row-btn,button.market-header,form,script,style')) contextLabels.push([node, value]);
                    }
                }
                for (const [node, value] of contextLabels) {
                    if (node.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING) context = value;
                }
            }
            const period = isPeriod(context) ? context : inTitle(name);
            const parameter = context && !isPeriod(context) ? context : null;
            const key = JSON.stringify([name, group, period, parameter, sourceMarket, scope]);
            if (!variants.has(key)) variants.set(key, {name, group, period, parameter, source_market_id: sourceMarket, _scope: scope, outcomes: []});
            const sourceOutcome = attribute(button,'data-outcome-id') || attribute(button,'data-id');
            variants.get(key).outcomes.push({name: outcomeName, parameter: null, source_outcome_id: sourceOutcome,
                odds: price, state: suspended ? 'suspended' : numeric ? 'open' : 'unavailable'});
        }
    }
    const allButtons = [...root.querySelectorAll('button.market__row-btn')].filter(visible);
    return {ready: true, home: titleText(home), away: titleText(away), title: text(eventTitle), date_text: dateText,
        phase: top ? 'prematch' : document.querySelector('.marketbook-header--live') ? 'live' : 'unknown', markets: [...variants.values()], statistics_links: statisticsLinks,
        market_sections: headers.length, expected_outcomes: allButtons.length,
        unparsed_outcomes: allButtons.length - [...variants.values()].reduce((total, item) => total + item.outcomes.length, 0),
        hidden_groups: hidden, all_tab_selected: allSelected, unsupported: unsupported.slice(0, 20),
        structure_keys: [...variants.values()].flatMap(market => market.outcomes.map(outcome =>
            JSON.stringify([[market.name,market.group,market.period,market.parameter,market.source_market_id], outcome.name, outcome.parameter, outcome.source_outcome_id]))),
        structure: JSON.stringify([...variants.values()].map(market =>
            [[market.name,market.group,market.period,market.parameter,market.source_market_id], market.outcomes.map(outcome => [outcome.name, outcome.parameter, outcome.source_outcome_id])]))};
}"""


def distinguish_market_sections(snapshot: dict) -> None:
    """Separate distinct visible selection sets under repeated source headings.

    The fingerprint identifies the observed set, not the bookmaker's market
    ID or its rules. Identical/subset sets remain ambiguous and fail closed.
    """
    groups={}
    for raw in snapshot.get('markets',[]):
        base={key:value for key,value in raw.items() if key not in ('outcomes','_scope')}
        key=identity(base)
        groups.setdefault(key,[]).append((base,raw.get('_scope'),raw['outcomes']))
    merged={}
    for group in groups.values():
        scopes={scope for _,scope,_ in group}
        signatures=[{identity({key:value for key,value in outcome.items() if key not in ('odds','state')}) for outcome in outcomes} for _,_,outcomes in group]
        separate=(not snapshot.get('unparsed_outcomes') and not snapshot.get('hidden_groups') and
                  None not in scopes and len(scopes)==len(group) and len(group)>1 and
                  all(len(signature)>=2 and len(signature)==len(outcomes) for signature,(_,_,outcomes) in zip(signatures,group)) and
                  all(not (left<=right or right<=left) for index,left in enumerate(signatures) for right in signatures[index+1:]))
        for (base,_,outcomes),signature in zip(group,signatures):
            market=dict(base)
            if separate: market['variant_id']=identity({'outcomes':sorted(signature)})
            market_id=identity(market)
            merged.setdefault(market_id,{**market,'outcomes':[]})['outcomes'].extend(outcomes)
    snapshot['markets']=list(merged.values())


def exclude_invalid_market_labels(snapshot: dict) -> None:
    """Retain valid markets and report malformed source labels without guessing."""
    valid=[]
    for market in snapshot.get('markets',[]):
        fields=[('market.name',market.get('name'),300,False),('market.group',market.get('group'),200,True),
                ('market.period',market.get('period'),100,True),('market.parameter',market.get('parameter'),100,True),
                ('market.source_market_id',market.get('source_market_id'),100,True)]
        for outcome in market.get('outcomes',[]):
            fields.extend([('outcome.name',outcome.get('name'),300,False),('outcome.parameter',outcome.get('parameter'),100,True),
                           ('outcome.source_outcome_id',outcome.get('source_outcome_id'),100,True)])
        issue=None
        for name,value,maximum,optional in fields:
            try: label(value,maximum,optional=optional)
            except ValueError:
                issue={'reason':'invalid_source_label','market':str(market.get('name') or '')[:120],
                       'field':name,'length':len(value) if isinstance(value,str) else None,'maximum':maximum}
                break
        if issue:
            snapshot['unparsed_outcomes']=snapshot.get('unparsed_outcomes',0)+len(market.get('outcomes',[]))
            snapshot['unsupported']=([issue]+snapshot.get('unsupported',[]))[:20]
        else: valid.append(market)
    snapshot['markets']=valid


def validate_event_labels_for_diagnostics(document: dict) -> None:
    kind=document['event_kind']
    fields=[('sport_label',document.get('sport_label'),100,False),
            ('title',document.get('title'),300,kind=='match'),
            ('home',document.get('home'),200,kind=='outright'),
            ('away',document.get('away'),200,kind=='outright'),
            ('competition',document.get('competition'),200,False),
            ('parser_version',document.get('parser_version'),80,False)]
    for name,value,maximum,optional in fields:
        try: label(value,maximum,optional=optional)
        except ValueError as exc:
            size=len(value) if isinstance(value,str) else 'не текст'
            raise ValueError(f'Некорректная подпись события: {name}, длина {size}, предел {maximum}') from exc


async def ensure_event_page(page, expected_url: str, metadata: dict | None = None):
    wanted = event_id_from_url(expected_url)
    if not wanted:
        raise ValueError("Некорректный адрес события Winline.")
    if public_event_id(page.url) != wanted:
        # A short /event link can initialize on the homepage first. Follow its
        # real current anchor when present, exactly as the verified diagnostic.
        try:
            await page.wait_for_selector('a[href="/stavki"],a[href="https://winline.ru/stavki"]', timeout=15000)
            if not await page.locator(f'a[href$="/{wanted}"],a[href$="/{wanted}/"]').count():
                raise ValueError('Current event link is outside the homepage slice')
            await open_event_link(page, event_id=int(wanted))
        except Exception:
            # Ordinary SPA navigation for a link discovered in the public
            # inventory but currently outside the homepage's visible slice.
            if urlsplit(page.url).hostname not in ('winline.ru', 'www.winline.ru'):
                raise
            beginning = (await page.locator('body').inner_text())[:500]
            if re.search(r'verify you are human|access denied|подтвердите.{0,40}человек|проверка безопасности', beginning, re.I):
                raise ValueError('Source requires an access check')
            if not await page.locator('a[href="/stavki"],a[href="https://winline.ru/stavki"]').count():
                raise ValueError('The public application did not initialize')
            await page.evaluate("url => { history.pushState({}, '', url); dispatchEvent(new PopStateEvent('popstate')); }", expected_url)
            await page.wait_for_url(lambda value: public_event_id(str(value)) == wanted, timeout=15000)
    if not sport_from_url(page.url):
        # The SPA may briefly retain /stavki/event/ID while it resolves the
        # actual sport route. Only accept an observed canonical URL for this
        # same event; never infer the sport from its teams or event number.
        try:
            await page.wait_for_url(lambda value: public_event_id(str(value)) == wanted and bool(sport_from_url(str(value))), timeout=15000)
        except Exception as exc:
            raise ValueError('Не дождались подтверждённой страницы вида спорта для события') from exc
    await page.wait_for_selector('ww-feature-marketbook-dsk button.market-header', timeout=30000)


async def read_marketbook(page, *, maximum_passes=200) -> dict:
    root = page.locator('ww-feature-marketbook-dsk')
    await root.wait_for(state='visible', timeout=30000)
    all_tab = root.locator('.marketbook__filter .ww-tabs__item').filter(has_text=re.compile(r'^\s*Все\s*$')).first
    await all_tab.wait_for(state='visible', timeout=15000)
    if 'ww-tabs__item--active' not in (await all_tab.get_attribute('class') or ''):
        await all_tab.click()
    stable, previous = 0, None
    snapshot, accumulated = {}, {}
    initial_keys, initial_variant_ids, virtualized = None, None, False
    reached_bottom = False
    max_unparsed, max_hidden, max_duplicates = 0, 0, 0
    capture_started_at = datetime.now(timezone.utc).isoformat()
    expanded_headers = set()
    for _ in range(maximum_passes):
        # Only group headers can be expanded. Outcome/price buttons are read.
        headers = root.locator('button.market-header')
        collapsed = await headers.evaluate_all("""headers => headers.flatMap((header, index) => {
                if (header.disabled) return [];
                for (let parent = header.parentElement; parent; parent = parent.parentElement) {
                    if (parent.querySelectorAll('button.market-header').length !== 1) break;
                    const buttons = [...parent.querySelectorAll('button.market__row-btn')];
                    if (buttons.length) return buttons.some(button => button.getClientRects().length) ? [] : [{index, name: header.innerText}];
                }
                return [{index, name: header.innerText}];
            })""")
        for item in collapsed[:1000]:
            key = (item['index'], item['name'])
            if key not in expanded_headers:
                await headers.nth(item['index']).click()
                expanded_headers.add(key)
        snapshot = await page.evaluate(MARKETS_SCRIPT)
        distinguish_market_sections(snapshot)
        exclude_invalid_market_labels(snapshot)
        part_outcomes, duplicates, ambiguous = {}, 0, set()
        conflicts=[]
        for market in snapshot.get('markets', []):
            market_id = identity({key: value for key, value in market.items() if key != 'outcomes'})
            for outcome in market['outcomes']:
                outcome_id = identity({key: value for key, value in outcome.items() if key not in ('odds', 'state')})
                key = (market_id, outcome_id)
                if key in part_outcomes:
                    if part_outcomes[key] != outcome:
                        ambiguous.add(market_id)
                        if len(conflicts)<5:
                            conflicts.append({'market':market['name'],'period':market.get('period'),
                                              'parameter':market.get('parameter'),'reason':'ambiguous_outcome_context',
                                              'outcomes':[part_outcomes[key],outcome]})
                    else: duplicates += 1
                part_outcomes[key] = outcome
        if ambiguous:
            rejected=[market for market in snapshot['markets'] if identity({key:value for key,value in market.items() if key!='outcomes'}) in ambiguous]
            snapshot['markets']=[market for market in snapshot['markets'] if market not in rejected]
            snapshot['unparsed_outcomes']=snapshot.get('unparsed_outcomes',0)+sum(len(market['outcomes']) for market in rejected)
            snapshot['unsupported']=(snapshot.get('unsupported',[])+conflicts)[:20]
            for market_id in ambiguous: accumulated.pop(market_id,None)
        for market in snapshot.get('markets', []):
            market_id = identity({key: value for key, value in market.items() if key != 'outcomes'})
            previous_market = accumulated.setdefault(market_id, {**market, 'outcomes': {}})
            for outcome in market['outcomes']:
                outcome_id = identity({key:value for key,value in outcome.items() if key not in ('odds','state')})
                previous_market['outcomes'][outcome_id] = outcome
        max_duplicates = max(max_duplicates, duplicates)
        signature = snapshot.get('structure')
        keys = set(snapshot.get('structure_keys', []))
        variant_ids=frozenset(market['variant_id'] for market in snapshot.get('markets',[]) if market.get('variant_id'))
        max_unparsed = max(max_unparsed, snapshot.get('unparsed_outcomes', 0))
        max_hidden = max(max_hidden, snapshot.get('hidden_groups', 0))
        if initial_keys is None:
            initial_keys = keys
        elif not virtualized and (not initial_keys.issubset(keys) or (initial_variant_ids is not None and variant_ids != initial_variant_ids)):
            # A structural change after jumping to the end can indicate a
            # virtual list. Restart at its top and scan viewport by viewport.
            virtualized = True
            await root.evaluate("""root => {
                for (let parent = root; parent && parent !== document.body; parent = parent.parentElement) {
                    if (parent.scrollHeight > parent.clientHeight && /auto|scroll/.test(getComputedStyle(parent).overflowY)) { parent.scrollTop = 0; return; }
                }
                window.scrollTo(0, 0);
            }""")
            reached_bottom = False
            await asyncio.sleep(0.3)
        if virtualized:
            # A virtualized viewport may expose only part of an outcome set;
            # discard these derived variants and mark the snapshot partial.
            uncertain=[key for key,market in accumulated.items() if market.get('variant_id')]
            if uncertain:
                max_unparsed=max(max_unparsed,sum(len(accumulated[key]['outcomes']) for key in uncertain))
                for key in uncertain: del accumulated[key]
        if not virtualized:
            initial_keys = keys
            if initial_variant_ids is None and variant_ids: initial_variant_ids=variant_ids
        stable = stable + 1 if signature == previous else 0
        previous = signature
        reached_bottom = await root.evaluate("""(root, virtualized) => {
            const headers = root.querySelectorAll('button.market-header');
            if (!virtualized && headers.length) headers[headers.length - 1].scrollIntoView({block: 'end'});
            for (let parent = root; parent && parent !== document.body; parent = parent.parentElement) {
                if (parent.scrollHeight > parent.clientHeight && /auto|scroll/.test(getComputedStyle(parent).overflowY)) {
                    if (virtualized) parent.scrollTop += Math.max(300, parent.clientHeight * 0.8);
                    else parent.scrollTop = parent.scrollHeight;
                    return parent.scrollTop + parent.clientHeight >= parent.scrollHeight - 5;
                }
            }
            if (virtualized) window.scrollBy(0, Math.max(300, window.innerHeight * 0.8));
            else window.scrollTo(0, document.documentElement.scrollHeight);
            return window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 5;
        }""", virtualized)
        if stable >= 2 and reached_bottom and snapshot.get('hidden_groups') == 0:
            if virtualized:
                snapshot['markets'] = [{**market, 'outcomes': list(market['outcomes'].values())} for market in accumulated.values()]
                snapshot['unparsed_outcomes'] = max_unparsed + max_duplicates
                snapshot['hidden_groups'] = max_hidden
                snapshot['expected_outcomes'] = sum(len(market['outcomes']) for market in accumulated.values()) + max_unparsed + max_duplicates
            snapshot['stable_passes'] = stable
            snapshot['scroll_exhausted'] = True
            snapshot['capture_started_at'] = capture_started_at
            return snapshot
        if stable >= 3 and reached_bottom and snapshot.get('hidden_groups', 0) > 0:
            break
        await asyncio.sleep(0.75)
    snapshot.update(stable_passes=stable, scroll_exhausted=False, capture_started_at=capture_started_at)
    return snapshot


async def discover_statistics_link(page, snapshot: dict) -> list[str]:
    """Resolve only the current event's explicitly labelled statistics control."""
    # Never resolve a global/menu statistics link outside an open event.
    if not event_id_from_url(page.url): return []
    known=snapshot.get('statistics_links',[])
    url,status=event_statistics_link(known)
    if status!='missing': return [url] if url else known
    controls=page.locator('.ww-markets a,.ww-markets button,.ww-markets [role="button"],.ww-markets__stickybar a,.ww-markets__stickybar button,.marketbook-header--prematch a,.marketbook-header--prematch button,.marketbook-header--live a,.marketbook-header--live button,.special__btns-wrapper > *')
    matches=await controls.evaluate_all(r'''nodes=>nodes.flatMap((node,index)=>{
        if (!node.getClientRects().length || node.closest('form') || node.matches('.market__row-btn,.market-header')) return [];
        if (node.querySelectorAll('a,button,[role="button"]').length>1) return [];
        const marker=[node.tagName,node.className,node.title,(node.innerText || '').slice(0,120),node.getAttribute('aria-label'),node.getAttribute('data-testid'),...[...node.querySelectorAll('svg,use,img')].map(icon=>[icon.tagName,icon.getAttribute('class'),icon.getAttribute('alt'),icon.getAttribute('src'),icon.getAttribute('href') || icon.getAttribute('xlink:href')].join(' '))].join(' ');
        return /statistic|статист|stats(?:[._\-#]|$)/i.test(marker)?[index]:[];
    })''')
    if not matches: return []
    popups=[]
    def opened(popup): popups.append(popup)
    page.on('popup',opened)
    original_frames={frame.url for frame in page.frames}
    try:
        try: await controls.nth(matches[0]).click(timeout=3000,no_wait_after=True)
        except Exception: pass
        for _ in range(10):
            candidates=[page.url]+[popup.url for popup in popups]
            ids={value for candidate in candidates if (value:=statistics_id_from_url(candidate))}
            for candidate in [frame.url for frame in page.frames if frame.url not in original_frames]+candidates:
                parsed=urlsplit(candidate)
                if parsed.hostname in ('statshub.yunai.tech','statshub.sportradar.com'):
                    match=re.fullmatch(r'/winline/(?:ru|en)/match/(\d{1,20})/?',parsed.path)
                    if match: ids.add(match[1])
            if ids: return ['https://winline.ru/stats/match/'+value for value in sorted(ids)]
            await asyncio.sleep(.2)
        return []
    finally:
        page.remove_listener('popup',opened)
        for popup in popups:
            if not popup.is_closed(): await popup.close()


def market_document(snapshot: dict, url: str, observed_at: str, metadata: dict | None = None) -> dict:
    metadata = metadata or {}
    event_id = event_id_from_url(url)
    sport = sport_from_url(url)
    if not event_id or not sport:
        raise ValueError("Нужна подтверждённая спортивная страница события.")
    start = metadata.get('kickoff_at')
    date_tokens = lines(snapshot.get('date_text', ''))
    date_labels = [token for token in date_tokens if DATE_RE.fullmatch(token)]
    if len(date_labels) == 1:
        start = kickoff(date_labels[0], observed_at)
    competition = metadata.get('competition', 'Турнир не указан')
    if re.fullmatch(r'/stavki/sport/futbol/angliya/apl/\d+/?', urlsplit(url).path):
        competition = EPL
    home, away = snapshot.get('home') or metadata.get('home'), snapshot.get('away') or metadata.get('away')
    title = snapshot.get('title') or metadata.get('title') or None
    kind = 'match' if home and away else 'outright'
    stats_url, stats_status = event_statistics_link(snapshot.get('statistics_links', []))
    document = {'format': FORMAT, 'source': 'winline', 'sport': sport, 'sport_label': sport_label(sport, metadata.get('sport_label')), 'source_event_id': event_id,
                'event_kind': kind, 'title': title, 'url': url, 'home': home or None, 'away': away or None,
                'statistics_url': stats_url, 'statistics_link_status': stats_status,
                'competition': competition, 'kickoff_at': start,
                'phase': snapshot.get('phase') if snapshot.get('phase') != 'unknown' else metadata.get('phase', 'unknown'),
                'observed_at': observed_at, 'capture_started_at': snapshot.get('capture_started_at', observed_at),
                'parser_version': PARSER_VERSION, 'markets': snapshot.get('markets', []),
                'coverage': {key: snapshot.get(key) for key in ('expected_outcomes', 'unparsed_outcomes', 'hidden_groups',
                                                            'all_tab_selected', 'stable_passes', 'scroll_exhausted')}}
    validate_event_labels_for_diagnostics(document)
    return validate_event_markets(document)


def primary_quote(document: dict) -> dict | None:
    """Project only an explicit regular-time 1X2 into the existing forecasts."""
    if document.get('sport', 'football') != 'football' or document.get('event_kind', 'match') != 'match' or document['phase'] != 'prematch' or not document.get('kickoff_at'):
        return None
    selected = []
    names = {'П1': 'home', '1': 'home', 'X': 'draw', 'Х': 'draw', 'П2': 'away', '2': 'away'}
    for market in document['markets']:
        if not re.fullmatch(r'Исход\s+1[XХ]2', market['name'], re.I) or market.get('parameter'):
            continue
        if re.sub(r'[.\s]', '', market.get('period') or '').casefold() not in ('оснвремя', 'основноевремя'):
            continue
        prices = {}
        for outcome in market['outcomes']:
            key = names.get(outcome['name'].upper())
            if key and outcome['state'] == 'open' and outcome['odds'] is not None:
                prices[key] = outcome['odds']
        if len(market['outcomes']) == 3 and set(prices) == {'home', 'draw', 'away'}:
            selected.append(prices)
    if len(selected) != 1:
        return None
    try:
        row = make_snapshot('winline', document['competition'], document['home'], document['away'],
                            document['kickoff_at'], document.get('capture_started_at', document['observed_at']),
                            [selected[0][key] for key in ('home', 'draw', 'away')], document['url'],
                            external_id=document['source_event_id'], competition_origin='winline_marketbook')
        row['parser_version'] = PARSER_VERSION
        return {'format': QUOTE_FORMAT, 'snapshots': [row]}
    except ValueError:
        return None
