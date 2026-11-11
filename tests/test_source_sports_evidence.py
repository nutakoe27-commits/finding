"""Source captions and null-status StatsHub history across supported sports."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import unittest

from matchlab.config import Settings
from matchlab.match_history import normalize
from matchlab.neural_verdict import build_packet,generate
from matchlab.season_evidence import source_pairs
from matchlab.statistics import analysis_statistics
from matchlab.statistics_feed import extract_passive_feed
from matchlab.verdict_sports import PROFILES,capabilities
from matchlab.widget_results import reported_full_time,REPORTED_WARNING
from test_multisport_verdict import answer
from verdict_helpers import statistics


# Exact captions/cell layout from the owner's Temple/Charleston snapshot.
BASKETBALL='''Статистика сезона
За матч
Итоги
ТЕМ
КОЧ
60.5
Очки
111.5
32
Подборы
42
12.5
Ассисты
24.5
9.5
Перехваты
12.5
6
Заблокировано
5.5
13
Потеря мяча
9.5
56.5
Попыток бросков с игры
80
38
%
% успешных бросков с игры
57
%
31
Попыток 3-очковых
32
24
%
% успешных 3-очковых
33
%
15.5
Попыток штрафных
12.5
65
%
% успешных штрафных
72
%
Вся статистика
Вся статистика'''
CAPTIONS={'football':'47\n%\nПобеды\n19\n%',
          'hockey':'2.7\nШайб заброшено\n3.1',
          'basketball':BASKETBALL,
          'tennis':'55.6\n%\nРеализованные брейк-пойнты\n66.7\n%',
          'volleyball':'8\nРезультативные блоки\n5',
          'table_tennis':'4\nПартии выиграны\n2'}


class SourceSportsEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.teams={'home':{'_id':11,'name':'Первый участник'},'away':{'_id':22,'name':'Второй участник'}}

    def event(self,sport):
        return {'source':'winline','source_event_id':'17136987','sport':sport,
            'home':'Первый участник','away':'Второй участник','competition':'Тестовый турнир',
            'phase':'prematch','kickoff_at':(self.now+timedelta(hours=12)).isoformat()}

    def row(self,sport,side='away'):
        row={'_id':100+int(side=='away'),'_sid':PROFILES[sport].archive_id,'status':None,
            'time':{'uts':int((self.now-timedelta(days=2)).timestamp())},
            'teams':{'home':self.teams[side],'away':{'_id':33,'name':'Соперник'}},
            'postponed':False,'retired':False,'walkover':False,'canceled':False}
        # Generic rows use the structures supplied for football/hockey/NCAA;
        # set-sport examples also verify a complete supported best-of format.
        parts={'football':[(4,0)],'hockey':[(1,1),(0,0),(3,1)],
            'basketball':[(52,27),(60,39)],'tennis':[(6,4),(6,0)],
            'volleyball':[(25,20),(25,21),(25,19)],'table_tennis':[(11,6),(11,7),(11,8)]}[sport]
        full={'football':(6,0),'hockey':(4,2),'basketball':(112,66),
            'tennis':(2,0),'volleyball':(3,0),'table_tennis':(3,0)}[sport]
        as_dict=lambda v:dict(zip(('home','away'),v))
        row['result']={**as_dict(full),'period':'nt','winner':'home'}
        row['periods']={'ft':as_dict(full),**{f'p{i+1}':as_dict(v) for i,v in enumerate(parts)}}
        if sport in ('football','basketball','hockey'):row.update(numberofperiods=3 if sport=='hockey' else 2,overtimeperiods={})
        else:row['bestof']=3 if sport=='tennis' else 5
        return row

    def snapshot(self,sport,*,season=False,history=False):
        snapshot=statistics(stats_id='700',at=self.now.isoformat())
        snapshot['match_phase']='unknown'
        snapshot['sections']=[{'title':'Статистика сезона','text':CAPTIONS[sport]}] if season else []
        if history:
            anchor={'_id':700,'_sid':PROFILES[sport].archive_id,'teams':self.teams}
            feed=extract_passive_feed([{'match':anchor}]+[{'team':self.teams[s], 'matches':[self.row(sport,s)]} for s in self.teams],'700')
            feed['observed_at']=self.now.isoformat();snapshot['structured_feed']=feed
        return analysis_statistics(snapshot)

    def packet(self,sport,snapshot):
        event=self.event(sport);p=PROFILES[sport]
        quote={**event,'observed_at':self.now.isoformat(),'scope':p.scope,
            'odds':{'home':1.9,'away':2.1,**({'draw':3.3} if p.draw else {})}}
        return build_packet(None,event,snapshot,self.now,quote)

    def test_all_active_sports_keep_source_captions_without_a_hardcoded_metric_whitelist(self):
        for sport in capabilities():
            with self.subTest(sport=sport):
                packet=self.packet(sport,self.snapshot(sport,season=True))
                self.assertFalse(packet['policy']['force_skip'],packet['limitations'])
                facts=[f for f in packet['facts'] if f['kind']=='statistics']
                self.assertEqual({f['side'] for f in facts},{'home','away'})
                self.assertTrue(all(f['measurement']=='unknown' for f in facts))
                self.assertTrue(any('нельзя выдавать за средние' in w for w in packet['limitations']))

    def test_temple_charleston_season_no_longer_stops_before_qwen(self):
        packet=self.packet('basketball',self.snapshot('basketball',season=True))
        values={f['side']:f['source_value'] for f in packet['facts'] if f.get('metric')=='Очки'}
        self.assertEqual(values,{'home':'60.5','away':'111.5'})
        settings=Settings(Path('/tmp'),'http://localhost:9/v1','fixture',event_analysis_mode='neural-verdict')
        seen=[]
        def model(_,messages):
            seen.append(json.loads(messages[1]['content']));return json.dumps(answer(packet))
        verdict,generation=generate(settings,packet,model,historical=True)
        self.assertEqual((generation['model_calls'],verdict['engine']),(1,'qwen'))
        self.assertEqual(seen[0]['facts'],packet['facts'])
        self.assertLess(len(json.dumps(packet,ensure_ascii=False).encode()),16000)

    def test_percentages_ratios_and_decimals_keep_units_and_cannot_be_confused_with_win_chances(self):
        section={'text':'38\n%\n% успешных бросков с игры\n57\n%\n1,2\nПоказатель подачи\n2,4\n3/5\nБрейк-пойнты\n2/4'}
        rows=source_pairs(section)
        self.assertEqual([(r['home'],r['away'],r['unit']) for r in rows],[('38%','57%','%'),('1,2','2,4',''),('3/5','2/4','ratio')])

    def test_conflicting_truncated_and_mixed_unit_rows_do_not_create_evidence(self):
        for section in ({'text':'1\nОчки\n2','truncated':True},
            {'text':'38\n%\nОчки\n57'}, {'text':'1\nОчки\n2\n3\nОчки\n4'},
            {'text':'101\n%\nПобеды\n50\n%'}, {'text':'1\nВся статистика\n2'}):
            with self.subTest(section=section):self.assertEqual(source_pairs(section),[])

    def test_selected_filters_are_used_but_visible_button_names_are_not_selected_filters(self):
        s=self.snapshot('basketball',season=True);s['sections'][0]['filters']=['За матч']
        packet=self.packet('basketball',s)
        self.assertTrue(all(f['measurement']=='rate' for f in packet['facts'] if f['kind']=='statistics'))
        s['sections'][0]['filters']=['Итоги']
        self.assertTrue(all(f['measurement']=='total' for f in self.packet('basketball',s)['facts'] if f['kind']=='statistics'))

    def test_all_sports_can_use_consistent_source_full_time_history_without_claiming_independent_confirmation(self):
        for sport in capabilities():
            with self.subTest(sport=sport):
                packet=self.packet(sport,self.snapshot(sport,history=True))
                self.assertFalse(packet['policy']['force_skip'],packet['limitations'])
                self.assertIn(REPORTED_WARNING,packet['limitations'])
                facts=[f for f in packet['facts'] if f['kind']=='history']
                self.assertEqual({f['side'] for f in facts},{'home','away'})
                self.assertTrue(all(f['source_reported_results']==1 for f in facts))
                self.assertTrue(all(REPORTED_WARNING in f['text'] for f in facts))

    def test_legacy_football_calculations_do_not_enable_reported_history_implicitly(self):
        s=self.snapshot('football',history=True);context={'event':self.event('football'),'team_statistics':s}
        self.assertEqual(normalize(context,self.now,broad=True)['rows'],[])
        self.assertEqual(len(normalize(context,self.now,broad=True,allow_reported=True)['rows']),2)

    def test_future_current_wrong_sport_and_unverified_histories_still_cannot_supply_facts(self):
        for sport in PROFILES:
            for mutation in ('future','current','sport','unverified','wrong_anchor'):
                s=self.snapshot(sport,history=True);feed=s['structured_feed']
                row=feed['participants']['away'][0]['verified_matches'][0]
                if mutation=='future':row['time']['uts']=int((self.now+timedelta(days=1)).timestamp())
                if mutation=='current':row['_id']=700
                if mutation=='sport':row['_sid']=999
                if mutation=='unverified':feed['participants']['away'][0]['unverified_matches']=feed['participants']['away'][0].pop('verified_matches')
                if mutation=='wrong_anchor':feed['match']['_id']=999
                with self.subTest(sport=sport,mutation=mutation):
                    packet=self.packet(sport,s)
                    self.assertTrue(packet['policy']['force_skip'])

    def test_explicit_nonfinal_flags_and_inconsistent_full_scores_are_rejected_for_every_sport(self):
        for sport in PROFILES:
            for mutation in ('live','scheduled','status_id','cancel','retire','winner','score','ft_missing','breakdown'):
                row=self.row(sport)
                if mutation=='live':row['status']='live'
                if mutation=='scheduled':row['status']='scheduled'
                if mutation=='status_id':row['status']={'_id':1}
                if mutation=='cancel':row['canceled']=True
                if mutation=='retire':row['retired']=True
                if mutation=='winner':row['result']['winner']='away'
                if mutation=='score':row['result']['home']+=1
                if mutation=='ft_missing':row['periods'].pop('ft')
                if mutation=='breakdown':row['periods']['p1']['home']=999
                with self.subTest(sport=sport,mutation=mutation):self.assertIsNone(reported_full_time(row,sport))

    def test_unknown_format_and_overtime_do_not_become_regulation_history(self):
        for sport in ('basketball','hockey'):
            row=self.row(sport);row['numberofperiods']=1
            self.assertIsNone(reported_full_time(row,sport))
            row=self.row(sport);row['overtimeperiods']={'ot1':{'home':1,'away':0}}
            self.assertIsNone(reported_full_time(row,sport))
        for sport in ('tennis','table_tennis','volleyball'):
            row=self.row(sport);row['bestof']=7 if sport=='tennis' else 3
            self.assertIsNone(reported_full_time(row,sport))

    def test_specific_missing_participant_reaches_admin_diagnostics_without_logging_private_notes(self):
        from matchlab.api import generation_details
        s=self.snapshot('basketball',history=True)
        s['structured_feed']['participants']['away'][0]['verified_matches'][0]['status']='live'
        packet=self.packet('basketball',s);packet['unverified_user_notes']='private-note-must-not-be-logged'
        with self.assertLogs('uvicorn.error',level='INFO') as log:
            verdict,generation=generate(Settings(Path('/tmp'),'http://localhost:9/v1','fixture'),packet,
                lambda *_:self.fail('must not call model'))
        self.assertEqual(generation['reason'],'insufficient_data')
        self.assertTrue(any('Второй участник' in line for line in generation['input_blockers']))
        details=generation_details({'id':'a'*32,'context':{'ai_generation':generation}})
        self.assertEqual(details['input_blockers'],packet['blockers'])
        self.assertIn('Второй участник',' '.join(log.output))
        self.assertNotIn('private-note-must-not-be-logged',' '.join(log.output))


if __name__=='__main__':unittest.main()
