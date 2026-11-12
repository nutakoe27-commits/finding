"""Anonymous server-rendered match pages. Never read or generate AI answers."""
from datetime import datetime,timezone
from html import escape
import json
import math
from pathlib import Path
import re
from threading import RLock
import time
import unicodedata
from xml.etree.ElementTree import Element,SubElement,tostring

from fastapi import Request
from fastapi.responses import HTMLResponse,RedirectResponse,Response

from . import archive
from .bookmakers import instant
from .sports import catalog_sport,sport_label,EXCLUDED
from .verdict_outcomes import MSK
from .verdict_sports import supported

STATIC=Path(__file__).parent/'static'
PAGE_SIZE=50
SITEMAP_SIZE=5000
NS='http://www.sitemaps.org/schemas/sitemap/0.9'
MONTHS=('января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря')


def eligible(event):
    try:
        return (bool(re.fullmatch('[0-9]{1,20}',str(event.get('source_event_id') or ''))) and
            isinstance(event.get('sport'),str) and bool(event['sport']) and
            catalog_sport(event.get('sport')) and not EXCLUDED.search(str(event.get('sport_label') or event.get('sport') or '')) and
            event.get('event_kind','match')=='match' and all(isinstance(event.get(k),str) and event[k].strip() for k in ('home','away','competition')) and
            event['home']!=event['away'] and bool(instant(event['kickoff_at'])))
    except (KeyError,ValueError,TypeError):return False


def slug(value):
    alphabet=dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
        ('a','b','v','g','d','e','yo','zh','z','i','y','k','l','m','n','o','p','r','s','t','u','f','kh','ts','ch','sh','sch','','y','','e','yu','ya')))
    value=''.join(alphabet.get(c,c) for c in value.casefold())
    value=unicodedata.normalize('NFKD',value).encode('ascii','ignore').decode()
    return re.sub('[^a-z0-9]+','-',value).strip('-')[:55].rstrip('-') or 'match'


def match_path(event):return '/match/'+event['source_event_id']+'/'+slug(event['home'])+'-'+slug(event['away'])


def public_links(data):
    return {**data,'events':[{**event,'public_url':match_path(event) if eligible(event) else None} for event in data['events']]}


def when(value,clock=True):
    at=instant(value).astimezone(MSK)
    return f'{at.day} {MONTHS[at.month-1]} {at.year}'+(f', {at:%H:%M} МСК' if clock else '')


def meta(origin,path,title,description,*,index=True,schema=()):
    canonical=origin+path
    blocks=''.join('<script type="application/ld+json">'+json.dumps(item,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')+'</script>' for item in schema)
    return (f'<title>{escape(title)}</title><meta name="description" content="{escape(description,quote=True)}">'
        f'<link rel="canonical" href="{escape(canonical,quote=True)}">'
        f'<meta name="robots" content="{"index,follow" if index else "noindex,follow"}">'
        f'<meta property="og:type" content="website"><meta property="og:locale" content="ru_RU">'
        f'<meta property="og:title" content="{escape(title,quote=True)}"><meta property="og:description" content="{escape(description,quote=True)}">'
        f'<meta property="og:url" content="{escape(canonical,quote=True)}"><meta property="og:site_name" content="prognozich ai">'
        f'<meta name="twitter:card" content="summary">{blocks}')


def shell(head,body):
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'+head+
        '<link rel="icon" href="/static/icon.svg" type="image/svg+xml"><script src="/static/theme.js?v=20261109-accounts1"></script><link rel="stylesheet" href="/static/seo.css?v=20261112-interface1"></head><body>'
        '<a class="seo-skip" href="#content">К содержимому</a><header class="seo-header seo-shell"><a class="seo-brand" href="/">prognozich <b>ai</b></a>'
        '<nav aria-label="Основная навигация"><a href="/matches/">Матчи и прогнозы</a><a href="/#results">Результаты ИИ</a><span class="seo-age">18+</span></nav></header>'
        '<main id="content" class="seo-shell">'+body+'</main><footer class="seo-footer seo-shell"><p>Спортивная аналитика. Сервис не принимает ставки. Прогноз ИИ может ошибаться.</p>'
        '<nav aria-label="Документы"><a href="/static/legal/terms.html">Пользовательское соглашение</a><a href="/static/legal/privacy.html">Конфиденциальность</a><a href="/static/legal/pricing.html">Тарифы</a><a href="/static/legal/offer.html">Оферта</a><a href="/static/legal/payment-refund.html">Оплата и возвраты</a><a href="/static/legal/contacts.html">Реквизиты</a></nav></footer></body></html>')


def home_html(settings):
    value=(STATIC/'index.html').read_text()
    schema={'@context':'https://schema.org','@type':'WebSite','name':'prognozich ai','url':settings.public_origin+'/','inLanguage':'ru'}
    return value.replace('<!-- SEO_HEAD -->',f'<link rel="canonical" href="{escape(settings.public_origin,quote=True)}/"><meta property="og:url" content="{escape(settings.public_origin,quote=True)}/">'
        +'<script type="application/ld+json">'+json.dumps(schema,ensure_ascii=False).replace('<','\\u003c')+'</script>')


class MatchIndex:
    def __init__(self,repo):self.repo=repo;self.lock=RLock();self.at=-math.inf;self.rows=()

    def entries(self):
        with self.lock:
            if time.monotonic()-self.at<60:return self.rows
            values={}
            with self.repo.connection() as db:
                for row in db.execute('SELECT metadata FROM bookmaker_event_markets'):
                    event=json.loads(row['metadata'])
                    if eligible(event):values[event['source_event_id']]=event
                for row in db.execute('SELECT payload FROM archive_headers'):
                    raw=archive.unpacked(row['payload']);members=raw.get('members') or []
                    event={'source_event_id':raw['event_id'],'home':members[0] if members else None,'away':members[1] if len(members)==2 else None,
                        'competition':raw.get('competition'),'sport':archive.sport_key(raw['sport_label'],raw['sport_id']),
                        'sport_label':raw['sport_label'],'kickoff_at':raw.get('kickoff_at')}
                    # Current source identity wins even when it became ineligible.
                    values.pop(raw['event_id'],None)
                    if eligible(event):values[event['source_event_id']]=event
            keys=('source_event_id','home','away','competition','sport','sport_label','kickoff_at')
            now=datetime.now(timezone.utc)
            def order(event):
                at=instant(event['kickoff_at']);return (0,at.timestamp(),event['source_event_id']) if at>now else (1,-at.timestamp(),event['source_event_id'])
            self.rows=tuple({k:e.get(k) for k in keys} for e in sorted(values.values(),key=order))
            self.at=time.monotonic();return self.rows


def read_event(repo,identifier):
    if not re.fullmatch('[0-9]{1,20}',identifier):return None
    event=archive.document(repo,identifier) or repo.event_markets(identifier)
    return event if event and eligible(event) else None


def stats_table(repo,event):
    from .season_evidence import source_pairs
    snapshot=repo.statistics(event['source_event_id'])
    if not snapshot or snapshot.get('state')!='ok' or snapshot.get('source_event_id')!=event['source_event_id']:return ''
    for section in snapshot.get('sections',[]):
        if not str(section.get('title') or '').casefold().startswith('статистика сезона'):continue
        pairs=source_pairs(section)[:6]
        if not pairs:continue
        filters=', '.join(section.get('filters') or []) or 'режим показателей не указан источником'
        rows=''.join(f'<tr><th scope="row">{escape(p["metric"])}</th><td>{escape(p["home"])}</td><td>{escape(p["away"])}</td></tr>' for p in pairs)
        return (f'<section class="seo-panel"><h2>Показатели участников</h2><p class="seo-muted">Статистика сезона · {escape(filters)}. Снимок: {escape(when(snapshot["observed_at"]))}.</p>'
            f'<div class="seo-table-wrap"><table><thead><tr><th>Показатель</th><th>{escape(event["home"])}</th><th>{escape(event["away"])}</th></tr></thead><tbody>{rows}</tbody></table></div>'
            '<p class="seo-muted">Показатели источника описывают доступную выборку и не являются вероятностями будущей победы.</p></section>')
    return ''


def event_html(repo,event,origin,index):
    path=match_path(event);url=origin+path;pair=event['home']+' — '+event['away'];date=when(event['kickoff_at'],False)
    phase=event.get('phase');future=instant(event['kickoff_at'])>datetime.now(timezone.utc)
    can_analyze=supported(event) and phase=='prematch' and future and not event.get('is_stale') and event.get('source_present') is not False
    title=pair+(' — прогноз ИИ на ' if can_analyze else ' — матч ')+date+' | prognozich ai'
    competition=event['competition'];label=sport_label(event['sport'],event.get('sport_label'))
    description=f'{pair}, {competition}, {when(event["kickoff_at"])}. '+('Сведения о встрече и запрос разбора от ИИ: сравнение участников, аргументы и решение.' if can_analyze else 'Дата встречи, доступные сведения и подтверждённый результат при его наличии.')
    state='Матч завершён' if phase=='finished' else 'Сейчас в игре' if phase=='live' else 'Предстоящий матч' if can_analyze or phase=='prematch' and future else 'Сведения о матче уточняются'
    score=(event.get('final_result') or {}).get('score_text') if phase=='finished' else event.get('live_score') if phase=='live' else None
    schema={'@context':'https://schema.org','@type':'SportsEvent','@id':url+'#event','url':url,'name':pair,'sport':label,
        'startDate':instant(event['kickoff_at']).isoformat(),'description':description,'mainEntityOfPage':url,
        'competitor':[{'@type':'Person' if event['sport']=='tennis' else 'SportsTeam','name':event[s]} for s in ('home','away')]}
    if phase=='prematch' and future:schema['eventStatus']='https://schema.org/EventScheduled'
    breadcrumb={'@context':'https://schema.org','@type':'BreadcrumbList','itemListElement':[
        {'@type':'ListItem','position':1,'name':'Главная','item':origin+'/'},
        {'@type':'ListItem','position':2,'name':'Матчи и прогнозы','item':origin+'/matches/'},
        {'@type':'ListItem','position':3,'name':pair,'item':url}]}
    body=(f'<nav class="seo-breadcrumb" aria-label="Хлебные крошки"><a href="/">Главная</a><span>/</span><a href="/matches/">Матчи</a><span>/</span><span>{escape(label)}</span></nav>'
        f'<section class="seo-hero"><p class="seo-eyebrow">{escape(label)} · {escape(state)}</p><h1>{escape(pair)}</h1><p class="seo-competition">{escape(competition)}</p>'
        f'<time datetime="{escape(event["kickoff_at"],quote=True)}">{escape(when(event["kickoff_at"]))}</time>'
        +(f'<p class="seo-score">{escape(str(score))}</p>' if score else '')+'</section>')
    if can_analyze:
        target='/#'+('stream/' if event.get('source_kind')=='stream' else 'match/')+event['source_event_id']
        body+=(f'<section class="seo-panel seo-analysis"><div><p class="seo-eyebrow">ПРОГНОЗ ОТ ИИ</p><h2>Кого выделит ИИ в этой встрече?</h2>'
            f'<p>Запросите разбор матча «{escape(pair)}». ИИ сравнит доступную историю и показатели участников, учтёт коэффициенты и объяснит решение.</p>'
            '<p class="seo-muted">Готового вердикта на этой странице нет. Разбор запускается в сервисе по вашему запросу; при недостатке данных или сомнениях ИИ может рекомендовать пропустить матч. Полный ответ открывается по условиям сервиса.</p></div>'
            f'<a class="seo-button" href="{target}">Открыть матч и запросить разбор →</a></section>')
    else:
        message=('Предматчевый разбор для завершившейся или начавшейся встречи недоступен.' if phase in ('finished','live') else
            'Запланированное время встречи уже наступило, но её итог ещё не подтверждён.' if not future else
            'Предматчевый ИИ-разбор этой встречи сейчас недоступен. Доступность зависит от вида спорта и актуальности данных.')
        body+='<section class="seo-panel"><h2>Информация о встрече</h2><p>'+message+'</p><a class="seo-link" href="/#matches">Выбрать актуальный матч →</a></section>'
    body+=stats_table(repo,event)
    headline=event.get('headline') or {}
    if headline.get('outcomes') and event.get('observed_at'):
        odds=[]
        for item in headline['outcomes'][:3]:
            value=item.get('odds')
            if item.get('state')=='open' and type(value) in (int,float) and math.isfinite(value) and 1<value<=100000:
                odds.append(f'<tr><th scope="row">{escape(str(item.get("name") or "Исход"))}</th><td>{value:.2f}</td></tr>')
        if odds:
            body+='<section class="seo-panel"><h2>Снимок коэффициентов</h2><p>'+escape(str(headline.get('name') or 'Рынок матча'))+'</p><table><thead><tr><th>Исход</th><th>Коэффициент</th></tr></thead><tbody>'+''.join(odds)+'</tbody></table><p class="seo-muted">Данные на '+escape(when(event['observed_at']))+'. Коэффициенты могли измениться; это цены источника, а не вероятности и не рекомендация ставки.</p></section>'
    related=[e for e in index.entries() if e['sport']==event['sport'] and e['source_event_id']!=event['source_event_id']][:6]
    if related:body+='<section class="seo-panel"><h2>Другие встречи</h2><div class="seo-grid">'+''.join(card(e) for e in related)+'</div></section>'
    body+='<p class="seo-muted seo-note">Источник сведений о матче — Winline. Расписание и показатели могут обновляться. Прогнозы не гарантируют выигрыш. Только для пользователей 18+.</p>'
    return shell(meta(origin,path,title,description,schema=(schema,breadcrumb)),body)


def card(event):
    return (f'<a class="seo-card" href="{match_path(event)}"><span class="seo-muted">{escape(sport_label(event["sport"],event.get("sport_label")))}</span>'
        f'<strong>{escape(event["home"])} — {escape(event["away"])}</strong><span>{escape(event["competition"])}</span><time datetime="{escape(event["kickoff_at"],quote=True)}">{escape(when(event["kickoff_at"]))}</time></a>')


def xml_response(tag,items):
    root=Element(tag,xmlns=NS)
    for kind,url in items:
        child=SubElement(root,kind);SubElement(child,'loc').text=url
    return Response(tostring(root,encoding='utf-8',xml_declaration=True),media_type='application/xml',headers={'Cache-Control':'public, max-age=300'})


def install(app,repo,settings):
    index=MatchIndex(repo);origin=settings.public_origin
    def missing():
        return HTMLResponse(shell(meta(origin,'/matches/','Матч не найден | prognozich ai','Эта страница недоступна.',index=False),
            '<section class="seo-hero"><h1>Матч не найден</h1><p>Выберите другую встречу в каталоге.</p><a class="seo-button" href="/matches/">Открыть каталог матчей</a></section>'),status_code=404,headers={'X-Robots-Tag':'noindex','Cache-Control':'no-store'})

    @app.api_route('/robots.txt',methods=['GET','HEAD'],include_in_schema=False)
    def robots():return Response('User-agent: *\nDisallow: /api/\nDisallow: /go/\n\nSitemap: '+origin+'/sitemap.xml\n',media_type='text/plain',headers={'Cache-Control':'public, max-age=300'})

    @app.api_route('/sitemap.xml',methods=['GET','HEAD'],include_in_schema=False)
    def sitemap():
        chunks=math.ceil(len(index.entries())/SITEMAP_SIZE)
        return xml_response('sitemapindex',[('sitemap',origin+'/sitemap-pages.xml')]+[('sitemap',origin+f'/sitemap-matches-{n}.xml') for n in range(1,chunks+1)])

    @app.api_route('/sitemap-pages.xml',methods=['GET','HEAD'],include_in_schema=False)
    def pages_map():
        count=len(index.entries());paths=['/']+(['/matches/'] if count else [])
        paths+=[f'/matches/?page={n}' for n in range(2,math.ceil(count/PAGE_SIZE)+1)]
        return xml_response('urlset',[('url',origin+path) for path in paths])

    @app.api_route('/sitemap-matches-{part}.xml',methods=['GET','HEAD'],include_in_schema=False)
    def matches_map(part:str):
        if not re.fullmatch('[1-9][0-9]{0,7}',part):return missing()
        rows=index.entries();page=int(part);batch=rows[(page-1)*SITEMAP_SIZE:page*SITEMAP_SIZE]
        if not batch:return missing()
        return xml_response('urlset',[('url',origin+match_path(e)) for e in batch])

    @app.api_route('/matches/',methods=['GET','HEAD'],include_in_schema=False)
    def listing(request:Request):
        raw=request.query_params.get('page','1')
        if not re.fullmatch('[1-9][0-9]{0,7}',raw):return missing()
        if raw=='1' and 'page' in request.query_params:return RedirectResponse('/matches/',status_code=301)
        page=int(raw);rows=index.entries();batch=rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
        if not batch and (rows or page!=1):return missing()
        path='/matches/'+(f'?page={page}' if page>1 else '')
        title='Матчи и прогнозы от ИИ'+(f' — страница {page}' if page>1 else '')+' | prognozich ai'
        description='Каталог спортивных матчей: участники, турниры, время начала и страницы встреч. ИИ-разборы для поддерживаемых видов спорта доступны по запросу.'
        body='<section class="seo-hero"><p class="seo-eyebrow">СПОРТИВНАЯ АНАЛИТИКА</p><h1>Матчи и прогнозы от ИИ</h1><p>Найдите встречу и откройте её страницу: участники, турнир, дата и доступные сведения о матче. Время указано по Москве.</p><a class="seo-link" href="/#matches">Предстоящие матчи и поиск в сервисе →</a></section>'
        body+='<div class="seo-grid">'+''.join(card(e) for e in batch)+'</div>' if batch else '<p>Матчи появятся после получения расписания.</p>'
        links=[]
        if page>1:links.append('<a rel="prev" href="/matches/'+(f'?page={page-1}' if page>2 else '')+'">← Назад</a>')
        if page*PAGE_SIZE<len(rows):links.append(f'<a rel="next" href="/matches/?page={page+1}">Далее →</a>')
        body+='<nav class="seo-pagination" aria-label="Страницы каталога">'+''.join(links)+'</nav>'
        return HTMLResponse(shell(meta(origin,path,title,description,index=bool(rows)),body),headers={'Cache-Control':'public, max-age=60'})

    @app.api_route('/match/{identifier}',methods=['GET','HEAD'],include_in_schema=False)
    @app.api_route('/match/{identifier}/{tail:path}',methods=['GET','HEAD'],include_in_schema=False)
    def match(identifier:str,request:Request,tail:str=''):
        event=read_event(repo,identifier)
        if not event:return missing()
        path=match_path(event)
        if request.url.path!=path:return RedirectResponse(path,status_code=301)
        return HTMLResponse(event_html(repo,event,origin,index),headers={'Cache-Control':'public, max-age=60'})
