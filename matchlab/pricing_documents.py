"""Public financial documents rendered from exactly the checkout catalogue."""
from datetime import datetime
from html import escape
from pathlib import Path
import re

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from . import pricing

ROOT=Path(__file__).parent/'static'/'legal'
DOCUMENTS=('pricing','offer','recurring','payment-refund','terms','privacy','consent','test-purchase')


def rub(n):
    whole=f'{n//100:,}'.replace(',',' ')
    return whole+(f',{n%100:02d}' if n%100 else '')+' ₽'


def units(n):
    return str(n)+' '+('разбор' if n%10==1 and n%100!=11 else 'разбора' if n%10 in (2,3,4) and n%100 not in (12,13,14) else 'разборов')


def days(n):return str(n)+' '+('день' if n%10==1 and n%100!=11 else 'дня' if n%10 in (2,3,4) and n%100 not in (12,13,14) else 'дней')


def window(d):
    def at(s):return datetime.fromisoformat(s).astimezone(pricing.MSK).strftime('%d.%m.%Y, %H:%M МСК')
    return ('с '+at(d['starts_at']) if d['starts_at'] else 'с начала действия этой редакции')+(' до '+at(d['ends_at']) if d['ends_at'] else ' до публикации новых условий')


def first_price(p,sub):
    base=rub(p['amount_kopecks']);d=p['discount']
    if not d['percent']:return escape(base)
    sale=rub(p['amount_kopecks']*(100-d['percent'])//100)
    return escape(sale)+' по акции −'+str(d['percent'])+'%<br><small>'+escape(window(d))+'. Вне акции '+escape(base)+'.'+(' Скидка только на первый платёж.' if sub else '')+'</small>'


def table(catalog,sub=True):
    rows=[]
    for p in catalog['subscriptions' if sub else 'packages']:
        if not p['enabled']:continue
        label='Безлимит' if not p['credits'] else units(p['credits'])
        if sub:
            label+=' / '+days(p['period_days'])
            hours=p['period_days']*24
            grant=(str(p['credits'])+' на постоянный баланс') if p['credits'] else ('Доступ на '+str(hours)+' '+('часа' if hours%10 in (2,3,4) and hours%100 not in (12,13,14) else 'часов'))
            values=[escape(label),escape(grant),first_price(p,True),escape(rub(p['amount_kopecks'])+' раз в '+days(p['period_days']))]
        else:values=[escape(label),first_price(p,False),'Однократно; без автопродления']
        rows.append('<tr>'+''.join('<td>'+v+'</td>' for v in values)+'</tr>')
    if not rows:return '<p>Новые '+('подписки' if sub else 'разовые покупки')+' сейчас недоступны. Ранее оплаченные права сохраняются.</p>'
    headings=('Подписка','Начисление или доступ','Первый платёж','Последующие платежи') if sub else ('Разовый пакет','Цена','Вид оплаты')
    return '<table><thead><tr>'+''.join('<th>'+v+'</th>' for v in headings)+'</tr></thead><tbody>'+''.join(rows)+'</tbody></table>'


def render(name,catalog,*,archive=False,preview=False):
    if name not in DOCUMENTS:raise HTTPException(404,'Документ не найден.')
    html=(ROOT/(name+'.html')).read_text()
    money='<p><strong>Денежная оплата подписки и расход разборов — разные действия.</strong> Первый платёж и дальнейшие списания указаны в таблице и подтверждаются при оформлении. Деньги списываются по периоду выбранного тарифа до отмены независимо от числа запросов и решений ИИ. Разбор расходуется только при открытии решения «Ставить»; полный ответ «Пропустить» бесплатен.</p>'
    period='<p>Первый платёж — при оформлении. Следующее списание — не раньше окончания оплаченного периода после предыдущей успешной оплаты; период и сумма указаны в тарифе. Списание не привязано к полуночи. Точный срок показан в личном кабинете по МСК. Пропущенные периоды задним числом не оплачиваются.</p>'
    discounts='<h2>Скидки и условия акций</h2><p>Скидка на подписку применяется <strong>только к первому платежу</strong> при оформлении в указанный срок акции. Дальнейшие списания — по полной регулярной цене, явно показанной до оформления и в отдельном согласии на автоплатежи. Цена со скидкой округляется вниз до копейки. Начало включается в срок акции, окончание — не включается; время указано по Москве. Для разовой покупки скидка применяется к цене выбранного пакета. Новая редакция тарифов и завершение акции не меняют условия уже оформленного договора.</p>'
    replacements={'SUBSCRIPTION_TABLE':table(catalog),'PACKAGE_TABLE':table(catalog,False),'MONEY_RULE':money,'PERIOD_RULE':period,'DISCOUNT_RULE':discounts}
    for key,value in replacements.items():html=html.replace('<!-- '+key+' -->',value)
    if catalog['revision']!='builtin':
        date=datetime.fromisoformat(catalog['published_at']).astimezone(pricing.MSK).date().isoformat()
        version=date+'.pricing.'+catalog['revision']+('.test' if name=='test-purchase' else '.recurring' if name=='recurring' else '')
        html=re.sub(r'2026-11-12(?:\.test)?\.v[0-9]+',version,html)
    if preview:html=html.replace('<main>','<main><p class="notice"><strong>Предпросмотр. Эти условия ещё не опубликованы.</strong></p>',1)
    if archive:
        if 'name="robots"' not in html:html=html.replace('<head>','<head><meta name="robots" content="noindex,nofollow">',1)
    elif not preview and catalog['revision']!='builtin':
        link='/legal/versions/'+catalog['revision']+'/'+name
        html=html.replace('<footer>','<p class="meta"><a href="'+link+'">Сохранённая редакция условий</a></p><footer>')
    return html


def install(app,repo):
    # Registered before /static so these familiar links remain readable without JS.
    def document_handler(document):
        def handler():
            return HTMLResponse(render(document,pricing.load(repo)),headers={'Cache-Control':'no-store'})
        return handler
    for name in DOCUMENTS:
        app.add_api_route('/static/legal/'+name+'.html',document_handler(name),methods=['GET','HEAD'],include_in_schema=False)

    @app.api_route('/legal/versions/{revision}/{document}',methods=['GET','HEAD'],include_in_schema=False)
    def previous(revision:str,document:str):
        if not re.fullmatch('[a-f0-9]{32}',revision) or document not in DOCUMENTS:raise HTTPException(404,'Редакция не найдена.')
        with repo.connection() as db:row=db.execute('SELECT value FROM site_settings WHERE key=?',('pricing:document:'+revision+':'+document,)).fetchone()
        if not row:raise HTTPException(404,'Редакция не найдена.')
        return HTMLResponse(row['value'],headers={'X-Robots-Tag':'noindex','Cache-Control':'public, max-age=31536000, immutable'})
