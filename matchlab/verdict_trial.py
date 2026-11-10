"""Ten frozen random prematch inputs, blind Qwen decisions and paper settlement."""
import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime,timezone
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import secrets
import sqlite3
import tempfile
from urllib.parse import quote as uri_quote

from .bookmakers import instant
from .config import Settings
from .neural_verdict import VERSION,build_packet,encoded,generate,latest_historical_quote,quote_matches_event


def input_candidates(db):
    from .winline_backtest import results
    from .snapshot_evaluation import recorded_inputs
    rows,excluded=results(db)
    candidates,rejected,pending,inventory=recorded_inputs(db,rows)
    return candidates,{'eligible_statistics':len(candidates),'excluded_statistics':rejected,
        'excluded_results':excluded,'pending_associations':pending,'snapshot_inventory':inventory}


def paired_observation(event,statistics,available_at,quotes):
    """Earliest jointly available input; never rewind a later price or use a live one."""
    matching=[q for q in quotes if quote_matches_event(q,event)]
    if not matching:return None,None,'no_matching_historical_prices'
    kickoff=instant(event['kickoff_at']);times=set();invalid_capture=False
    for row in matching:
        try:
            at=max(available_at,instant(row['observed_at']),instant(row.get('_capture_at') or row['observed_at']))
            if at<min(kickoff,instant(row['kickoff_at'])):times.add(at)
        except (ValueError,TypeError):invalid_capture=True
    if not times:return None,None,'invalid_quote_capture' if invalid_capture else 'prices_not_available_before_kickoff'
    reason='no_joint_fresh_observation'
    for at in sorted(times):
        if (at-instant(statistics['observed_at'])).total_seconds()>3600:
            reason='statistics_expired_before_prices';continue
        quote=latest_historical_quote(event,at,matching)
        if quote and at<instant(quote['kickoff_at']) and 0<=(at-instant(quote['observed_at'])).total_seconds()<=180:return at,quote,None
    return None,None,reason


def prepare(db,settings,limit=10,seed=None,exclude_reports=(),event_ids=()):
    from .model_comparison import historical_quotes
    from .snapshot_evaluation import ReadOnlyInputs
    if not 1<=limit<=100:raise ValueError('Размер теста должен быть от 1 до 100.')
    seed=secrets.randbits(32) if seed is None else seed
    candidates,inventory=input_candidates(db)
    excluded_keys=set()
    for path in exclude_reports:
        old=json.loads(Path(path).read_text())
        if old.get('format')!='matchlab.neural-verdict-test.v1' or not isinstance(old.get('cases'),list):raise ValueError('Неподдерживаемый отчёт исключения.')
        excluded_keys.update(str(c['result_key']) for c in old['cases'] if c.get('result_key') is not None)
    quotes=historical_quotes(db);eligible=[];excluded=Counter()
    for candidate in candidates:
        target=candidate['result'];stats=deepcopy(candidate['statistics'])
        if event_ids and str(candidate['event_id']) not in event_ids:excluded['outside_debug_event_filter']+=1;continue
        if str(target['key']) in excluded_keys:excluded['previously_reviewed_fixture']+=1;continue
        event={k:target[k] for k in ('home','away','competition')}
        event.update(source_event_id=candidate['event_id'],sport='football',sport_label='Футбол',phase='prematch',kickoff_at=target['at'].isoformat())
        at,market,reason=paired_observation(event,stats,candidate['as_of'],quotes)
        if reason:excluded[reason]+=1;continue
        stats['age_seconds']=max(0,int((at-instant(stats['observed_at'])).total_seconds()));stats['is_stale']=stats['age_seconds']>3600
        packet=build_packet(ReadOnlyInputs(db),event,stats,at,market)
        if packet['policy']['force_skip']:excluded['other_data_constraints']+=1;continue
        eligible.append({'result_key':target['key'],'event':event,'input':packet,
            'input_sha256':hashlib.sha256(encoded(packet).encode()).hexdigest(),'state':'pending'})
    cases=random.Random(seed).sample(eligible,min(limit,len(eligible)))
    inventory.update(historical_price_snapshots=len(quotes),eligible_verdict_inputs=len(eligible),excluded_joint_inputs=dict(excluded))
    return {'format':'matchlab.neural-verdict-test.v1','policy_version':VERSION,'scope':'team_winner_90m',
        'selection_policy':'targeted-debug-input-v1' if event_ids else 'random-with-joint-prematch-input-v2',
        'diagnostic_only':bool(event_ids),'event_filter':list(event_ids),
        'created_at':datetime.now(timezone.utc).isoformat(),'state':'prepared' if cases else 'no_eligible_inputs','seed':seed,'requested':limit,
        'selected':len(cases),'model':settings.qwen_model,'max_stake_pct':settings.verdict_max_stake_pct,
        'excluded_reviewed_fixtures':len(excluded_keys),
        'inputs_with_fresh_prices':sum(bool(c['input']['options']) for c in cases),
        'inputs_requiring_skip':sum(c['input']['policy']['force_skip'] for c in cases),
        'source_inventory':inventory,'cases':cases,'real_money_used':False,
        'limitations':['Случайная выборка фиксируется до вызовов ИИ. Пропуски не считаются угаданными матчами.',
            'Это тест на записанном предматчевом входе, а не фактически выданные тогда рекомендации.',
            'Сначала проверяется совместная доступность статистики и коэффициентов до начала; затем случайно выбираются пригодные входы. Итог целевого матча ИИ не получает.',
            'Выборка ограничена наличием записанных данных; её результаты не переносятся на весь каталог.',
            'Размер ставки — фиксированный ограничитель для виртуального теста, не оценка вероятности и не обещание безопасности.',
            'Десять матчей не подтверждают точность стратегии или прибыльность на будущих играх.']}


def save(report,path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    candidate=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,mode='w',encoding='utf-8',delete=False) as stream:
            candidate=Path(stream.name);os.fchmod(stream.fileno(),0o600)
            json.dump(report,stream,ensure_ascii=False,indent=2,allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        candidate.replace(path)
    finally:
        if candidate is not None:candidate.unlink(missing_ok=True)


def metrics(report):
    done=[c for c in report['cases'] if c['state']=='completed']
    bets=[c for c in done if c['verdict']['decision']=='bet']
    settled=[c for c in bets if c.get('settlement') in ('win','loss')]
    stake=sum(c.get('paper_stake',0) for c in settled);profit=sum(c.get('paper_profit',0) for c in settled)
    preferences=[c for c in done if c['verdict'].get('preference') in ('home','away') and
                 (c['verdict']['engine']=='qwen' or (c['verdict']['engine']=='timing_guard' and c['generation'].get('reason')=='quote_expired'))
                 and c.get('actual_outcome') in ('home','away','draw')]
    preference_hits=sum(c['verdict']['preference']==c['actual_outcome'] for c in preferences)
    return {'selected':report['selected'],'completed':len(done),'bets':len(bets),'skipped':len(done)-len(bets),
            'won':sum(c.get('settlement')=='win' for c in settled),'lost':sum(c.get('settlement')=='loss' for c in settled),
            'hit_rate_on_bets':sum(c.get('settlement')=='win' for c in settled)/len(settled) if settled else None,
            'coverage':len(bets)/len(done) if done else 0,
            'skip_engines':dict(Counter(c['verdict']['engine'] for c in done if c['verdict']['decision']=='skip')),
            'forced_data_skips':sum(c['verdict']['data_forced_skip'] for c in done),
            'model_calls':sum(c['generation']['model_calls'] for c in done),
            'preference_evaluated':len(preferences),'preference_hits':preference_hits,
            'preference_accuracy':preference_hits/len(preferences) if preferences else None,
            'preference_coverage':len(preferences)/len(done) if done else 0,
            'preference_without_bet':sum(c['verdict']['decision']=='skip' for c in preferences),
            'paper_initial_bank':100,'paper_total_staked':stake,'paper_net_profit':profit,'paper_final_bank':100+profit,
            'paper_roi':profit/stake if stake else None}


def run_trial(db,settings,report,path,complete=None):
    if report.get('format')!='matchlab.neural-verdict-test.v1' or report.get('policy_version')!=VERSION:raise ValueError('План использует другую версию протокола. Сохраните его и подготовьте новый --output.')
    if report['model']!=settings.qwen_model or report['max_stake_pct']!=settings.verdict_max_stake_pct:raise ValueError('Модель или лимит изменились. Нужен новый файл теста.')
    from .winline_backtest import results
    truth={r['key']:r for r in results(db)[0]}
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not report['cases']:
        report['state']='no_eligible_inputs';report['metrics']=metrics(report);save(report,path)
        return report
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
        verdict,generation=generate(settings,packet,complete,historical=True,capture_rejected=True)
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
        print(f"  Предпочтение: {verdict.get('preference_label') or 'нет'}; решение: {verdict['decision']}; движок: {verdict['engine']}; вызовов: {generation['model_calls']}",flush=True)
        if generation.get('reason'):print('  Причина:',generation['reason'],flush=True)
        for attempt in generation.get('attempts',[]):
            detail=attempt.get('validation_details') or {}
            if detail:print('  Проверка:',detail.get('field','response'),'|',detail.get('rule',''),flush=True)
    report['state']='completed';report['metrics']=metrics(report);save(report,path)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path);parser.add_argument('--limit',type=int,default=10)
    parser.add_argument('--seed',type=int);parser.add_argument('--resume',action='store_true');parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--exclude-report',type=Path,action='append',default=[],help='Исключить уже просмотренные матчи; можно указать несколько раз.')
    parser.add_argument('--event-id',action='append',default=[],help='Отладка конкретного события, без заявления о случайной выборке.')
    parser.add_argument('--output',type=Path,default=Path('data/evaluations/neural-verdict-10-v6-2.json'))
    args=parser.parse_args();settings=Settings.from_env()
    path=args.output;path.parent.mkdir(parents=True,exist_ok=True)
    try:
        if args.resume and (args.exclude_report or args.event_id):raise ValueError('Фильтры применяются только при подготовке нового файла, без --resume.')
        if any(not i.isdecimal() or len(i)>20 for i in args.event_id):raise ValueError('Нужен цифровой --event-id Winline.')
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
                    report=prepare(db,settings,args.limit,args.seed,args.exclude_report,args.event_id);save(report,path)
                print('Зафиксирована выборка:',report['selected'],'seed:',report['seed'],flush=True)
                if report.get('diagnostic_only'):print('Отладка конкретного события. Этот прогон не является независимой проверкой стратегии.',flush=True)
                print('Свежие сохранённые коэффициенты:',report['inputs_with_fresh_prices'],
                      '| входов с обязательным пропуском:',report['inputs_requiring_skip'],flush=True)
                print('Пригодных совместных входов во всей базе:',report.get('source_inventory',{}).get('eligible_verdict_inputs','не проверено'),flush=True)
                if not report['selected']:print('Нет пригодных входов. Запросы к Qwen не выполняются; причины сохранены в source_inventory.excluded_joint_inputs.',flush=True)
                elif report['selected']<report['requested']:print('Для полного теста данных пока недостаточно. Будет обработано:',report['selected'],flush=True)
                if not args.prepare_only:
                    opts=replace(settings,qwen_timeout=min(90,settings.qwen_timeout),event_analysis_mode='neural-verdict')
                    report=run_trial(db,opts,report,path)
    except (OSError,ValueError,sqlite3.Error) as error:parser.exit(1,'Ошибка теста: '+str(error)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('cases',)},ensure_ascii=False,indent=2))
    print('JSON:',path)


if __name__=='__main__':main()
