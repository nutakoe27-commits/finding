"""Participant-bound, explicitly scoped facts for qualitative match comparisons."""
import re
import unicodedata

from .match_history import identifier


def name_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC',str(value or '')).casefold() if c.isalnum())


def aliases(event,statistics):
    """No guessed nicknames: provider names require this exact statistics anchor."""
    names={s:{name_key(event.get(s))} for s in ('home','away')}
    feed=(statistics or {}).get('structured_feed') or {}
    anchor=feed.get('match') or {};teams=anchor.get('teams') or {}
    expected=identifier((statistics or {}).get('statistics_id'))
    valid=bool(expected and identifier(feed.get('statistics_id'))==expected==identifier(anchor.get('_id')))
    ids=[identifier((teams.get(s) or {}).get('_id')) for s in names]
    if valid and all(ids) and ids[0]!=ids[1]:
        for s in names:
            value=name_key((teams.get(s) or {}).get('name'))
            if value:names[s].add(value)
    shared=names['home']&names['away']
    for s in names:names[s]-=shared;names[s].discard('')
    return names


def standing_rows(section):
    # The two П columns mean losses and conceded goals. Require the full known
    # football header, rather than guessing the meaning of arbitrary numbers.
    cells=[c.strip() for line in str(section.get('text') or '').splitlines() for c in line.split('\t') if c.strip()]
    keys=[name_key(c) for c in cells]
    header=['','команда','и','в','н','п','з','п','разн','очки','форма']
    start=next((i+len(header) for i in range(len(keys)-len(header)+1) if keys[i:i+len(header)]==header),None)
    if start is None:return []
    rows=[]
    for i in range(start,len(cells)-9):
        if not re.fullmatch(r'\d{1,3}',cells[i]) or not name_key(cells[i+1]):continue
        values=cells[i+2:i+10]
        if not all(re.fullmatch(r'-?\d{1,5}',v) for v in values):continue
        games,wins,draws,losses,gf,ga,diff,points=map(int,values)
        if min(games,wins,draws,losses,gf,ga)<0 or games!=wins+draws+losses or diff!=gf-ga:continue
        rows.append({'name':cells[i+1],'position':cells[i],'games':games,'wins':wins,'draws':draws,
                     'losses':losses,'for':gf,'against':ga,'difference':diff,'points':points})
    return rows


def collect(repo,event,statistics,as_of,add):
    from .chances import football_sample,norm
    from .match_history import normalize
    from .season_evidence import season_views
    context={'event':event,'team_statistics':statistics or {}}
    warnings=[];views=season_views(context)
    if (statistics or {}).get('sections_truncated') or (statistics or {}).get('truncated_sections') or ((statistics or {}).get('structured_feed') or {}).get('truncated'):
        warnings.append('Источник передал сокращённую статистику; полнота ограничена.')
    if (statistics or {}).get('season_filters_complete') is False:
        warnings.append('Не все фильтры сезонной статистики прочитаны.')
    for mode,venue in [('rate','overall'),('rate','venue'),('total','overall')]:
        for side,rate in (views.get((mode,venue)) or {}).items():
            label='Средние за матч' if mode=='rate' else 'Суммарные показатели сезона'
            where='общее' if venue=='overall' else 'хозяева / гости'
            literal=f"{label} ({where}), {event.get(side)}: забито {rate['for']:g}, пропущено {rate['against']:g}."
            metadata={}
            overall=(views.get(('rate','overall')) or {}).get(side)
            if mode=='rate' and venue=='venue' and overall:
                directions={k:'lower' if rate[k]<overall[k] else 'higher' if rate[k]>overall[k] else 'equal' for k in ('for','against')}
                words={'lower':'меньше','higher':'больше','equal':'столько же'}
                literal+=f" По сравнению с общим средним этой команды: забивает {words[directions['for']]}; пропускает {words[directions['against']]}. Сравнение относится к разным фильтрам источника."
                metadata['venue_vs_overall']=directions
            add('statistics',literal,side=side,scope=venue,**metadata)
    rows=football_sample(repo,event,as_of) if repo is not None and event.get('sport')=='football' else []
    feed=normalize(context,as_of,rows,broad=True);all_rows={}
    for row in rows+feed['rows']:
        if row['at']>=as_of:continue
        key=(row['home'],row['away'],row['at'],tuple(row['goals']))
        if key not in all_rows or row.get('same_competition'):all_rows[key]=row
    for side in ('home','away'):
        name=norm(event.get(side));selected=[r for r in all_rows.values() if name in (r['home'],r['away'])]
        selected.sort(key=lambda r:r['at'],reverse=True)
        remaining=8
        for same in (True,False):
            group=[r for r in selected if bool(r.get('same_competition'))==same]
            kept=group[:remaining];remaining-=len(kept)
            if len(kept)<len(group):warnings.append('История участников сокращена до последних подтверждённых встреч.')
            if not kept:continue
            outcomes=[]
            for row in kept:
                h,a=row['goals'];gf,ga=(h,a) if row['home']==name else (a,h)
                outcomes.append('win' if gf>ga else 'loss' if gf<ga else 'draw')
            words={'win':'победа','loss':'поражение','draw':'ничья'}
            latest='; '.join(f"{r['at'].date()} — {words[o]}" for r,o in zip(kept[:2],outcomes[:2]))
            lines=[f"{r['at'].date()}: {r['home']} — {r['away']} {r['goals'][0]}:{r['goals'][1]}; исход: {words[o]}" for r,o in zip(kept,outcomes)]
            scope='same_competition' if same else 'other_competitions'
            label='В текущем турнире' if same else 'Другие турниры или турнир не подтверждён; отдельно от формы в текущей лиге'
            literal=(label+', '+str(event.get(side))+'.\nПорядок дат: сначала новые встречи. '
                'Все исходы указаны для этой команды и только для показанной истории.\n'
                'Последние известные исходы: '+latest+'.\n'+'\n'.join(lines))
            add('history',literal,side=side,scope=scope,history_order='newest_first',outcomes=outcomes)
    names=aliases(event,statistics)
    for section in (statistics or {}).get('sections',[]):
        title=str(section.get('title') or '')
        if not title.casefold().startswith(('таблица','лучшие игроки')):continue
        if section.get('truncated'):
            warnings.append('Обрезанный источником раздел не использован.');continue
        if title.casefold().startswith('лучшие игроки'):
            warnings.append('Блок игроков не использован: принадлежность показателей конкретным командам не подтверждена.');continue
        rows=standing_rows(section);bound={}
        for side in names:
            matches=[r for r in rows if name_key(r['name']) in names[side]]
            if len(matches)==1:bound[side]=matches[0]
        if len(bound)!=2:warnings.append('Часть строк таблицы не использована: привязка к участникам не подтверждена.')
        filters=[str(f) for f in section.get('filters',[])]
        if not filters and bound:warnings.append('Фильтры таблицы не подтверждены; её показатели относятся только к сохранённому виду.')
        for side,row in bound.items():
            scope=' / '.join(filters) if filters else 'сохранённый вид; фильтры не подтверждены'
            literal=(f"Таблица ({scope}), {event[side]} [название источника: {row['name']}]: место {row['position']}, "
                f"игр {row['games']}, побед {row['wins']}, ничьих {row['draws']}, поражений {row['losses']}, "
                f"забито {row['for']}, пропущено {row['against']}, разница {row['difference']}, очков {row['points']}.")
            add('statistics',literal,side=side,scope='standings',binding='exact_or_anchored_name')
    return list(dict.fromkeys(warnings))
