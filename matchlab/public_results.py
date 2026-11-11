"""Anonymous public projection of genuine, frozen prematch betting decisions."""
import json
import re

from fastapi import HTTPException

from .verdict_outcomes import run

METHODOLOGY=[
    'Только предматчевые ответы ИИ с решением «Ставить» и подтверждёнными коэффициентами. Претенденты и пропуски не входят в показатели.',
    'Изменение линии даёт отдельное решение. Одинаковые рекомендации по тому же матчу и линии учитываются один раз; связанные решения не являются независимыми матчами.',
    'Ожидающие и неподтверждённые итоги исключены из процента выигранных решений и виртуальной доходности.',
    'Виртуальный пример: начальный банк 10 000 ₽, каждая рассчитанная ставка — от 5% до 10% именно начального банка (500–1 000 ₽). Сумма ставки не увеличивается после побед.',
    'Изменение банка — суммарная прибыль или убыток по подтверждённым решениям, делённые на начальные 10 000 ₽. Ожидающие итога решения не рассчитаны. Одновременные ставки и наличие свободных средств не моделируются.',
    'Переключатель меняет только виртуальный пример. Сохранённые решения ИИ и указанный в них размер ставки не меняются. Реальные деньги не используются.',
]


def simulation(metrics):
    initial=10000
    scenarios=[]
    for percent in range(5,11):
        stake=initial*percent/100
        profit=round(stake*metrics['flat_profit_units'],2) if metrics['priced_bets'] else None
        scenarios.append({'stake_percent':percent,'stake':stake,'profit':profit,
            'final_bank':round(initial+profit,2) if profit is not None else None,
            'bank_return':profit/initial if profit is not None else None})
    return {'initial_bank':initial,'basis':'fixed_initial_bank','settled_bets':metrics['priced_bets'],
        'pending_bets':metrics['pending']+metrics['unresolved'],'scenarios':scenarios}


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
        'simulation':simulation(m),
        'daily':[{k:row.get(k) for k in ('day','bet_wins','bet_losses','pending','paper_profit','cumulative_bank')} for row in data['daily']],
        'matches':[case(row) for row in data['matches']],'total':data['total'],'offset':offset,'limit':limit,
        'methodology':METHODOLOGY,'archive_current':data['archive_health']['state']=='ready'}


def answer_blocks(repo,analysis_id):
    with repo.connection() as db:
        row=db.execute('''SELECT json_extract(payload,'$.context.neural_verdict') verdict,
            json_extract(payload,'$.context.neural_input.unverified_user_notes','$.context.unverified_user_notes','$.context.notes') notes
            FROM event_analyses WHERE id=?''',(analysis_id,)).fetchone()
    if row is None:raise HTTPException(404,'Прогноз не найден.')
    verdict=json.loads(row['verdict']);notes=json.loads(row['notes'])
    redacted=any(bool(value) for value in notes)
    blocks=[]
    if not redacted:
        for key,label in [('home','Первый участник'),('away','Второй участник')]:
            value=(verdict.get('comparison') or {}).get(key)
            if isinstance(value,dict) and isinstance(value.get('text'),str):blocks.append({'label':label,'text':value['text'][:600]})
        for key,label in [('counterargument','Что может помешать'),('market_context','Коэффициенты и решение'),('reason','Почему ИИ принял это решение')]:
            value=verdict.get(key)
            if isinstance(value,dict) and isinstance(value.get('text'),str):blocks.append({'label':label,'text':value['text'][:600]})
    return {'blocks':blocks,'explanation_redacted':redacted,
        'notice':'Текст этого ответа закрыт, поскольку разбор использовал личные заметки. Публично доступны данные матча и сохранённое решение.' if redacted else
                 'Сохранённый ответ ИИ до начала матча. Текст не переписан с учётом результата.'}


def detail(repo,analysis_id):
    if not re.fullmatch(r'[a-f0-9]{32}',analysis_id):raise HTTPException(404,'Прогноз не найден.')
    rows=report(repo,analysis_id=analysis_id,limit=1)['matches']
    if rows:return {**rows[0],**answer_blocks(repo,analysis_id)}
    from .today_picks import detail as today_detail
    return today_detail(repo,analysis_id)


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
            return {**data,'picks':[preview(item) for item in data['picks']]}
        except ValueError as error:raise HTTPException(422,str(error)) from error

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
