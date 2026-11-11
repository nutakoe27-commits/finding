"""Anonymous public projection of genuine, frozen prematch betting decisions."""
import json
import re

from fastapi import HTTPException

from .verdict_outcomes import run

METHODOLOGY=[
    'Только предматчевые ответы ИИ с решением «Ставить» и подтверждёнными коэффициентами. Претенденты и пропуски не входят в показатели.',
    'Изменение линии даёт отдельное решение. Одинаковые рекомендации по тому же матчу и линии учитываются один раз; связанные решения не являются независимыми матчами.',
    'Ожидающие и неподтверждённые итоги исключены из процента выигранных решений и виртуальной доходности.',
    'Виртуальный расчёт использует сохранённый коэффициент и лимит ставки от начального банка 100. Это история решений, без реальных ставок.',
]


def case(row):
    # A whitelist; never return admin diagnostics, user identity, notes, input
    # facts, tokens or the original full context through the public endpoint.
    return {k:row.get(k) for k in ('id','event','forecast_at','sport_label','market_label','origin','state',
        'decision','stake_percent','actual_score','actual_outcome','bet_won','decision_result','odds','paper_stake','paper_profit')}|{
        'selection_label':row['preference_label']}


def report(repo,*,sport='',offset=0,limit=20,days=30,analysis_id=''):
    data=run(repo,days=days,sport=sport,offset=offset,limit=limit,selection='distinct_bets',analysis_id=analysis_id)
    m=data['metrics']
    return {'format':'matchlab.public-results.v1','checked_at':data['checked_at'],'days':days,'sport':sport,'sports':data['sports'],
        'metrics':{'decisions':m['fixtures'],'matches':data['unique_fixtures'],'settled':m['settled'],'won':m['bet_wins'],
            'lost':m['bet_losses'],'pending':m['pending'],'unresolved':m['unresolved'],'win_rate':m['bet_accuracy'],
            'paper_roi':m['paper_roi'],'paper_profit':m['paper_profit'],'paper_staked':m['paper_staked']},
        'daily':[{k:row.get(k) for k in ('day','bet_wins','bet_losses','pending','paper_profit','cumulative_bank')} for row in data['daily']],
        'matches':[case(row) for row in data['matches']],'total':data['total'],'offset':offset,'limit':limit,
        'methodology':METHODOLOGY,'archive_current':data['archive_health']['state']=='ready'}


def detail(repo,analysis_id):
    if not re.fullmatch(r'[a-f0-9]{32}',analysis_id):raise HTTPException(404,'Прогноз не найден.')
    rows=report(repo,analysis_id=analysis_id,limit=1)['matches']
    if not rows:raise HTTPException(404,'Этот ответ не входит в публичную историю рекомендаций.')
    with repo.connection() as db:
        row=db.execute('''SELECT json_extract(payload,'$.context.neural_verdict') verdict,
            json_extract(payload,'$.context.neural_input.unverified_user_notes','$.context.unverified_user_notes','$.context.notes') notes
            FROM event_analyses WHERE id=?''',(analysis_id,)).fetchone()
    verdict=json.loads(row['verdict']);notes=json.loads(row['notes'])
    redacted=any(bool(value) for value in notes)
    blocks=[]
    if not redacted:
        for key,label in [('home','Первый участник'),('away','Второй участник')]:
            value=(verdict.get('comparison') or {}).get(key)
            if isinstance(value,dict) and isinstance(value.get('text'),str):blocks.append({'label':label,'text':value['text'][:600]})
        for key,label in [('counterargument','Что может помешать'),('market_context','Коэффициенты и решение'),('reason','Почему ИИ рекомендовал ставить')]:
            value=verdict.get(key)
            if isinstance(value,dict) and isinstance(value.get('text'),str):blocks.append({'label':label,'text':value['text'][:600]})
    return {**rows[0],'blocks':blocks,'explanation_redacted':redacted,
        'notice':'Текст этого ответа закрыт, поскольку разбор использовал личные заметки. Решение и результат учтены в общей статистике.' if redacted else
                 'Сохранённый ответ ИИ до начала матча. Текст не переписан с учётом результата.'}


def install(app,repo):
    from fastapi import Response
    from threading import RLock
    import time
    cache={};lock=RLock()
    @app.get('/api/results')
    def results(response:Response,sport:str='',offset:int=0,limit:int=20):
        response.headers['Cache-Control']='public, max-age=20'
        try:
            with repo.connection() as db:
                revision=tuple(db.execute('SELECT count(*),max(generated_at) FROM event_analyses').fetchone())+tuple(db.execute('SELECT max(received_at) FROM archive_latest_results').fetchone())
            key=(sport,offset,limit,revision)
            with lock:
                old=cache.get(key)
                if old and time.monotonic()-old[0]<20:return old[1]
                value=report(repo,sport=sport,offset=offset,limit=limit)
                if len(cache)>=32:cache.clear()
                cache[key]=(time.monotonic(),value)
                return value
        except ValueError as error:raise HTTPException(422,str(error)) from error

    @app.get('/api/results/{analysis_id}')
    def result(analysis_id:str,response:Response):
        response.headers['Cache-Control']='public, max-age=20'
        return detail(repo,analysis_id)
