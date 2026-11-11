"""Recognize source-reported full-time scores; never settle bets from them.

Some StatsHub history rows have status=null. Their explicit ft score can be
used as reported evidence only when the winner and sport-specific breakdown
agree. This is distinct from an independently confirmed final status.
"""
import re

from .verdict_sports import key,result_score

FINAL={'100','finished','ended','closed','ft'}
NONSTANDARD=('postponed','cancelled','canceled','retired','walkover','disqualified','tobeannounced')
REPORTED_WARNING='Часть истории использована как итоговые счета источника без отдельного финального статуса; согласованность счёта проверена, независимое подтверждение завершения отсутствует.'


def status(row):
    value=row.get('status')
    if isinstance(value,dict):value=next((value[k] for k in ('_id','id','name') if value.get(k) not in (None,'')),None)
    return key(value) if value is not None else ''


def pair(value):
    if not isinstance(value,dict):return None
    values=value.get('home'),value.get('away')
    return values if all(type(v) is int and 0<=v<=1000 for v in values) else None


def reported_full_time(row,sport):
    if status(row) or any(row.get(k) for k in NONSTANDARD):return None
    result=row.get('result');periods=row.get('periods')
    if not isinstance(result,dict) or not isinstance(periods,dict):return None
    total=pair(result)
    if total is None or pair(periods.get('ft'))!=total:return None
    winner='home' if total[0]>total[1] else 'away' if total[1]>total[0] else 'draw'
    if key(result.get('winner'))!=winner:return None
    if key(result.get('period')) not in ('nt','regular','regular_time','full','match','final'):return None
    # Overtime has a different source schema. Do not infer its score or scope.
    if row.get('overtimeperiods'):return None
    found={int(k[1:]):pair(v) for k,v in periods.items() if re.fullmatch(r'p[1-9][0-9]*',str(k))}
    if any(v is None for v in found.values()):return None
    if sport in ('basketball','hockey'):
        n=row.get('numberofperiods')
        allowed=(2,4) if sport=='basketball' else (3,)
        if type(n) is not int or n not in allowed or set(found)!=set(range(1,n+1)):return None
        if tuple(sum(v[i] for v in found.values()) for i in (0,1))!=total:return None
        if sport=='basketball' and result_score(f'{total[0]}:{total[1]}',sport) is None:return None
        if sport=='hockey' and max(total)>30:return None
    elif sport=='football':
        if row.get('numberofperiods')!=2 or max(total)>30 or set(found) not in ({1},{1,2}):return None
        if any(v>total[i] for i,v in enumerate(found[1])):return None
        if 2 in found and tuple(found[1][i]+found[2][i] for i in (0,1))!=total:return None
    elif sport in ('tennis','volleyball','table_tennis'):
        bestof=row.get('bestof')
        allowed={'tennis':(3,5),'volleyball':(5,),'table_tennis':(3,5,7)}[sport]
        if type(bestof) is not int or bestof not in allowed or max(total)!=bestof//2+1:return None
        if set(found)!=set(range(1,sum(total)+1)):return None
        detail=' '.join(f'{found[i][0]}:{found[i][1]}' for i in sorted(found))
        if result_score(f'{total[0]}:{total[1]} ({detail})',sport)!=total:return None
    else:return None
    return total


def finality(row,sport):
    if any(row.get(k) for k in NONSTANDARD):return None
    if status(row) in FINAL:return 'explicit_status'
    if reported_full_time(row,sport) is not None:return 'source_full_time'
    return None
