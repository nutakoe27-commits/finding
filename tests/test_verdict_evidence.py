"""Aliases, table columns, league scope and explicit truncation are evidence boundaries."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import unittest
from unittest.mock import patch

from matchlab.neural_verdict import build_packet
from matchlab.verdict_evidence import aliases,standing_rows
from matchlab.statistics import analysis_statistics
from matchlab.bookmakers import make_snapshot
from verdict_helpers import statistics


def table(away='Зенит'):
    header='#\nКоманда\nИ\nВ\nН\nП\nЗ\nП\nРазн.\nОчки\nФорма\n'
    return {'title':'Таблица','text':header+'1\nСпартак\n6\t3\t1\t2\t12\t9\t3\t10\nВ\nП\n2\n'+away+'\n6\t2\t1\t3\t8\t10\t-2\t7\nП\nВ'}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.at=datetime.now(timezone.utc)
        self.event={'home':'Спартак','away':'Зенит','sport':'football','competition':'League',
            'phase':'prematch','kickoff_at':(self.at+timedelta(days=1)).isoformat()}
        self.stats=analysis_statistics(statistics())

    def packet(self,stats=None,repo=None):
        return build_packet(repo,self.event,self.stats if stats is None else stats,self.at)

    def test_table_keeps_only_unambiguous_rows_for_the_actual_participants(self):
        self.stats['sections'].append(table('Депортиво'))
        packet=self.packet();rows=[f for f in packet['facts'] if f.get('scope')=='standings']
        self.assertEqual([r['side'] for r in rows],['home'])
        self.assertTrue(any('привязка' in x for x in packet['limitations']))
        self.assertNotIn('Депортиво',' '.join(f['text'] for f in packet['facts']))

    def test_a_provider_alias_requires_the_same_statistics_anchor_and_distinct_team_ids(self):
        self.stats['structured_feed']={'statistics_id':'72334410','match':{'_id':72334410,
            'teams':{'home':{'_id':10,'name':'Спартак'},'away':{'_id':20,'name':'Депортиво'}}},'participants':{'home':[],'away':[]}}
        self.stats['sections'].append(table('Депортиво'))
        rows=[f for f in self.packet()['facts'] if f.get('scope')=='standings']
        self.assertEqual([r['side'] for r in rows],['home','away'])
        for mutation in ('wrong_match','missing_anchor','shared_ids'):
            s=deepcopy(self.stats)
            if mutation=='wrong_match':s['structured_feed']['match']['_id']=999
            elif mutation=='missing_anchor':s['structured_feed'].pop('statistics_id');s['structured_feed']['match'].pop('_id');s.pop('statistics_id')
            else:s['structured_feed']['match']['teams']['away']['_id']=10
            with self.subTest(mutation=mutation):self.assertNotIn('депортиво',aliases(self.event,s)['away'])

    def test_ambiguous_duplicate_rows_and_unknown_columns_do_not_become_team_facts(self):
        section=table();self.assertEqual(len(standing_rows(section)),2)
        self.assertEqual(standing_rows({'text':section['text'].replace('Очки','Непонятная колонка')}),[])
        bad=deepcopy(section);bad['text']=bad['text'].replace('12\t9\t3','12\t9\t8')
        self.assertEqual([r['name'] for r in standing_rows(bad)],['Зенит'])
        duplicated=deepcopy(section);duplicated['text']+='\n1\nСпартак\n6\t3\t1\t2\t12\t9\t3\t10'
        self.stats['sections'].append(duplicated)
        self.assertEqual([f['side'] for f in self.packet()['facts'] if f.get('scope')=='standings'],['away'])

    def test_history_from_other_competitions_is_separate_and_cannot_overwrite_league_scope(self):
        rows=[{'home':'спартак','away':'зенит','goals':(1,0),'at':self.at-timedelta(days=1),'same_competition':True},
              {'home':'спартак','away':'чужой клуб','goals':(0,4),'at':self.at-timedelta(days=2),'same_competition':False}]
        with patch('matchlab.chances.football_sample',return_value=rows),patch('matchlab.match_history.normalize',return_value={'rows':[{**rows[0],'same_competition':False}]}):
            packet=self.packet(repo=object())
        facts=[f for f in packet['facts'] if f['kind']=='history' and f['side']=='home']
        self.assertEqual([f['scope'] for f in facts],['same_competition','other_competitions'])
        self.assertNotIn('чужой клуб',facts[0]['text']);self.assertIn('чужой клуб',facts[1]['text'])

    def test_history_marks_participant_outcomes_and_latest_wins_in_date_order(self):
        rows=[{'home':'спартак','away':'клуб а','goals':(2,0),'at':self.at-timedelta(days=1),'same_competition':True},
              {'home':'клуб б','away':'спартак','goals':(1,2),'at':self.at-timedelta(days=2),'same_competition':True},
              {'home':'спартак','away':'клуб в','goals':(1,3),'at':self.at-timedelta(days=3),'same_competition':True},
              {'home':'клуб г','away':'спартак','goals':(2,1),'at':self.at-timedelta(days=4),'same_competition':True}]
        with patch('matchlab.chances.football_sample',return_value=[rows[2],rows[0],rows[3],rows[1]]):packet=self.packet(repo=object())
        fact=next(f for f in packet['facts'] if f['kind']=='history' and f['side']=='home')
        self.assertEqual(fact['history_order'],'newest_first')
        self.assertEqual(fact['outcomes'],['win','win','loss','loss'])
        self.assertIn('Последние известные исходы: '+str(rows[0]['at'].date())+' — победа; '+str(rows[1]['at'].date())+' — победа.',fact['text'])
        self.assertLess(fact['text'].index('клуб а'),fact['text'].index('клуб б'))
        self.assertIn('исход: поражение',fact['text'])

    def test_away_wins_losses_and_draws_are_not_reversed_by_score_orientation(self):
        rows=[{'home':'соперник','away':'спартак','goals':score,'at':self.at-timedelta(days=i+1),'same_competition':True}
              for i,score in enumerate(((0,2),(3,1),(1,1)))]
        with patch('matchlab.chances.football_sample',return_value=rows):packet=self.packet(repo=object())
        fact=next(f for f in packet['facts'] if f['kind']=='history' and f['side']=='home')
        self.assertEqual(fact['outcomes'],['win','loss','draw'])
        self.assertIn('соперник — спартак 0:2; исход: победа',fact['text'])

    def test_attack_and_conceded_comparisons_keep_separate_directions(self):
        self.stats['sections']=[{'title':'Статистика сезона','filters':['За матч','Общее'],
            'text':'1.42\nЗабитые мячи\n1.08\n1.75\nГолов пропущено\n1.42'},
            {'title':'Статистика сезона','filters':['За матч','Хозяева / Гости'],
             'text':'1\nЗабитые мячи\n0.67\n1.33\nГолов пропущено\n1.5'}]
        facts={f['side']:f for f in self.packet()['facts'] if f.get('scope')=='venue' and f['kind']=='statistics'}
        self.assertEqual(facts['home']['venue_vs_overall'],{'for':'lower','against':'lower'})
        self.assertEqual(facts['away']['venue_vs_overall'],{'for':'lower','against':'higher'})
        self.assertIn('забивает меньше; пропускает меньше',facts['home']['text'])
        self.assertIn('забивает меньше; пропускает больше',facts['away']['text'])
        self.stats['sections'][1]['text']=self.stats['sections'][0]['text']
        equal=[f for f in self.packet()['facts'] if f.get('scope')=='venue' and f['kind']=='statistics']
        self.assertTrue(all(f['venue_vs_overall']=={'for':'equal','against':'equal'} for f in equal))

    def test_latest_history_summary_never_merges_cup_results_into_league_form(self):
        rows=[{'home':'спартак','away':'соперник а','goals':(0,1),'at':self.at-timedelta(days=5),'same_competition':True},
              {'home':'спартак','away':'соперник б','goals':(4,0),'at':self.at-timedelta(days=1),'same_competition':False}]
        with patch('matchlab.chances.football_sample',return_value=rows):packet=self.packet(repo=object())
        facts={f['scope']:f for f in packet['facts'] if f['kind']=='history' and f['side']=='home'}
        self.assertEqual(facts['same_competition']['outcomes'],['loss'])
        self.assertEqual(facts['other_competitions']['outcomes'],['win'])

    def test_evidence_budget_preserves_required_market_when_history_fills_the_packet(self):
        quote=make_snapshot('winline','League','Спартак','Зенит',self.event['kickoff_at'],self.at.isoformat(),
                            [2.5,3.4,2.8],'https://winline.ru/stavki/sport/futbol/123')
        def fill(repo,event,statistics,at,add):
            for i in range(40):add('history',str(i)+' '+('Сведения о прошлых матчах. '*35),side='home' if i%2==0 else 'away')
            return []
        with patch('matchlab.verdict_evidence.collect',side_effect=fill):packet=build_packet(None,self.event,self.stats,self.at,quote)
        self.assertEqual(len([f for f in packet['facts'] if f['kind']=='market']),1)
        self.assertFalse(packet['policy']['force_skip'])
        self.assertTrue(any('сокращена' in note for note in packet['limitations']))

    def test_cutting_a_long_history_is_recorded_on_the_fact_and_in_limitations(self):
        rows=[{'home':'спартак','away':'соперник'+str(i)+'а'*190,'goals':(1,0),
            'at':self.at-timedelta(days=i+1),'same_competition':True} for i in range(8)]
        with patch('matchlab.chances.football_sample',return_value=rows):packet=self.packet(repo=object())
        self.assertTrue(any(f.get('truncated') for f in packet['facts']))
        self.assertTrue(any('сокращена' in x for x in packet['limitations']))

    def test_player_columns_without_team_binding_are_not_assigned_by_position(self):
        self.stats['sections'].append({'title':'Лучшие Игроки','text':'Игрок А\nИгрок Б\n4\nГолы\n9'})
        packet=self.packet()
        self.assertNotIn('Игрок А',' '.join(f['text'] for f in packet['facts']))
        self.assertTrue(any('принадлежность' in x for x in packet['limitations']))


if __name__=='__main__':unittest.main()
