"""Checks for observed reasoning errors, without predicting match outcomes."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import unittest
from unittest.mock import patch

from matchlab.bookmakers import make_snapshot
from matchlab.config import Settings
from matchlab.neural_verdict import build_packet,generate,validate_reply,VerdictValidationError
from matchlab.verdict_checks import add_comparisons,semantic_issues
from verdict_helpers import reply


class VerdictEvidenceChecksTests(unittest.TestCase):
    def setUp(self):
        self.at=datetime.now(timezone.utc)
        self.event={'source_event_id':'123','home':'Альфа (до21)','away':'Бета (до21)',
            'competition':'Лига','sport':'football','phase':'prematch',
            'kickoff_at':(self.at+timedelta(hours=2)).isoformat()}
        self.stats={'match_phase':'prematch','sections':[
            {'title':'Статистика сезона','filters':['За матч','Общее'],
             'text':'1.5\nЗабитые мячи\n2.67\n2.33\nГолов пропущено\n2.67'},
            {'title':'Статистика сезона','filters':['За матч','Хозяева / Гости'],
             'text':'1\nЗабитые мячи\n2\n2.5\nГолов пропущено\n1.67'}]}
        self.rows=[]

    def history(self,home_scores,away_scores):
        for s,scores in (('home',home_scores),('away',away_scores)):
            for i,score in enumerate(scores):
                self.rows.append({'home':self.event[s].casefold(),'away':f'соперник {s} {i}',
                    'goals':score,'at':self.at-timedelta(days=i+1),'same_competition':True})

    def packet(self):
        q=make_snapshot('winline','Лига',self.event['home'],self.event['away'],self.event['kickoff_at'],
            self.at.isoformat(),[1.63,4.5,4.1],'https://winline.ru/stavki/sport/futbol/123')
        with patch('matchlab.chances.football_sample',return_value=self.rows):
            return build_packet(object(),self.event,self.stats,self.at,q)

    def error(self,p,value,rule):
        with self.assertRaises(VerdictValidationError) as caught:validate_reply(value,p)
        self.assertIn(rule,[i['rule'] for i in caught.exception.details['issues']])
        return caught.exception

    def test_rate_comparisons_preserve_scope_and_do_not_predict_a_winner(self):
        p=self.packet();facts=[f for f in p['facts'] if f.get('comparison_type')=='rates']
        self.assertEqual([f['scope'] for f in facts],['overall','venue'])
        self.assertEqual(facts[0]['home_relation'],{'for':'lower','against':'lower'})
        self.assertEqual(facts[1]['home_relation'],{'for':'lower','against':'higher'})
        self.assertTrue(all(len(f['source_fact_ids'])==2 for f in facts))
        self.assertIn('а не оценка силы',facts[0]['text'])
        self.assertLessEqual(len(json.dumps(p['facts'],ensure_ascii=False,separators=(',',':')).encode()),11000)

    def test_an_actual_reversed_conceded_comparison_is_rejected_and_can_be_repaired(self):
        p=self.packet();bad=reply(p)
        bad['comparison']['home']['text']='Альфа забивает меньше (1.5) и пропускает больше (2.33), чем Бета. Это важное отличие команд.'
        error=self.error(p,bad,'comparison_direction')
        detail=next(i for i in error.details['issues'] if i['rule']=='comparison_direction')
        self.assertEqual(detail['actual_relation'],'lower');self.assertEqual(detail['metric'],'against')
        self.assertEqual(detail['values'],{'home':2.33,'away':2.67})
        good=deepcopy(bad);good['comparison']['home']['fact_ids']=['F1','F2']
        good['comparison']['home']['text']='Альфа забивает меньше (1.5) и пропускает меньше (2.33), чем Бета. Это описание исходных средних.'
        seen=[]
        def model(opts,messages):
            seen.append(messages)
            return json.dumps(bad if len(seen)==1 else good,ensure_ascii=False)
        verdict,g=generate(Settings('/tmp','http://localhost:9/v1','test'),p,model,historical=True)
        self.assertEqual(verdict['engine'],'qwen');self.assertEqual(g['model_calls'],2)
        self.assertEqual(json.loads(seen[1][-1]['content'])['details']['issues'][0]['rule'],'comparison_direction')

    def test_both_subject_directions_and_decimal_comma_are_checked(self):
        p=self.packet()
        for field,wording in [
            ('comparison.home','В среднем хозяева пропускают больше, чем гости. Это характеристика общей статистики.'),
            ('comparison.away','Бета пропускает меньше (2,67), чем Альфа. Так описаны общие показатели команд.')]:
            with self.subTest(field=field):
                issues=semantic_issues(wording,field,p,['F1','F2'],'home')
                self.assertIn('comparison_direction',[i['rule'] for i in issues])

    def test_short_subjects_in_prose_require_an_unambiguous_prefix_of_a_participant(self):
        self.event.update(home='Альфа Сити (до21)',away='Бета Таун (до21)')
        # The same subject value can occur in both filters while the opponent's
        # differs. The cited overall facts resolve that ambiguity.
        self.stats['sections'][1]['text']='1.67\nЗабитые мячи\n2.67\n2.33\nГолов пропущено\n1.67'
        p=self.packet()
        issues=semantic_issues('Альфа пропускает больше (2.33), чем Бета. Сравниваются общие показатели.',
            'comparison.home',p,['F1','F2'],'home')
        self.assertIn('comparison_direction',[i['rule'] for i in issues])
        self.event.update(home='Юнайтед Сити',away='Юнайтед Таун');p=self.packet()
        self.assertEqual(semantic_issues('Юнайтед пропускает больше (2.33), чем Юнайтед. Команда не уточнена.',
            'comparison.home',p,['F1','F2'],'home'),[])

    def test_ambiguous_scope_correct_venue_and_negated_comparisons_are_not_rejected(self):
        p=self.packet()
        for wording in [
            'Хозяева пропускают больше, чем гости. Это сравнение без указания фильтра.',
            'В гостях Бета пропускает меньше (1.67), чем Альфа. Сопоставляются показатели по месту игры.',
            'Нельзя утверждать, что в среднем Альфа пропускает больше, чем Бета. Это было бы ошибкой.',
            'Дома Альфа пропускает больше, чем в среднем. Это другой фильтр источника.']:
            with self.subTest(wording=wording):self.assertEqual(semantic_issues(wording,'comparison.home',p,['F1','F2','F3','F4'],'home'),[])

    def test_different_units_duplicate_views_and_truncated_rates_are_not_compared(self):
        p=self.packet();facts=[f for f in p['facts'] if f['kind']=='statistics'];seen=[]
        for change in ('total','duplicate','truncated'):
            changed=deepcopy(facts)
            if change=='total':
                for f in changed:f['measurement']='total'
            elif change=='duplicate':changed+=deepcopy(changed)
            else:
                for f in changed:f['truncated']=True
            with self.subTest(change=change):
                seen.clear();add_comparisons(changed,self.event,lambda *args,**kwargs:seen.append(args))
                self.assertEqual(seen,[])

    def test_form_uses_same_number_of_latest_known_games_and_separates_cup(self):
        self.history([(0,0),(1,0)],[(2,1),(1,1),(0,2)])
        self.rows.append({'home':self.event['home'].casefold(),'away':'кубковый соперник',
            'goals':(0,5),'at':self.at-timedelta(hours=2),'same_competition':False})
        p=self.packet();f=next(f for f in p['facts'] if f.get('comparison_type')=='recent_results')
        self.assertEqual(f['sample_size'],2)
        self.assertEqual(f['outcome_counts']['home'],{'win':1,'draw':1,'loss':0})
        self.assertEqual(f['outcome_counts']['home'],f['outcome_counts']['away'])
        self.assertIn('Набор исходов одинаков',f['text'])
        source={x['id']:x for x in p['facts']}
        self.assertTrue(all(source[i]['scope']=='same_competition' for i in f['source_fact_ids']))

    def test_unqualified_better_form_with_equal_results_requires_repair(self):
        self.history([(0,0),(1,0)],[(2,1),(1,1),(0,2)])
        p=self.packet();f=next(f for f in p['facts'] if f.get('comparison_type')=='recent_results')
        value=reply(p,'skip');value['reason']['fact_ids']=f['source_fact_ids']
        value['reason']['text']='Альфа имеет лучшую недавнюю форму в лиге, но противоположные показатели мешают решиться на ставку.'
        self.error(p,value,'equal_recent_outcomes_do_not_establish_better_form')
        value['reason']['text']='Последние сохранённые результаты команд сходны. Недостаток сведений о соперниках мешает принять решение о ставке.'
        validate_reply(value,p)
        value['reason']['text']='Нельзя утверждать, что Альфа имеет лучшую недавнюю форму. Сходство результатов ограничивает выбор ставки.'
        validate_reply(value,p)

    def test_comparison_facts_can_ground_both_sides_without_inventing_more_support(self):
        self.history([(0,0),(1,0)],[(2,1),(1,1)])
        p=self.packet();f=next(f for f in p['facts'] if f.get('comparison_type')=='recent_results')
        v=reply(p,'skip')
        for a in v['comparison'].values():a['fact_ids']=[f['id']]
        v['counterargument']['fact_ids']=[f['id']];v['reason']['fact_ids']=[f['id']]
        self.assertEqual(validate_reply(v,p)['decision'],'skip')

    def test_score_margin_is_relative_to_participant_and_one_goal_loss_is_not_large(self):
        self.history([(3,4)],[(1,0)])
        p=self.packet();history=next(f for f in p['facts'] if f.get('kind')=='history' and f['side']=='home')
        self.assertEqual(history['matches'][0]['difference'],-1)
        self.assertIn('разница мячей для этой команды: -1',history['text'])
        value=reply(p,'skip');value['counterargument']['fact_ids']=[history['id']]
        for wording in ('Альфа потерпела крупное поражение 3:4. Этот результат мешает довериться её защите.',
                        'Альфа недавно допустила крупный проигрыш. Это служит встречным доводом к выбору хозяев.'):
            value['counterargument']['text']=wording
            with self.subTest(wording=wording):self.error(p,value,'large_defeat_requires_three_goal_margin')
        value['counterargument']['text']='Альфа проиграла с разницей в один мяч. Этот результат сам по себе ещё не устанавливает слабость команды.'
        validate_reply(value,p)

    def test_three_goal_loss_and_explicit_denial_are_allowed(self):
        self.history([(0,3)],[(1,0)])
        p=self.packet();history=next(f for f in p['facts'] if f.get('kind')=='history' and f['side']=='home')
        self.assertEqual(semantic_issues('Альфа потерпела крупное поражение 0:3. Это факт показанной истории.',
            'counterargument',p,[history['id']],'home'),[])
        self.assertEqual(semantic_issues('Альфа одержала победу 2:1 и потерпела крупное поражение 0:3.',
            'counterargument',p,[history['id']],'home'),[])
        self.assertEqual(semantic_issues('Альфа потерпела крупное поражение 0:3 и одержала победу 2:1.',
            'counterargument',p,[history['id']],'home'),[])
        self.history([(3,4)],[])
        p=self.packet()
        self.assertEqual(semantic_issues('Не было крупного поражения 3:4: разница составила один мяч.',
            'counterargument',p,[history['id']],'home'),[])

    def test_selected_team_can_be_preserved_while_skipping_a_bet(self):
        p=self.packet();v=reply(p,'skip')
        v['reason']['text']='Противоречие данных не позволяет выбрать сторону. Поэтому решение по матчу следует отложить.'
        self.error(p,v,'chosen_preference_is_not_absence_of_preference')
        v['reason']['text']='Предпочтение хозяев сохраняется, но данных недостаточно для решения о ставке. Не хватает сведений о соперниках.'
        validate_reply(v,p)
        v['reason']['text']='Предпочтение хозяев сохраняется, но пока невозможно выбрать сторону для ставки из-за недостатка сведений о соперниках.'
        validate_reply(v,p)
        v['preference']=None;v['reason']['code']='no_clear_preference'
        v['reason']['text']='Противоречие данных не позволяет выбрать сторону. Поэтому решение по матчу следует отложить.'
        validate_reply(v,p)

    def test_prices_do_not_establish_why_a_bookmaker_set_them(self):
        p=self.packet();v=reply(p)
        v['market_context']['text']='Коэффициент 4.1 на гостей учитывает их недостатки в защите. Так объясняется положение команд в линии.'
        self.error(p,v,'bookmaker_pricing_causes_are_not_observed')
        v['market_context']['text']='Коэффициент 1.63 на хозяев ниже цены гостей. Причины назначения этой цены из доступных данных неизвестны.'
        validate_reply(v,p)
        v['market_context']['text']='Нельзя утверждать, что коэффициент 4.1 учитывает состав. Мы наблюдаем только цену, а не причины её назначения.'
        validate_reply(v,p)

    def test_number_present_elsewhere_is_a_citation_error_not_a_fabricated_number(self):
        p=self.packet();p['facts'].append({'id':'F99','kind':'statistics','side':'home','text':'В таблице Альфа забила 8 голов.'})
        v=reply(p);v['comparison']['home']['text']='Альфа забила 8 голов по сохранённой таблице. Полнота и фильтры этой таблицы ограничивают вывод.'
        error=self.error(p,v,'numbers_from_cited_facts_required')
        self.assertEqual(str(error),'verdict_fact_reference')
        self.assertEqual(error.details['numbers_elsewhere_in_input'],{'8':['F99']})
        self.assertNotIn('F99',v['comparison']['home']['fact_ids'])
        v['comparison']['home']['fact_ids'].append('F99')
        validate_reply(v,p)
        v['comparison']['home']['text']=v['comparison']['home']['text'].replace('8','9999')
        error=self.error(p,v,'numbers_from_cited_facts_required')
        self.assertEqual(str(error),'unsupported_number')


if __name__=='__main__':unittest.main()
