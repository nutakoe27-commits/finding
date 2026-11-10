"""Ten frozen random prematch inputs, blind Qwen decisions and paper settlement."""
import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import secrets
import sqlite3
from urllib.parse import quote as uri_quote

from .bookmakers import instant
from .config import Settings
from .neural_verdict import VERSION,build_packet,encoded,generate,latest_historical_quote


def sample_inputs(db,limit,seed):
    from .winline_backtest import results
    from .snapshot_evaluation import recorded_inputs
    rows,excluded=results(db)
    candidates,rejected,pending,inventory=recorded_inputs(db,rows)
    rng=random.Random(seed)
    return rng.sample(candidates,min(limit,len(candidates))),{'eligible':len(candidates),'excluded_statistics':rejected,
        'excluded_results':excluded,'pending_associations':pending,'snapshot_inventory':inventory}


def prepare(db,settings,limit=10,seed=None):
    from .model_comparison import historical_quotes
    from .snapshot_evaluation import ReadOnlyInputs
    if not 1<=limit<=100:raise ValueError('Размер теста должен быть от 1 до 100.')
    seed=secrets.randbits(32) if seed is None else seed
    sampled,inventory=sample_inputs(db,limit,seed)
    if not sampled:raise ValueError('Нет завершённых футбольных матчей с сохранённой предматчевой статистикой.')
    quotes=historical_quotes(db);cases=[]
    for candidate in sampled:
        target=candidate['result'];at=candidate['as_of'];stats=candidate['statistics']
        stats['age_seconds']=max(0,int((at-instant(stats['observed_at'])).total_seconds()));stats['is_stale']=stats['age_seconds']>3600
        event={k:target[k] for k in ('home','away','competition')}
        event.update(source_event_id=candidate['event_id'],sport='football',sport_label='Футбол',phase='prematch',kickoff_at=target['at'].isoformat())
        market=latest_historical_quote(event,at,quotes)
        packet=build_packet(ReadOnlyInputs(db),event,stats,at,market)
        cases.append({'result_key':target['key'],'event':event,'input':packet,
            'input_sha256':hashlib.sha256(encoded(packet).encode()).hexdigest(),'state':'pending'})
    return {'format':'matchlab.neural-verdict-test.v1','policy_version':VERSION,'scope':'team_winner_90m',
        'created_at':datetime.now(timezone.utc).isoformat(),'state':'prepared','seed':seed,'requested':limit,
        'selected':len(cases),'model':settings.qwen_model,'max_stake_pct':settings.verdict_max_stake_pct,
        'inputs_with_fresh_prices':sum(bool(c['input']['options']) for c in cases),
        'inputs_requiring_skip':sum(c['input']['policy']['force_skip'] for c in cases),
        'source_inventory':inventory,'cases':cases,'real_money_used':False,
        'limitations':['Случайная выборка фиксируется до вызовов ИИ. Пропуски не считаются угаданными матчами.',
            'Это тест на записанном предматчевом входе, а не фактически выданные тогда рекомендации.',
            'История, статистика и коэффициенты ограничены временем каждого входа. Итог целевого матча ИИ не получает.',
            'Размер ставки — фиксированный ограничитель для виртуального теста, не оценка вероятности и не обещание безопасности.',
            'Десять матчей не подтверждают точность стратегии или прибыльность на будущих играх.']}


def save(report,path):
    from .backtest import save_report
    save_report(report,path);os.chmod(path,0o600)


def metrics(report):
    done=[c for c in report['cases'] if c['state']=='completed']
    bets=[c for c in done if c['verdict']['decision']=='bet']
    settled=[c for c in bets if c.get('settlement') in ('win','loss')]
    stake=sum(c.get('paper_stake',0) for c in settled);profit=sum(c.get('paper_profit',0) for c in settled)
    return {'selected':report['selected'],'completed':len(done),'bets':len(bets),'skipped':len(done)-len(bets),
            'won':sum(c.get('settlement')=='win' for c in settled),'lost':sum(c.get('settlement')=='loss' for c in settled),
            'hit_rate_on_bets':sum(c.get('settlement')=='win' for c in settled)/len(settled) if settled else None,
            'coverage':len(bets)/len(done) if done else 0,
            'skip_engines':dict(Counter(c['verdict']['engine'] for c in done if c['verdict']['decision']=='skip')),
            'forced_data_skips':sum(c['verdict']['data_forced_skip'] for c in done),
            'model_calls':sum(c['generation']['model_calls'] for c in done),
            'paper_initial_bank':100,'paper_total_staked':stake,'paper_net_profit':profit,'paper_final_bank':100+profit,
            'paper_roi':profit/stake if stake else None}


def run_trial(db,settings,report,path,complete=None):
    if report.get('format')!='matchlab.neural-verdict-test.v1' or report.get('policy_version')!=VERSION:raise ValueError('Неизвестный формат плана.')
    if report['model']!=settings.qwen_model or report['max_stake_pct']!=settings.verdict_max_stake_pct:raise ValueError('Модель или лимит изменились. Нужен новый файл теста.')
    from .winline_backtest import results
    truth={r['key']:r for r in results(db)[0]}
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    report['state']='running';save(report,path)
    for index,case in enumerate(report['cases'],1):
        if case['state']=='completed':continue
        if 'ai_analysis_jobs' in tables and db.execute("SELECT 1 FROM ai_analysis_jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
            report['state']='paused_site_busy';report['metrics']=metrics(report);save(report,path)
            print('Пауза: сайт выполняет разбор. Повторите с --resume после завершения очереди.',flush=True)
            return report
        packet=case['input']
        if hashlib.sha256(encoded(packet).encode()).hexdigest()!=case['input_sha256']:raise ValueError('Изменён зафиксированный вход.')
        print(f"Вердикт {index}/{len(report['cases'])}: {case['event']['home']} — {case['event']['away']}",flush=True)
        verdict,generation=generate(settings,packet,complete,historical=True)
        case.update(verdict=verdict,generation=generation,state='completed')
        # Settlement is attached only AFTER this stateless model request finished.
        target=truth.get(case['result_key'])
        if target:
            h,a=target['goals'];actual='home' if h>a else 'away' if a>h else 'draw'
            case.update(actual_score=f'{h}:{a}',actual_outcome=actual,settlement='skipped')
            if verdict['decision']=='bet':
                won=verdict['selection']==actual;stake=verdict['stake_percent']
                case.update(settlement='win' if won else 'loss',paper_stake=stake,
                            paper_profit=stake*(verdict['odds']-1) if won else -stake)
        else:case['settlement']='result_unavailable'
        report['metrics']=metrics(report);save(report,path)
        print(f"  Решение: {verdict['decision']}; движок: {verdict['engine']}; вызовов: {generation['model_calls']}",flush=True)
    report['state']='completed';report['metrics']=metrics(report);save(report,path)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path);parser.add_argument('--limit',type=int,default=10)
    parser.add_argument('--seed',type=int);parser.add_argument('--resume',action='store_true');parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--output',type=Path,default=Path('data/evaluations/neural-verdict-10.json'))
    args=parser.parse_args();settings=Settings.from_env()
    path=args.output;path.parent.mkdir(parents=True,exist_ok=True)
    try:
        if not settings.qwen_configured and not args.prepare_only:raise ValueError('Qwen не настроена.')
        with path.with_suffix('.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('Этот файл теста уже обрабатывается.')
            with sqlite3.connect('file:'+uri_quote(str((args.database or settings.database).resolve()))+'?mode=ro',uri=True) as db:
                db.row_factory=sqlite3.Row
                if args.resume:
                    report=json.loads(path.read_text())
                else:
                    if path.exists():raise ValueError('Файл уже существует. Используйте --resume или другой --output.')
                    report=prepare(db,settings,args.limit,args.seed);save(report,path)
                print('Зафиксирована выборка:',report['selected'],'seed:',report['seed'],flush=True)
                print('Свежие сохранённые коэффициенты:',report['inputs_with_fresh_prices'],
                      '| входов с обязательным пропуском:',report['inputs_requiring_skip'],flush=True)
                if not args.prepare_only:
                    opts=replace(settings,qwen_timeout=min(90,settings.qwen_timeout),event_analysis_mode='neural-verdict')
                    report=run_trial(db,opts,report,path)
    except (OSError,ValueError,sqlite3.Error) as error:parser.exit(1,'Ошибка теста: '+str(error)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('cases',)},ensure_ascii=False,indent=2))
    print('JSON:',path)


if __name__=='__main__':main()
