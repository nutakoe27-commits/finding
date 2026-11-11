"""Descriptive evidence comparisons, not a prediction or stake-sizing model.

Only narrowly recognizable contradictions can be checked in unrestricted prose.
These checks do not establish that an entire explanation is semantically correct.
"""
import math
import re


EVIDENCE_KINDS={'statistics','history','comparison'}


def sides(fact):
    return set(fact.get('sides',[])) if fact.get('kind')=='comparison' else {fact.get('side')}


def relation(a,b):
    return 'lower' if a<b else 'higher' if a>b else 'equal'


def rates(facts):
    groups={}
    for f in facts:
        if f.get('kind')!='statistics' or f.get('measurement')!='rate' or f.get('truncated'):continue
        m=f.get('metrics') or {}
        if set(m)!={'for','against'} or not all(type(v) in (int,float) and math.isfinite(v) for v in m.values()):continue
        groups.setdefault(f.get('scope'),{}).setdefault(f.get('side'),[]).append(f)
    return {scope:{s:entries[s][0] for s in ('home','away')} for scope,entries in groups.items()
        if scope in ('overall','venue') and all(len(entries.get(s,[]))==1 for s in ('home','away'))}


def add_comparisons(facts,event,add):
    """All comparisons retain their source IDs and their exact sample/filter."""
    original=list(facts);words={'lower':'меньше','higher':'больше','equal':'столько же'}
    for scope,pair in rates(original).items():
        h,a=(pair[s]['metrics'] for s in ('home','away'))
        directions={k:relation(h[k],a[k]) for k in h}
        where='общие средние за матч' if scope=='overall' else 'средние хозяев дома / гостей в гостях'
        literal=(f"Сопоставление: {where}. {event['home']} забивает {h['for']:g}, {event['away']} — {a['for']:g}; "
            f"хозяева забивают {words[directions['for']]}. Пропущено: {event['home']} {h['against']:g}, "
            f"{event['away']} {a['against']:g}; хозяева пропускают {words[directions['against']]}. "
            'Это описание разных команд без поправки на соперников, а не оценка силы.')
        add('comparison',literal,sides=['home','away'],scope=scope,comparison_type='rates',
            source_fact_ids=[pair[s]['id'] for s in ('home','away')],home_relation=directions)
    histories={s:[f for f in original if f.get('kind')=='history' and f.get('side')==s and
        f.get('scope')=='same_competition' and not f.get('truncated') and f.get('matches')] for s in ('home','away')}
    if not all(len(histories[s])==1 for s in histories):return
    pair={s:histories[s][0] for s in histories};n=min(3,*(len(f['matches']) for f in pair.values()))
    if not n:return
    counts={s:{o:sum(m['outcome']==o for m in pair[s]['matches'][:n]) for o in ('win','draw','loss')} for s in pair}
    summaries=[f"{event[s]}: побед {counts[s]['win']}, ничьих {counts[s]['draw']}, поражений {counts[s]['loss']}" for s in pair]
    literal=(f"По {n} последним сохранённым встречам каждой команды в текущем турнире: "+'; '.join(summaries)+'. '
        'Число встреч одинаковое, даты могут различаться; полнота календаря не подтверждена. ')
    if counts['home']==counts['away']:literal+='Набор исходов одинаков: по этой сводке преимущество в недавних результатах не выделяется.'
    add('comparison',literal,sides=['home','away'],scope='same_competition',comparison_type='recent_results',
        source_fact_ids=[pair[s]['id'] for s in ('home','away')],sample_size=n,outcome_counts=counts)


def team_patterns(event):
    labels={s:{str(event.get(s) or '').casefold()} for s in ('home','away')}
    for s in labels:
        labels[s]|={re.sub(r'\s*\([^)]*\)','',v).strip() for v in labels[s]}
        # This identifies subjects in prose about these two participants only;
        # it is never used to bind a provider's statistics to a team. Accept an
        # unambiguous leading word (e.g. "Дерби" in "Дерби Каунти").
        labels[s]|={v.split()[0] for v in labels[s] if v.split() and
            re.fullmatch(r'[^\W\d_]{4,}',v.split()[0])}
    shared=labels['home']&labels['away']
    for s in labels:labels[s]-=shared;labels[s].discard('')
    labels['home']|={'хозяева','хозяев','хозяевами'};labels['away']|={'гости','гостей','гостями'}
    return {s:re.compile(r'(?<!\w)(?:'+'|'.join(re.escape(v) for v in sorted(labels[s],key=len,reverse=True))+r')(?!\w)',re.I) for s in labels}


def mentioned_sides(wording,patterns):
    return [(m.start(),s) for s,p in patterns.items() for m in p.finditer(wording)]


def negated_claim(clause,start,end):
    # Do not turn an explicit denial into a positive claim. Ambiguous negation
    # is left to the reader instead of triggering another expensive model call.
    before=re.split(r'\b(?:но|однако|зато)\b',clause[max(0,start-70):start],flags=re.I)[-1]
    return bool(re.search(r'\b(?:не|нет|нельзя|невозможно|без)\b',before,re.I) or
        re.match(r'\s+(?:не\s+было|нет)\b',clause[end:],re.I))


def semantic_issues(wording,field,packet,ids,preference):
    """Reject precise, demonstrable contradictions; abstain on ambiguous prose."""
    issues=[];bank={f['id']:f for f in packet['facts']};patterns=team_patterns(packet['event'])
    cited=[bank[i] for i in ids];expanded=list(cited)
    for f in cited:
        expanded.extend(bank[i] for i in f.get('source_fact_ids',[]) if i in bank)
    def issue(rule,**detail):issues.append({'code':'verdict_evidence_conflict','field':field,'rule':rule,**detail})
    # This grammar deliberately requires an identifiable subject AND an explicit
    # opponent. Venue/overall comparisons and vague judgements are not guessed.
    comparison=re.compile(r'(?P<metric>забива\w*|пропуска\w*)\s+(?P<rel>больше|меньше|столько\s+же)'
        r'(?P<value>\s*\(\s*\d+(?:[.,]\d+)?\s*\))?\s*,?\s*чем\s+(?P<other>[^.!?;\n]{1,100})',re.I)
    groups=rates(packet['facts'])
    clauses=re.split(r'[!?;\n]|(?<!\d)\.(?!\d)',wording)
    for clause in clauses:
        for m in comparison.finditer(clause):
            if negated_claim(clause,m.start(),m.end()):continue
            before=mentioned_sides(clause[:m.start()],patterns)
            subject=max(before)[1] if before else field.split('.')[-1] if field in ('comparison.home','comparison.away') else None
            targets=mentioned_sides(m['other'],patterns)
            other=min(targets)[1] if targets else None
            if not subject or not other or subject==other:continue
            metric='for' if m['metric'].casefold().startswith('забива') else 'against'
            expected={'меньше':'lower','больше':'higher','столько же':'equal'}[re.sub(r'\s+',' ',m['rel'].casefold())]
            explicit=float(m['value'].strip().strip('()').strip().replace(',','.')) if m['value'] else None
            cited_scopes={f['scope'] for f in expanded if f.get('measurement')=='rate' and f.get('side')==subject}
            scopes={'venue'} if re.search(r'дома|в\s+гостях|домашн|гостев',clause,re.I) else {'overall'} if re.search(r'в\s+среднем|в\s+целом|общ\w*\s+средн',clause,re.I) else cited_scopes or set(groups)
            comparisons=[]
            for scope,pair in groups.items():
                if scope not in scopes:continue
                a,b=(pair[s]['metrics'][metric] for s in (subject,other))
                if explicit is not None and not math.isclose(a,explicit,abs_tol=1e-8):continue
                comparisons.append((relation(a,b),pair,a,b,scope))
            if comparisons and all(actual!=expected for actual,*_ in comparisons):
                actual,pair,a,b,scope=comparisons[0]
                issue('comparison_direction',subject=subject,other=other,metric=metric,scope=scope,
                    actual_relation=actual,claimed_relation=expected,values={subject:a,other:b},
                    source_fact_ids=[pair[s]['id'] for s in ('home','away')])
        large=re.search(r'крупн\w*\s+(?:поражен\w*|проигр\w*)',clause,re.I)
        if packet['event'].get('sport')=='football' and large and not negated_claim(clause,large.start(),large.end()):
            # A nearby victory's score must not be attributed to this defeat.
            score=re.match(r'\s*(?:со\s+сч[её]том\s*)?[«"(]?\s*(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)',clause[large.end():],re.I)
            named=mentioned_sides(clause[:large.start()],patterns)
            subject=max(named)[1] if named else None
            losses=[m for f in expanded if subject is None or f.get('side')==subject for m in f.get('matches',[]) if m.get('outcome')=='loss']
            if (score and abs(int(score[1])-int(score[2]))<3) or (losses and all(abs(m['difference'])<3 for m in losses)):
                issue('large_defeat_requires_three_goal_margin',source_fact_ids=[f['id'] for f in expanded if f.get('matches')])
    # When counts are equal, they cannot by themselves establish strictly better
    # recent results. Other concrete factors can still support a preference.
    form_claim=re.search(r'(?:лучш\w*|худш\w*)\s+(?:недавн\w*\s+|текущ\w*\s+)?форм\w*|форм\w*\s+(?:лучше|хуже)',wording,re.I)
    if form_claim and not negated_claim(wording,form_claim.start(),form_claim.end()):
        for f in packet['facts']:
            if (f.get('comparison_type')=='recent_results' and f.get('outcome_counts',{}).get('home')==f.get('outcome_counts',{}).get('away') and
                set(f.get('source_fact_ids',[]))<=set(x['id'] for x in expanded)):
                issue('equal_recent_outcomes_do_not_establish_better_form',comparison_fact_id=f['id'],sample_size=f['sample_size'])
                break
    if field=='reason' and preference and re.search(r'(?:не\s+позволяет|нельзя|невозможно)\s+выбрать\s+сторону(?=\s*(?:[.!?;]|$))',wording,re.I):
        issue('chosen_preference_is_not_absence_of_preference',preference=preference)
    if packet.get('policy',{}).get('draw_possible') is False and re.search(r'риск\w*\s+ничь|возможн\w*\s+ничь|ничья\s+возможна',wording,re.I):
        issue('draw_not_in_selected_market')
    if packet['event'].get('sport') in ('tennis','basketball','table_tennis','volleyball') and re.search(r'(?<!\w)(?:гол(?:а|ов|ы|ами)?|шайб\w*)(?!\w)',wording,re.I):
        issue('score_unit_does_not_match_sport')
    if field=='market_context':
        for clause in clauses:
            cause=re.search(r'(?:коэффициент\w*|лини[яию]).{0,80}\b(?:учитыва\w*|обусловлен\w*|объясняется)',clause,re.I)
            if cause and not negated_claim(clause,cause.start(),cause.end()):
                issue('bookmaker_pricing_causes_are_not_observed');break
    return issues
