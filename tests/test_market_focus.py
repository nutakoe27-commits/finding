"""Relevant-market selection and read-only comparisons of the same frozen input."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from account_helpers import signed_in
from test_product import event
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.market_focus import family,focus_context
from matchlab.qwen_probe import compare_context,saved_sample
from matchlab.storage import Repository


def market(name,index,period='осн.время',parameter=None,odds=(1.9,1.9)):
    return {'key':f'{index:032x}','name':name,'period':period,'parameter':parameter,'is_stale':False,
            'outcomes_complete':True,'outcomes':[{'key':f'{index*10+i:032x}','name':str(i+1),
                'odds':price,'state':'open','parameter':None} for i,price in enumerate(odds)]}


def pool():
    markets=[market('Исход 1X2',1,odds=(2.1,3.4,3.5)),market('Двойной шанс',2,odds=(1.3,1.7,1.6)),
             market('Обе забьют',3),market('Фора',4,parameter='-0.5'),market('Победа без ничьей',5),
             market('Индивидуальный тотал 1',6,parameter='1.5'),market('Индивидуальный тотал 2',7,parameter='1.5')]
    for i,prices in enumerate(((1.01,15),(1.2,4),(1.9,1.9),(3,1.4),(5,1.1))):
        markets.append(market('Тотал',10+i,parameter=str(i+.5),odds=prices))
    markets.extend([market('Исход 1X2',30,period='1 тайм',odds=(1.8,2.5,3.9)),
                    market('Тотал угловых',31),market('Тотал карточек',32),market('Чет/Нечет',33),
                    market('Точный счёт',34)])
    rows=[{'market_key':m['key'],'outcome_key':o['key'],'market':m['name'],'period':m['period'],
           'parameter':m['parameter'],'outcome':o['name'],'odds':o['odds'],'probability':.7,'basis':'market'}
          for m in markets for o in m['outcomes']]
    return {'event':{'home':'А','away':'Б','sport':'football','phase':'prematch'},'markets':markets,
            'selection':{'market_key':None,'outcome_key':None},
            'coverage':{'stored_markets':len(markets),'stored_outcomes':len(rows),'analyzed_markets':len(markets),'analyzed_outcomes':len(rows)},
            'team_statistics':{'sections':[{'title':'Форма','text':'Полная статистика участников'}]},
            'archive_history':{'state':'ready','teams':[{'recent_results':[{'score':'2:1'}]*20}]},
            'web_research':{'state':'disabled','sources':[]},
            'probability_assessment':{'markets':rows,'winner':{**rows[0],'participant':'А'},
                'facts':['Подтверждённый факт'],'limitations':[],'finished':False}}


class MarketFocusTests(unittest.TestCase):
    def test_diverse_main_markets_replace_part_markets_and_lopsided_alternative_lines(self):
        full=pool();before=deepcopy(full);focused=focus_context(full,8)
        self.assertEqual(len(focused['markets']),8)
        self.assertEqual({family(m) for m in focused['markets']},
                         {'winner','double_chance','both_score','handicap','draw_no_bet','team_total_home','team_total_away','total'})
        total=next(m for m in focused['markets'] if family(m)=='total')
        self.assertEqual(total['parameter'],'2.5')
        self.assertEqual(focused['team_statistics'],full['team_statistics'])
        self.assertEqual(focused['archive_history'],full['archive_history'])
        self.assertEqual(full,before)

    def test_explicit_exotic_selection_is_kept_whole_with_primary_winner_within_limit(self):
        full=pool();selected=full['markets'][-3];full['selection']['market_key']=selected['key']
        full['selection']['outcome_key']=selected['outcomes'][-1]['key']
        focused=focus_context(full,2)
        self.assertEqual(focused['markets'][0],selected)
        self.assertEqual(focused['markets'][1]['key'],full['probability_assessment']['winner']['market_key'])
        self.assertEqual(len(focused['markets']),2)
        self.assertEqual(focused['selection'],full['selection'])

    def test_duplicate_probability_rows_are_pruned_with_matching_markets_and_coverage_is_honest(self):
        full=pool();focused=focus_context(full)
        keys={m['key'] for m in focused['markets']}
        self.assertEqual({r['market_key'] for r in focused['probability_assessment']['markets']},keys)
        self.assertEqual(focused['coverage']['analyzed_markets'],8)
        self.assertEqual(focused['coverage']['stored_markets'],len(full['markets']))
        for chosen in focused['markets']:
            self.assertEqual(chosen,next(m for m in full['markets'] if m['key']==chosen['key']))
        self.assertEqual(focused['probability_assessment']['winner'],full['probability_assessment']['winner'])

    def test_stale_and_closed_auto_markets_are_excluded(self):
        full=pool();full['markets'][2]['is_stale']=True;full['markets'][3]['outcomes'][0]['state']='closed'
        keys={m['key'] for m in focus_context(full)['markets']}
        self.assertNotIn(full['markets'][2]['key'],keys);self.assertNotIn(full['markets'][3]['key'],keys)

    def test_expired_primary_is_retained_as_context_without_price_predictions(self):
        full=pool()
        for m in full['markets']:m['is_stale']=True
        focused=focus_context(full)
        self.assertEqual(len(focused['markets']),1)
        self.assertTrue(focused['markets'][0]['is_stale'])
        self.assertEqual(focused['market_selection']['eligible_markets'],0)
        self.assertEqual(focused['market_selection']['context_only_keys'],[full['markets'][0]['key']])
        self.assertIsNone(focused['probability_assessment']['winner'])
        self.assertEqual(focused['probability_assessment']['markets'],[])
        self.assertIsNone(focused['market_implied'])
        self.assertTrue(any('Свежих доступных рынков нет' in line for line in focused['probability_assessment']['limitations']))

    def test_explicit_source_headline_keeps_its_scope_even_when_its_label_is_unfamiliar(self):
        full=pool();primary=market('Линия источника',90,period='1 период')
        full['markets'].append(primary);full['primary_market_key']=primary['key']
        focused=focus_context(full)
        self.assertIn(primary,focused['markets'])
        self.assertEqual(focused['primary_market_key'],primary['key'])

    def test_tennis_units_and_hockey_overtime_preserve_their_actual_scope(self):
        self.assertEqual(family(market('Тотал геймов',1,period='Матч')),'total')
        self.assertIsNone(family(market('Победитель',2,period='1 сет')))
        full=pool();full['markets']=[market('Исход 1X2',1,odds=(2,3,4)),
            market('Победитель',2,period='с ОТ и Б'),market('Исход 1X2',3,period='1 период',odds=(2,3,4))]
        focused=focus_context(full)
        self.assertEqual({m['period'] for m in focused['markets']},{'осн.время','с ОТ и Б'})
        self.assertEqual(family(market('Победитель (с ОТ и Б)',4,period='Матч')),'winner')

    def test_combination_minute_and_per_team_yes_no_markets_are_not_primary_families(self):
        for name,period,parameter in (('Исход и обе забьют','осн.время',None),
                ('Обе забьют и тотал','осн.время','Больше 2.5'),('Исход 1X2','1-10 мин.',None),
                ('Победит (осн.время)','осн.время','А')):
            self.assertIsNone(family(market(name,90,period=period,parameter=parameter)))
        self.assertEqual(family(market('Тотал (осн.время) Торренсе',95,parameter='Больше'),
                                {'home':'Торренсе','away':'Лейрия'}),'team_total_home')
        self.assertIsNone(family(market('Тотал (осн.время) Торренсе',95,parameter='Больше')))

    def test_source_total_and_handicap_tables_keep_both_rows_with_all_their_lines(self):
        full=pool();full['event'].update(home='А',away='Б')
        full['markets']=[full['markets'][0]]
        for index,(name,parameter,labels) in enumerate((
                ('Тотал','Больше',['б 1.5','б 2.5','б 3.5']),('Тотал','Меньше',['м 1.5','м 2.5','м 3.5']),
                ('Фора','А',['−1.0','0.0','+1.0']),('Фора','Б',['+1.0','0.0','−1.0'])),100):
            row=market(name,index,parameter=parameter,odds=(2.1,1.9,1.8))
            for outcome,label in zip(row['outcomes'],labels):outcome['name']=label
            full['markets'].append(row)
        full['markets'].append(market('Тотал угловых',199))
        focused=focus_context(full,8)
        self.assertEqual(len(focused['markets']),5)
        self.assertEqual(focused['markets'],full['markets'][:5])
        limited=focus_context(full,2)
        self.assertEqual(len(limited['markets']),1,'A source table must not be cut into one-sided automatic rows')

    def test_disabled_filter_and_already_small_input_do_not_add_overhead(self):
        full=pool();self.assertIs(focus_context(full,0),full)
        full['markets']=full['markets'][:1]
        self.assertIs(focus_context(full,8),full)
        for limit in (1,-1,21):
            with self.assertRaises(ValueError):focus_context(full,limit)


class MarketComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.settings=Settings(Path(self.tmp.name),'http://localhost:9/v1','fixture',qwen_batch_tokens=128000)
        self.repo=Repository(self.settings.database);self.repo.initialize()
        self.full=pool();self.focused=focus_context(self.full)

    def save(self,context,full=None):
        self.repo.save_event_analysis({'id':'saved','generated_at':datetime.now(timezone.utc).isoformat(),'context':context},
                                      'request',user_id='owner',full_context=full)

    def request(self,settings,method,path,**kwargs):
        if path=='/models':return {'data':[{'id':'fixture','max_model_len':131072}]}
        if path=='/tokenize':return {'count':sum(len(m['content'].encode()) for m in kwargs['json']['messages'])//2}
        self.fail('Comparison must not generate model output')

    def test_comparison_reads_original_audit_snapshot_and_tokenizes_only_two_inputs(self):
        self.save(self.focused,self.full)
        with patch('matchlab.qwen._request',side_effect=self.request) as request:
            report=compare_context(self.settings)
        self.assertEqual(report['sample']['source'],'audit_snapshot')
        self.assertEqual((report['full']['markets'],report['focused']['markets']),(len(self.full['markets']),8))
        self.assertGreater(report['savings']['input_tokens'],0)
        self.assertEqual(report['full']['sections_bytes']['team_statistics'],report['focused']['sections_bytes']['team_statistics'])
        self.assertEqual(request.call_count,3)
        self.assertNotIn('Полная статистика',json.dumps(report,ensure_ascii=False))
        self.assertEqual(saved_sample(self.settings)[0],self.focused)

    def test_old_database_without_audit_table_remains_read_only_and_can_be_compared(self):
        self.save(self.full)
        with self.repo.connection() as db:db.execute('DROP TABLE event_analysis_inputs')
        with patch('matchlab.qwen._request',side_effect=self.request):report=compare_context(self.settings,5)
        self.assertEqual(report['sample']['source'],'saved_report')
        self.assertEqual(report['focused']['markets'],5)
        with self.repo.connection() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='event_analysis_inputs'").fetchone())

    def test_missing_tokenizer_never_reports_estimated_counts_as_real_tokens(self):
        self.save(self.full)
        with patch('matchlab.qwen._request',side_effect=ValueError('Нет подключения')):
            report=compare_context(self.settings)
        self.assertNotIn('input_tokens',report['focused'])
        self.assertNotIn('tokens_percent',report['savings'])
        self.assertIn('tokenizer_error',report['focused'])


class MarketFocusApiTests(unittest.TestCase):
    def test_model_receives_shortlist_while_database_retains_exact_full_input(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(Path(folder),'http://localhost:9/v1','fixture')
            repo=Repository(settings.database)
            with TestClient(create_app(settings)) as client:
                signed_in(client,repo)
                source=event();source['markets']=pool()['markets']
                source['coverage']['expected_outcomes']=sum(len(m['outcomes']) for m in source['markets'])
                repo.import_event_markets(source)
                stored=repo.event_markets('123')
                selected=next(m for m in stored['markets'] if m['name']=='Тотал карточек')
                with patch('matchlab.api.qwen.explain_event',return_value='Разбор') as model:
                    response=client.post('/api/bookmakers/events/123/explain',json={'market_key':selected['key']})
                self.assertEqual(response.status_code,200,response.text)
                context=model.call_args.args[1]
                self.assertLessEqual(len(context['markets']),8)
                self.assertIn(selected['key'],{m['key'] for m in context['markets']})
                self.assertEqual(len(repo.event_markets('123')['markets']),len(source['markets']))
                focused,_=saved_sample(settings)
                full,metadata=saved_sample(settings,True)
                self.assertEqual(metadata['source'],'audit_snapshot')
                self.assertEqual(len(full['markets']),len(source['markets']))
                self.assertEqual(full['team_statistics'],focused['team_statistics'])
                self.assertEqual(repo.event_analysis(analysis_id=response.json()['id'])['context'],response.json()['context'])


if __name__=='__main__':unittest.main()
