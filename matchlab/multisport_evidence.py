"""Sport-scoped recorded results and participant-bound widget evidence."""
from collections import Counter
from datetime import datetime, timezone
import json

from .bookmakers import instant
from .verdict_sports import PROFILES, key, result_score


def archive_results(db, sport, as_of=None, participants=None, include_unresolved=False):
    p = PROFILES[sport]
    mysql=getattr(db,'dialect',None)=='mysql'
    if not mysql:db.create_function('verdict_key', 1, key)
    params = [p.archive_id, key(p.label)]
    if as_of is None:
        sql = 'SELECT result_key,payload,start_at FROM archive_latest_results WHERE sport_id=? AND verdict_key(sport_label)=?'
    else:
        at = as_of.isoformat()
        sql = '''WITH ranked AS (SELECT *,row_number() OVER
            (PARTITION BY result_key ORDER BY julianday(observed_at) DESC,julianday(received_at) DESC) n
            FROM archive_versions WHERE sport_id=? AND verdict_key(sport_label)=?
            AND julianday(observed_at)<=julianday(?) AND julianday(received_at)<=julianday(?))
            SELECT result_key,payload FROM ranked WHERE n=1'''
        params += [at, at]
    records = {}; rejected = Counter()
    if mysql:
        if as_of is None:
            candidates=db.execute('SELECT result_key,payload,start_at,sport_label FROM archive_latest_results WHERE sport_id=?',(p.archive_id,))
        else:
            candidates=db.execute('''SELECT result_key,payload,sport_label FROM archive_versions WHERE sport_id=?
                AND julianday(observed_at)<=julianday(?) AND julianday(received_at)<=julianday(?)
                ORDER BY result_key,julianday(observed_at) DESC,julianday(received_at) DESC''',(p.archive_id,at,at))
        selected=[];seen=set()
        for row in candidates:
            if key(row['sport_label'])!=key(p.label) or as_of is not None and row['result_key'] in seen:continue
            seen.add(row['result_key']);selected.append(row)
    else:selected=db.execute(sql,params)
    for raw in selected:
        try:
            value = json.loads(raw['payload']); at = instant(value['date'])
            if at >= (as_of or datetime.now(timezone.utc)):
                rejected['current_or_future'] += 1; continue
            names = value.get('member1'), value.get('member2')
            if not all(isinstance(n, str) and n.strip() for n in names) or key(names[0]) == key(names[1]):
                rejected['invalid_identity'] += 1; continue
            if participants and not set(map(key, names)) & set(map(key, participants)):
                continue
            nonstandard=any(value.get(k) for k in ('postponed','cancelled','canceled','retired','walkover','disqualified'))
            pair = None if nonstandard else result_score(value.get('score'), sport)
            if pair is None:
                rejected['unsupported_or_ambiguous_score'] += 1
                if not include_unresolved:continue
            competition = value.get('championship')
            if not isinstance(competition, str) or not competition.strip():
                rejected['invalid_identity'] += 1; continue
            identity = key(names[0]), key(names[1]), key(competition), at
            row = {'key': raw['result_key'], 'sport': sport, 'home': names[0], 'away': names[1],
                   'competition': competition, 'at': at, 'goals': pair, 'score_unit': p.unit}
            if identity in records and (records[identity] is None or records[identity]['goals'] != pair):
                records[identity] = None; rejected['conflicting_results'] += 1
            elif identity not in records:
                records[identity] = row
        except (KeyError, ValueError, TypeError, OverflowError):
            rejected['invalid_result'] += 1
    return [r for r in records.values() if r is not None], dict(rejected)


def provider_results(event, statistics, cutoff):
    from .match_history import identifier, same_team, row_time
    feed = (statistics or {}).get('structured_feed') or {}
    anchor = feed.get('match') or {}; targets = anchor.get('teams') or {}
    expected = identifier((statistics or {}).get('statistics_id'))
    if not expected or identifier(feed.get('statistics_id')) != expected or identifier(anchor.get('_id')) != expected:
        return [], 0
    p = PROFILES[event['sport']]
    # StatsHub uses Sportradar sport IDs, never the bookmaker's event ID.
    if identifier(anchor.get('_sid')) != str(p.archive_id):
        return [], 0
    records = []; excluded = 0
    for side in ('home', 'away'):
        for group in (feed.get('participants') or {}).get(side, []):
            for row in group.get('verified_matches', []):
                if not isinstance(row, dict):
                    continue
                if identifier(row.get('_id')) == expected:
                    continue
                at = row_time(row); teams = row.get('teams') or {}; values = row.get('result') or {}
                if not at or at >= cutoff or not isinstance(teams, dict) or not isinstance(values, dict):
                    continue
                if row.get('_sid') is not None and identifier(row['_sid']) != str(p.archive_id):
                    continue
                from .widget_results import finality
                evidence_status=finality(row,event['sport'])
                positions = [s for s in ('home', 'away') if same_team(teams.get(s), targets.get(side))]
                if not evidence_status or len(positions) != 1:
                    excluded += 1; continue
                if any(row.get(k) for k in ('postponed', 'cancelled', 'canceled', 'retired', 'walkover', 'disqualified')):
                    excluded += 1; continue
                h, a = values.get('home'), values.get('away')
                if type(h) is not int or type(a) is not int or min(h, a) < 0:
                    continue
                period = key(values.get('period'))
                if event['sport'] == 'hockey':
                    if period not in ('nt', 'regular', 'regular_time') or max(h, a) > 30:
                        excluded += 1; continue
                elif period not in ('', 'nt', 'full', 'match', 'final') or result_score(f'{h}:{a}', event['sport']) is None:
                    excluded += 1; continue
                names = {}
                for pos in ('home', 'away'):
                    bound = [s for s in ('home', 'away') if same_team(teams.get(pos), targets.get(s))]
                    names[pos] = event.get(bound[0]) if len(bound) == 1 else (teams.get(pos) or {}).get('name')
                if not all(names.values()) or key(names['home']) == key(names['away']):
                    continue
                competition = row.get('competition') or row.get('tournament')
                if isinstance(competition, dict):
                    competition = competition.get('name')
                if not competition and any(identifier(row.get(k)) and identifier(row.get(k)) == identifier(anchor.get(k)) for k in ('_utid', '_tid')):
                    competition = event['competition']
                records.append({**names, 'competition': competition, 'at': at, 'goals': (h, a), 'sport': event['sport'],
                                'evidence_status':evidence_status})
    return records, excluded


def collect(repo, event, statistics, as_of, add):
    p = PROFILES[event['sport']]; warnings = []
    cutoff = min(as_of, instant(event['kickoff_at'])) if event.get('kickoff_at') else as_of
    rows = []
    if repo is not None:
        with repo.connection() as db:
            rows, _ = archive_results(db, event['sport'], as_of, [event.get('home'), event.get('away')])
    provider, excluded = provider_results(event, statistics, cutoff)
    from .widget_results import REPORTED_WARNING
    if any(r.get('evidence_status')=='source_full_time' for r in provider):warnings.append(REPORTED_WARNING)
    if excluded:
        warnings.append('Часть истории виджета не использована: завершение или условия результата не подтверждены.')
    unique = {}
    for row in rows + provider:
        if row['at'] >= cutoff:
            continue
        identity = key(row['home']), key(row['away']), row['at']
        if identity in unique and (unique[identity] is None or unique[identity]['goals'] != row['goals']):
            unique[identity] = None
        elif identity not in unique:
            unique[identity] = row
    for side in ('home', 'away'):
        selected = sorted([r for r in unique.values() if r and key(event.get(side)) in (key(r['home']), key(r['away']))], key=lambda r: r['at'], reverse=True)
        if len(selected)>8:
            warnings.append('История участников сокращена до последних подтверждённых встреч.')
        remaining = 8
        for same in (True, False):
            group = [r for r in selected if (key(r.get('competition')) == key(event.get('competition'))) == same]
            kept = group[:remaining]; remaining -= len(kept)
            if not kept:
                continue
            matches = []; lines = []
            for row in kept:
                h, a = row['goals']; gf, ga = (h, a) if key(row['home']) == key(event[side]) else (a, h)
                outcome = 'win' if gf > ga else 'loss' if gf < ga else 'draw'
                words = {'win': 'победа', 'loss': 'поражение', 'draw': 'ничья'}
                matches.append({'date': str(row['at'].date()), 'score': f'{h}:{a}', 'difference': gf-ga, 'outcome': outcome})
                lines.append(f"{row['at'].date()}: {row['home']} — {row['away']} {h}:{a}; для участника {words[outcome]}.")
            label = 'Текущий турнир' if same else 'Другие турниры или турнир не подтверждён'
            reported=sum(r.get('evidence_status')=='source_full_time' for r in kept)
            provenance=(' '+REPORTED_WARNING) if reported else ''
            add('history', f"{label}, {event[side]}. Единица счёта: {p.unit}. Условия: {p.market_label}. От новых встреч к старым." +provenance+'\n' + '\n'.join(lines),
                side=side, scope='same_competition' if same else 'other_competitions', history_order='newest_first',
                score_unit=p.unit, matches=matches, outcomes=[m['outcome'] for m in matches],source_reported_results=reported)
    from .season_evidence import collect_source_season
    warnings.extend(collect_source_season(event,statistics,add))
    if (statistics or {}).get('sections_truncated') or ((statistics or {}).get('structured_feed') or {}).get('truncated'):
        warnings.append('Источник передал сокращённую статистику; полнота ограничена.')
    warnings.append('История может быть неполной; покрытие, составы и состояние участников подтверждены не для всех встреч.')
    return warnings
