"""Read-only, versioned preparation for a shared participant-win model."""

from collections import Counter
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
from zoneinfo import ZoneInfo

from .monitor_data import readonly
from .profiles import DEFAULT_SPORTS, PROFILES, selection
from .store import check_observation_times, digest, encoded, instant, now, sample_blocker, widget_blocker
from .training_evidence import frozen_format_documents, participant_key, schedule_revision


# Native ResultService identities verified from the user's five raw examples.
# These are NOT the provider's _sid identifiers.
NATIVE_SPORTS = {1:'football', 2:'basketball', 4:'hockey', 5:'tennis', 23:'volleyball'}
CATEGORICAL = ('sport', 'competition', 'selection_side', 'score_units', 'market_scope', 'capture_state')
NUMERIC = ('lead_minutes', 'market_count', 'selected_odds', 'opponent_odds', 'draw_odds',
           'implied_selected', 'implied_opponent', 'implied_draw', 'overround',
           'filter_coverage', 'section_count', 'document_count',
           'selected_form_count', 'selected_form_win_rate', 'opponent_form_count', 'opponent_form_win_rate',
           'selected_season_played', 'selected_season_win_rate', 'opponent_season_played', 'opponent_season_win_rate',
           'selected_goals_for_per_match', 'selected_goals_against_per_match',
           'opponent_goals_for_per_match', 'opponent_goals_against_per_match')
SCHEMA = {'format':'doscbot.training-schema.v1', 'target':'participant_win.v1',
          'categorical':list(CATEGORICAL), 'numeric':list(NUMERIC),
          'missing_indicators':[key+'__missing' for key in NUMERIC],
          'target_definition':'Selected participant wins the explicitly scoped market; a valid 3-way draw labels both participants 0',
          'label_source':'Linked ResultService score with validated complete normal-period structure; not bookmaker settlement',
          'selection_policy':'Latest valid-horizon sample per event; two participant rows remain one group',
          'unsupported_labels':'No guessing of overtime, retirements, missing best-of, ambiguous markets or score syntax',
          'feature_policy':'Allowlisted pre-match fields only; missing numeric values are null, never imputed to zero'}


def number(value):
    return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None


def ratio(top, bottom):
    top,bottom=number(top),number(bottom)
    return top/bottom if top is not None and bottom is not None and bottom>0 else None


def winner_market(event):
    candidates=[]
    for market in event.get('markets',[]):
        name=' '.join(str(market.get('name') or '').split())
        if market.get('parameter') or not re.fullmatch(r'(?:Исход(?:\s+(?:1[XХ]2|12))?|Победитель(?:\s+матча)?|Победа(?:\s+в\s+матче)?|Победит)',name,re.I):
            continue
        period=re.sub(r'[.\s]','',str(market.get('period') or '')).casefold()
        scope='regulation' if period in ('оснвремя','основноевремя') else 'match' if period in ('матч','весьматч') else None
        if not period and name.casefold()=='победитель матча':
            scope='match'
        if not scope:
            continue
        prices={}; invalid=False
        aliases={'п1':'home','1':'home','п2':'away','2':'away','x':'draw','х':'draw'}
        for item in market.get('outcomes',[]):
            side=aliases.get(str(item.get('name') or '').strip().casefold())
            price=number(item.get('odds'))
            if side is None or side in prices or item.get('state')!='open' or price is None or price<=1:
                invalid=True;break
            prices[side]=price
        if not invalid and set(prices) in ({'home','away'}, {'home','draw','away'}):
            candidates.append({'key':market.get('key'),'name':name,'period':market.get('period'),
                               'scope':scope,'prices':prices})
    if not candidates:
        return None,'winner_market_missing'
    # A regulation contract is a reproducible first target when both exist.
    rank=min(0 if item['scope']=='regulation' else 1 for item in candidates)
    candidates=[item for item in candidates if (0 if item['scope']=='regulation' else 1)==rank]
    unique={encoded(item):item for item in candidates}
    return (next(iter(unique.values())),None) if len(unique)==1 else (None,'winner_market_ambiguous')


def latest_value(items, extract, cutoff):
    values=[]
    for item in items:
        try:
            at=instant(item['source']['observed_at'])
            if at<=cutoff:
                values.append((at,extract(item)))
        except (KeyError,TypeError,ValueError):
            continue
    if not values:
        return None
    latest=max(at for at,_ in values)
    unique={encoded(value):value for at,value in values if at==latest}
    return next(iter(unique.values())) if len(unique)==1 else None


def side_statistics(widget, side, cutoff):
    target=widget.get('match') or {}; identifier=str((target.get(side) or {}).get('id') or '')
    output={'form_count':None,'form_win_rate':None,'season_played':None,'season_win_rate':None,
            'goals_for_per_match':None,'goals_against_per_match':None}
    if not identifier:
        return output
    form=latest_value([item for item in widget.get('form',[]) if str(item.get('team',{}).get('id'))==identifier],
                      lambda item:item.get('form'),cutoff)
    form=form.get('total') if isinstance(form,dict) else form
    if isinstance(form,list):
        codes=[item.get('typeid') for item in form if isinstance(item,dict) and
               item.get('matchid') is not None and str(item.get('matchid'))!=str(widget.get('statistics_id')) and item.get('typeid') in ('W','D','L')]
        if codes:
            output.update(form_count=len(codes),form_win_rate=codes.count('W')/len(codes))
    season=str(target.get('season_id') or '')
    rows=[item for item in widget.get('season_statistics',[]) if season and str(item.get('season_id'))==season and
          str(item.get('team',{}).get('id'))==identifier and item.get('window',{}).get('kind')=='season']
    total=latest_value(rows,lambda item:item.get('scopes',{}).get('total'),cutoff)
    if isinstance(total,dict):
        played=number(total.get('matches'));wins=number(total.get('wins'))
        output['season_played']=played if played is not None and played>=0 else None
        if played is not None and wins is not None and 0<=wins<=played:
            output['season_win_rate']=ratio(wins,played)
        if widget.get('sport') in ('football','hockey'):
            for field in ('goals_for','goals_against'):
                explicit=number(total.get(field+'_per_match'))
                value=explicit if explicit is not None else ratio(total.get(field),played)
                output[field+'_per_match']=value if value is not None and value>=0 else None
    return output


def best_of(widget):
    target=widget.get('match') or {}
    status=target.get('status')
    status=status.get('_id') if isinstance(status,dict) else status
    planned=type(status) is int and status==0 and all((target.get('result') or {}).get(side) is None for side in ('home','away'))
    values=set()
    def add(value):
        if value is not None:
            values.add(value if isinstance(value,(int,float)) and not isinstance(value,bool) else '__invalid__')
    add(target.get('best_of'))
    if planned:add(target.get('number_of_periods'))
    for document in widget.get('documents',[]):
        if document.get('name') not in ('match_info_statshub','stats_match_get'):continue
        prefix='match.' if document['name']=='match_info_statshub' else ''
        fields={item.get('path'):item.get('value') for item in document.get('fields',[])}
        if str(fields.get(prefix+'_id'))!=str(widget.get('statistics_id')):continue
        status=fields.get(prefix+'status._id',fields.get(prefix+'status'))
        if (isinstance(status,bool) or status not in (None,0,'0') or any(fields.get(prefix+'result.'+key) is not None for key in ('home','away','winner')) or
                any(fields.get(prefix+key) is True for key in ('retired','walkover','disqualified','canceled','postponed'))):
            add('__source_not_prematch__');continue
        add(fields.get(prefix+'bestof'))
        # StatsHub also declares the planned set count here. Use it only from
        # a confirmed not-started, scoreless target; played periods are not a
        # substitute for the scheduled match format.
        if planned:add(fields.get(prefix+'numberofperiods'))
    value=next(iter(values)) if len(values)==1 else None
    return int(value) if value in (3,5) else None


def score_winner(sport, score, widget):
    """Conservative normal-period adapter; annotations and extras stay unlabelled."""
    parsed=re.fullmatch(r'\s*(\d{1,3}):(\d{1,3})\s*\((\d{1,3}:\d{1,3}(?:\s+\d{1,3}:\d{1,3})*)\)\s*',str(score or ''))
    if not parsed:
        return None,'score_format_unverified'
    main=tuple(map(int,parsed.group(1,2)))
    periods=[tuple(map(int,item.split(':'))) for item in parsed.group(3).split()]
    if sport in ('football','hockey','basketball'):
        expected={'football':2,'hockey':3,'basketball':4}[sport]
        if len(periods)!=expected:
            return None,'normal_period_count_unverified'
        if tuple(map(sum,zip(*periods)))!=main:
            return None,'period_total_mismatch'
    else:
        total=best_of(widget)
        if total is None:
            return None,'best_of_unconfirmed'
        if len(periods)!=sum(main) or any(a==b for a,b in periods):
            return None,'set_total_mismatch'
        wins=(sum(a>b for a,b in periods),sum(b>a for a,b in periods))
        if wins!=main or max(main)!=(total//2+1) or min(main)>=max(main):
            return None,'incomplete_set_match'
        progress=[0,0]
        for a,b in periods[:-1]:
            progress[0 if a>b else 1]+=1
            if max(progress)>total//2:
                return None,'set_after_match_clinched'
        if sport=='volleyball':
            for index,(a,b) in enumerate(periods):
                goal=15 if index==total-1 else 25
                if max(a,b)<goal or abs(a-b)<2 or (max(a,b)>goal and abs(a-b)!=2):
                    return None,'incomplete_set_score'
        else:
            for a,b in periods:
                high,low=max(a,b),min(a,b)
                if not (high==6 and low<=4 or high==7 and low in (5,6) or high>=8 and low>=6 and high-low==2):
                    return None,'tennis_set_format_unverified'
    return 'home' if main[0]>main[1] else 'away' if main[1]>main[0] else 'draw',None


def linked_label(db, event, widget, market, cutoff, *, as_of):
    result=db.execute("SELECT id,received_at,sha256,payload FROM observations WHERE kind='event_outcome' AND entity_id=? "
                      "AND julianday(received_at)<=julianday(?) ORDER BY julianday(received_at) DESC,id DESC LIMIT 1",
                      (event['source_event_id'],cutoff.isoformat())).fetchone()
    if not result:
        return None,'result_not_available_at_cutoff',None
    value=json.loads(result['payload'])
    context={'observation_id':result['id'],'received_at':result['received_at'],'score_text':value.get('score_text'),
             'result_key':value.get('result_key'),'source_result_sha256':value.get('source_result_sha256')}
    if digest(value)!=result['sha256']:
        return None,'outcome_integrity_mismatch',context
    try:
        if (value.get('format')!='doscbot.event-outcome.v1' or str(value.get('event_id'))!=event['source_event_id'] or
            isinstance(value.get('sport_id'),bool) or NATIVE_SPORTS.get(value.get('sport_id'))!=event['sport'] or
            [participant_key(value.get(side)) for side in ('home','away')]!=[participant_key(event.get(side)) for side in ('home','away')] or
            instant(result['received_at'])<instant(event['kickoff_at']) or
            instant(result['received_at'])<instant(value['kickoff_at']) or
            value.get('method') not in ('sport_radar_names_time','sport_names_time_unique')):
            return None,'outcome_identity_unconfirmed',context
        if abs((instant(value['kickoff_at'])-instant(event['kickoff_at'])).total_seconds())>900:
            revision=schedule_revision(db,event,widget,value,as_of,result['received_at'])
            if not revision:return None,'outcome_identity_unconfirmed',context
            context['identity_evidence']=revision
    except (KeyError,TypeError,ValueError):
        return None,'outcome_identity_unconfirmed',context
    if not market:
        return None,'winner_market_unconfirmed',context
    if ((event['sport'] in ('tennis','volleyball') and market['scope']!='match') or
            (event['sport'] in ('football','hockey') and market['scope']=='match' and 'draw' in market['prices'])):
        return None,'market_scope_unverified',context
    outcome,reason=score_winner(event['sport'],value.get('score_text'),widget)
    if event['sport'] in ('tennis','volleyball') and reason in (None,'best_of_unconfirmed'):
        documents,provenance,error=frozen_format_documents(db,event,widget,as_of)
        if error:return None,error,context
        if documents:
            label_widget={**widget,'documents':widget.get('documents',[])+documents}
            outcome,reason=score_winner(event['sport'],value.get('score_text'),label_widget)
            context['format_evidence']=provenance
    if reason:
        return None,reason,context
    if outcome=='draw' and 'draw' not in market['prices']:
        return None,'draw_in_two_way_market',context
    return outcome,None,context


def participant_rows(payload, market):
    event,widget=payload['event'],payload['widget_data'];cutoff=instant(payload['as_of'])
    stats={side:side_statistics(widget,side,cutoff) for side in ('home','away')}
    groups=(widget.get('views') or {}).get('groups',{})
    expected=sum(item.get('expected',0) for item in groups.values())
    captured=sum(item.get('captured',0) for item in groups.values())
    prices=market['prices'] if market else {}; implied={key:1/value for key,value in prices.items()}
    total=sum(implied.values())
    for side,other in (('home','away'),('away','home')):
        features={key:None for key in NUMERIC}
        features.update(sport=event['sport'],competition=event.get('competition') or '__missing__',selection_side=side,
                        score_units='|'.join(PROFILES[event['sport']]['score_units']),
                        market_scope=market['scope'] if market else '__missing__',
                        capture_state=(widget.get('views') or {}).get('state','__missing__'),
                        lead_minutes=(instant(event['kickoff_at'])-cutoff).total_seconds()/60,
                        market_count=len(event.get('markets',[])),selected_odds=prices.get(side),opponent_odds=prices.get(other),
                        draw_odds=prices.get('draw'),implied_selected=implied.get(side)/total if total else None,
                        implied_opponent=implied.get(other)/total if total else None,
                        implied_draw=implied.get('draw')/total if total and 'draw' in implied else None,
                        overround=total-1 if total else None,filter_coverage=ratio(captured,expected),
                        section_count=number(widget.get('coverage',{}).get('sections')),
                        document_count=number(widget.get('coverage',{}).get('documents')))
        for prefix,which in (('selected',side),('opponent',other)):
            features.update({prefix+'_'+key:value for key,value in stats[which].items()})
        features.update({key+'__missing':int(features[key] is None) for key in NUMERIC})
        yield side,features


def prepare_training(state, finding, *, output=None, lead_minutes=60, sports=DEFAULT_SPORTS,valid_from=None,test_from=None,
                     diagnostic_examples=0):
    state,finding=Path(state).resolve(),Path(finding).resolve()
    if state==finding or finding in state.parents or state in finding.parents:
        raise ValueError('Training preparation only uses the new collector state outside finding')
    if not 0<=lead_minutes<=10080:
        raise ValueError('lead-minutes must be between 0 and 10080')
    if type(diagnostic_examples) is not int or not 0<=diagnostic_examples<=3:
        raise ValueError('diagnostic-examples must be between 0 and 3')
    if bool(valid_from)!=bool(test_from):
        raise ValueError('Provide both valid-from and test-from, or neither')
    valid,test=instant(valid_from) if valid_from else None,instant(test_from) if test_from else None
    if valid and valid>=test:
        raise ValueError('Validation must start before test')
    selected=selection(sports) or tuple(PROFILES)
    if any(key not in PROFILES for key in selected):
        raise ValueError('Unsupported training sport')
    path=state/'dataset.sqlite3'
    if path.is_symlink():
        raise ValueError('Refusing redirected collector database')
    checked=now();current=instant(checked)
    report={'format':'doscbot.training-readiness.v1','checked_at':checked,'database':str(path),
            'target':'participant_win.v1','lead_minutes':lead_minutes,'schema_sha256':digest(SCHEMA),
            'preparation_code_sha256':digest({name:hashlib.sha256(Path(__file__).with_name(name+'.py').read_bytes()).hexdigest()
                for name in ('training','training_evidence','training_diagnostics','profiles','store','outcomes','monitor_data')}),
            'valid_from':valid_from,'test_from':test_from,'structurally_ready':False,'sports':{},'rejected':{},'output':None,
            'requested_sports':list(selected),
            'coverage_timezone':'Europe/Moscow',
            'scope':'First normal-period win baseline; no bookmaker settlement or betting profit claim'}
    if not path.is_file():
        report['reason']='database_missing';return report
    diagnostics=[];diagnostic_counts=Counter()
    if diagnostic_examples:
        report['diagnostics']={'limit_per_sport_reason':diagnostic_examples,'limit_total':20,'examples':diagnostics,
                               'scope':'Saved source evidence only; diagnostics never supply labels or features'}
    target=Path(output).expanduser().resolve() if output else None
    if target and (target==finding or finding in target.parents or target==state or state in target.parents):
        raise ValueError('Export outside finding and collector data to protect source databases')
    if target and target.exists():
        raise ValueError('Training export exists; choose a new directory')
    def partition(kickoff):
        if not valid:return 'unassigned',current
        return ('train',valid) if kickoff<valid else ('validation',test) if kickoff<test else ('test',current)
    placeholders=','.join('?' for _ in selected)
    sport_filter=f"json_extract(payload,'$.event.sport') IN ({placeholders})"
    query=f"""WITH ranked AS (
        SELECT id,row_number() OVER(PARTITION BY event_id ORDER BY as_of DESC,id DESC) rn FROM samples
        WHERE training_horizon(as_of,kickoff_at)=1 AND julianday(as_of)<=julianday(?) AND {sport_filter}
    ) SELECT s.* FROM samples s JOIN ranked r ON r.id=s.id WHERE r.rn=1 ORDER BY s.kickoff_at,s.event_id"""
    def records(db, count=False):
        rejected=Counter()
        for row in db.execute(query,(current.isoformat(),*selected)):
            try:
                payload=json.loads(row['payload']);event=payload['event'];widget=payload.get('widget_data')
                if event.get('sport') not in selected:continue
                reason='legacy_input_format' if payload.get('format')!='doscbot.source-input.v2' else None
                reason=reason or sample_blocker(event,payload['team_statistics'],row['as_of'])
                reason=reason or ('widget_missing' if not widget else widget_blocker(event,payload['team_statistics'],widget))
                if reason:
                    rejected[reason]+=1;continue
                check_observation_times(payload,instant(row['as_of']))
                if not all(participant_key(event.get(side)) for side in ('home','away')) or participant_key(event.get('home'))==participant_key(event.get('away')):
                    reason='participant_identity_missing'
                if any((widget.get('match') or {}).get(flag) is True for flag in ('cancelled','postponed','retired','walkover','disqualified')):
                    reason='target_unavailable'
                if (str(event['source_event_id'])!=row['event_id'] or instant(event['kickoff_at'])!=instant(row['kickoff_at']) or
                    instant(payload['as_of'])!=instant(row['as_of']) or digest(payload)!=row['sha256']):
                    reason='sample_integrity_mismatch'
                if reason:
                    rejected[reason]+=1;continue
                split,cutoff=partition(instant(row['kickoff_at']))
                market,market_reason=winner_market(event)
                winner,label_reason,result=linked_label(db,event,widget,market,min(cutoff,current),as_of=row['as_of'])
                sides=list(participant_rows(payload,market))
                if count:
                    item=report['sports'].setdefault(event['sport'],{'events':0,'rows':0,'labelled_rows':0,'labelled_events':0,'full':0,'partial':0,
                        'partitions':{},'label_reasons':{},'market_reasons':{},'label_evidence':{},'train_classes':{},'labelled_hours_moscow':{},
                        'kickoff_first_utc':None,'kickoff_last_utc':None,'labelled_kickoff_first_utc':None,'labelled_kickoff_last_utc':None})
                    item['events']+=1;item['rows']+=2;item['labelled_rows']+=2 if winner else 0;item['labelled_events']+=int(winner is not None)
                    kickoff=instant(row['kickoff_at'])
                    for prefix in ('kickoff', 'labelled_kickoff') if winner else ('kickoff',):
                        for boundary in ('first','last'):
                            key=prefix+'_'+boundary+'_utc';previous=item[key]
                            if previous is None or (kickoff<instant(previous) if boundary=='first' else kickoff>instant(previous)):
                                item[key]=kickoff.isoformat()
                    if winner:
                        hour=kickoff.astimezone(ZoneInfo('Europe/Moscow')).replace(minute=0,second=0,microsecond=0).isoformat()
                        item['labelled_hours_moscow'][hour]=item['labelled_hours_moscow'].get(hour,0)+1
                    item['full' if widget.get('views',{}).get('state')=='captured' else 'partial']+=1
                    item['partitions'][split]=item['partitions'].get(split,0)+(2 if winner else 0)
                    if label_reason:item['label_reasons'][label_reason]=item['label_reasons'].get(label_reason,0)+1
                    if market_reason:item['market_reasons'][market_reason]=item['market_reasons'].get(market_reason,0)+1
                    if winner:
                        for key in ('identity_evidence','format_evidence'):
                            if result.get(key):item['label_evidence'][key]=item['label_evidence'].get(key,0)+1
                    if (diagnostic_examples and label_reason and label_reason!='result_not_available_at_cutoff' and
                            len(diagnostics)<20 and diagnostic_counts[event['sport'],label_reason]<diagnostic_examples):
                        from .training_diagnostics import example
                        try:
                            diagnostics.append(example(db,row,payload,label_reason,result,min(cutoff,current)))
                            diagnostic_counts[event['sport'],label_reason]+=1
                        except (KeyError,TypeError,ValueError,AttributeError):
                            report['diagnostics']['unreadable_examples']=report['diagnostics'].get('unreadable_examples',0)+1
                    if winner and split=='train':
                        for side,_ in sides:
                            label=str(int(winner==side));item['train_classes'][label]=item['train_classes'].get(label,0)+1
                for side,features in sides:
                    yield {'format':'doscbot.participant-training-row.v1','event_group':row['event_id'],'sample_id':row['id'],
                           'sample_sha256':row['sha256'],'source_revision':row['source_revision'],'as_of':row['as_of'],
                           'kickoff_at':row['kickoff_at'],'partition':split,'features':features,
                           'target':int(winner==side) if winner else None,'label_reason':label_reason,
                           'market':market,'result':result,'training_weight':None}
            except (KeyError,TypeError,ValueError,OverflowError):
                rejected['invalid_sample']+=1
        if count:report['rejected']=dict(rejected)
    with closing(readonly(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone():
            raise ValueError('Only the new collector database is allowed')
        owner=db.execute("SELECT value FROM metadata WHERE key='owner'").fetchone()
        if not owner or owner[0]!='doscbot.fresh-collector.v1':raise ValueError('Only the new collector database is allowed')
        def horizon(as_of,kickoff):
            try:return int((instant(kickoff)-instant(as_of)).total_seconds()>=lead_minutes*60)
            except (TypeError,ValueError,OverflowError):return 0
        db.create_function('training_horizon',2,horizon,deterministic=True)
        report['sample_count']=db.execute('SELECT count(*) FROM samples').fetchone()[0]
        report['sample_events']=db.execute(f'SELECT count(DISTINCT event_id) FROM samples WHERE {sport_filter}',selected).fetchone()[0]
        report['snapshot']={'max_sample_id':db.execute('SELECT max(id) FROM samples').fetchone()[0],
                            'max_observation_id':db.execute('SELECT max(id) FROM observations').fetchone()[0]}
        for _ in records(db,True):pass
        if diagnostics:
            from .training_diagnostics import saved_context
            saved_context(db,diagnostics)
        report['eligible_events']=sum(item['events'] for item in report['sports'].values())
        report['labelled_events']=sum(item['labelled_events'] for item in report['sports'].values())
        report['without_horizon_sample']=report['sample_events']-report['eligible_events']-sum(report['rejected'].values())
        blockers=[] if valid else ['time_boundaries_missing']
        if valid:
            for sport in selected:
                for split in ('train','validation','test'):
                    if not report['sports'].get(sport,{}).get('partitions',{}).get(split,0):blockers.append(sport+':'+split+'_empty')
                if set(report['sports'].get(sport,{}).get('train_classes',{}))!={'0','1'}:blockers.append(sport+':train_single_class')
        report['blocking_reasons']=blockers
        report['structurally_ready']=not blockers
        report['readiness_scope']='Structural prerequisites only; not statistical validation or a claim of model quality'
        report['split_required']=not bool(valid)
        if target:
            target.mkdir(parents=True,exist_ok=False)
            try:
                train_counts={sport:item['partitions'].get('train',0) for sport,item in report['sports'].items()
                              if item['partitions'].get('train',0)}
                checksum=hashlib.sha256();count=0
                with (target/'rows.jsonl').open('xb') as handle:
                    for record in records(db):
                        sport=record['features']['sport']
                        if record['partition']=='train' and record['target'] is not None:
                            record['training_weight']=sum(train_counts.values())/(len(train_counts)*train_counts[sport])
                        line=(encoded(record)+'\n').encode('utf-8');handle.write(line);checksum.update(line);count+=1
                report['export_rows']=count
                report['rows_sha256']=checksum.hexdigest()
                (target/'schema.json').write_text(json.dumps(SCHEMA,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                report['output']=str(target)
                (target/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            except BaseException:
                shutil.rmtree(target)
                raise
    return report
