"""Anonymous public projection of genuine, frozen prematch betting decisions."""
import json
import re

from fastapi import HTTPException,Request

from .verdict_outcomes import run
from .vocabulary import text as display_text

METHODOLOGY=[
    'Только предматчевые ответы ИИ с решением «Заходим» и подтверждёнными коэффициентами. Претенденты и пропуски не входят в показатели.',
    'Изменение линии даёт отдельное решение. Одинаковые рекомендации по тому же матчу и линии учитываются один раз; связанные решения не являются независимыми матчами.',
    'Процент верных разборов и счётчик «Решение ИИ верно / не верно» используют только подтверждённые исходы рекомендаций «Заходим». Ожидающие и неподтверждённые итоги не считаются верными или неверными.',
    'Средний коэффициент — среднее арифметическое сохранённых коэффициентов всех учтённых рекомендаций «Заходим», включая ожидающие итога. Он рассчитан по всей выборке и не зависит от страницы списка. Коэффициенты зафиксированы при разборе.',
]


def case(row):
    # A whitelist; never return admin diagnostics, user identity, notes, input
    # facts, tokens or the original full context through the public endpoint.
    return {k:row.get(k) for k in ('id','event','forecast_at','sport_label','market_label','origin','state',
        'decision','stake_percent','actual_score','actual_outcome','bet_won','decision_result','odds')}|{
        'selection_label':row['preference_label']}


def report(repo,*,sport='',offset=0,limit=20,days=30,analysis_id=''):
    data=run(repo,days=days,sport=sport,offset=offset,limit=limit,selection='distinct_bets',analysis_id=analysis_id)
    m=data['metrics']
    return {'format':'matchlab.public-results.v1','checked_at':data['checked_at'],'days':days,'sport':sport,'sports':data['sports'],
        'metrics':{'decisions':m['fixtures'],'matches':data['unique_fixtures'],'settled':m['settled'],'won':m['bet_wins'],
            'lost':m['bet_losses'],'pending':m['pending'],'unresolved':m['unresolved'],'win_rate':m['bet_accuracy'],
            'average_odds':m['average_odds'],'average_odds_count':m['average_odds_count']},
        'daily':[{k:row.get(k) for k in ('day','bet_wins','bet_losses','pending')} for row in data['daily']],
        'matches':[case(row) for row in data['matches']],'total':data['total'],'offset':offset,'limit':limit,
        'methodology':METHODOLOGY,'archive_current':data['archive_health']['state']=='ready'}


def answer_blocks(repo,analysis_id):
    with repo.connection() as db:
        row=db.execute('''SELECT json_extract(payload,'$.context.neural_verdict') verdict,
            json_extract(payload,'$.context.neural_input.unverified_user_notes','$.context.unverified_user_notes','$.context.notes') notes
            FROM event_analyses WHERE id=?''',(analysis_id,)).fetchone()
    if row is None:raise HTTPException(404,'Разбор не найден.')
    verdict=json.loads(row['verdict']);notes=json.loads(row['notes'])
    redacted=any(bool(value) for value in notes)
    blocks=[]
    if not redacted:
        for key,label in [('home','Первый участник'),('away','Второй участник')]:
            value=(verdict.get('comparison') or {}).get(key)
            if isinstance(value,dict) and isinstance(value.get('text'),str):blocks.append({'label':label,'text':display_text(value['text'][:600])})
        for key,label in [('counterargument','Что может помешать'),('market_context','Коэффициенты и решение'),('reason','Почему ИИ принял это решение')]:
            value=verdict.get(key)
            if isinstance(value,dict) and isinstance(value.get('text'),str):blocks.append({'label':label,'text':display_text(value['text'][:600])})
    return {'blocks':blocks,'explanation_redacted':redacted,
        'notice':'Текст этого ответа закрыт, поскольку разбор использовал личные заметки. Публично доступны данные матча и сохранённое решение.' if redacted else
                 'Сохранённый ответ ИИ до начала матча. Подписи приведены к текущему интерфейсу; решение и основания не изменены.'}


def detail(repo,analysis_id,*,settings=None,user=None):
    if not re.fullmatch(r'[a-f0-9]{32}',analysis_id):raise HTTPException(404,'Разбор не найден.')
    rows=report(repo,analysis_id=analysis_id,limit=1)['matches']
    if rows:
        row=rows[0]
        from .billing import active,locked
        if settings and active(repo,settings) and row['state']!='settled':
            gate=locked(repo,settings,repo.event_analysis(analysis_id=analysis_id),user)
            if gate:return gate
        return {**row,**answer_blocks(repo,analysis_id)}
    if settings:
        from .billing import active,entitled,ready
        if active(repo,settings) and entitled(repo,user,analysis_id,settings):
            with repo.connection() as db:stored=db.execute('SELECT user_id,payload FROM event_analyses WHERE id=?',(analysis_id,)).fetchone()
            if stored and stored['user_id'] in (user['id'],'__system_featured__'):
                payload=json.loads(stored['payload']);v=payload.get('context',{}).get('neural_verdict') or {};e=payload.get('event') or {}
                if ready(payload):
                    from .verdict_sports import PROFILES
                    profile=PROFILES.get(e.get('sport'))
                    return {'id':analysis_id,'event':{k:e.get(k) for k in ('home','away','competition','kickoff_at','sport')},
                        'forecast_at':payload['generated_at'],'selection_label':e.get(v.get('preference')) or 'Никто',
                        'market_label':profile.market_label if profile else 'Матч','sport_label':profile.label if profile else 'Спорт',
                        'decision':v['decision'],'odds':v.get('odds'),'stake_percent':v.get('stake_percent'),
                        'state':'archived','purchased_archive':True,'actual_score':None,**answer_blocks(repo,analysis_id)}
    from .today_picks import detail as today_detail
    row=today_detail(repo,analysis_id)
    from .billing import active,locked
    if settings and active(repo,settings):
        gate=locked(repo,settings,repo.event_analysis(analysis_id=analysis_id),user)
        if gate:return gate
    return row


def install(app,repo):
    from fastapi import Response
    from threading import RLock
    import time
    cache={};lock=RLock()
    @app.get('/api/picks/today')
    def today(response:Response,min_odds:float|None=None,max_odds:float|None=None):
        from .today_picks import collection,preview
        response.headers['Cache-Control']='no-store'
        try:
            data=collection(repo,min_odds=min_odds,max_odds=max_odds)
            from .billing import active
            if active(repo,app.state.settings):
                return {**data,'picks':[{k:item[k] for k in ('id','sport_label','forecast_at','odds')} for item in data['picks']]}
            return {**data,'picks':[preview(item) for item in data['picks']]}
        except ValueError as error:raise HTTPException(422,str(error)) from error

    @app.get('/api/results')
    def results(response:Response,sport:str='',offset:int=0,limit:int=20):
        response.headers['Cache-Control']='public, max-age=20'
        try:
            with repo.connection() as db:
                if getattr(db,'dialect',None)=='mysql':
                    from .report_queries import RECEIVED_COLUMN
                    latest=db.execute('SELECT received_at FROM archive_latest_results ORDER BY '+RECEIVED_COLUMN+' DESC LIMIT 1').fetchone()
                    result_revision=(latest[0] if latest else None,)
                else:result_revision=tuple(db.execute('SELECT max(received_at) FROM archive_latest_results').fetchone())
                revision=tuple(db.execute('SELECT count(*),max(generated_at) FROM event_analyses').fetchone())+result_revision
            key=(sport,offset,limit,revision)
            with lock:
                old=cache.get(key)
                if old and time.monotonic()-old[0]<20:return old[1]
                value=report(repo,sport=sport,offset=offset,limit=limit)
                from .billing import active
                if active(repo,app.state.settings):
                    value={**value,'matches':[{**row,'selection_label':'Разбор закрыт'} if row['state']!='settled' else row for row in value['matches']]}
                if len(cache)>=32:cache.clear()
                cache[key]=(time.monotonic(),value)
                return value
        except ValueError as error:raise HTTPException(422,str(error)) from error

    @app.get('/api/results/{analysis_id}')
    def result(analysis_id:str,response:Response,request:Request):
        from . import accounts
        response.headers['Cache-Control']='private, no-store'
        return detail(repo,analysis_id,settings=app.state.settings,user=accounts.current_user(request,False))
