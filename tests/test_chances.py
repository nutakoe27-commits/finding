"""Probability invariants, chronological identity, settlement and safe fallback."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

import numpy as np
from scipy.stats import poisson

from matchlab.chances import fit_goals,goal_probabilities,market_probabilities,render_report,feed_sample
from matchlab import qwen
from matchlab.config import Settings
from matchlab.chances_audit import evaluate

NOW=datetime.now(timezone.utc)


def rows():
    result=[]
    for i in range(80):
        result.append({'home':'a' if i%2==0 else 'b','away':'b' if i%2==0 else 'a',
                       'goals':(i%4,(i+1)%3),'at':NOW-timedelta(days=i+1)})
    return result


def market(name,labels,period='осн.время',parameter=None):
    return {'key':name,'name':name,'period':period,'parameter':parameter,'outcomes_complete':True,
        'outcomes':[{'key':label,'name':label,'state':'open','odds':2.0} for label in labels]}


class ChanceTests(unittest.TestCase):
    def test_forward_audit_excludes_late_predictions_and_duplicate_users(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE event_analyses(generated_at TEXT,payload TEXT)')
            db.execute('CREATE TABLE archive_latest_results(sport_id INTEGER,start_at TEXT,payload TEXT)')
            event={'source_event_id':'123','sport':'football','phase':'prematch','kickoff_at':NOW.isoformat(),'home':'A','away':'B','competition':'League'}
            chances={'markets':[{'market_key':'m','market':'Исход 1X2','period':'осн.время','outcome':label,'basis':'history','probability':p} for label,p in [('П1',.5),('X',.2),('П2',.3)]]}
            saved={'generated_at':(NOW-timedelta(hours=1)).isoformat(),'context':{'event':event,'probability_assessment':chances}}
            for _ in range(2): db.execute('INSERT INTO event_analyses VALUES (?,?)',(saved['generated_at'],json.dumps(saved)))
            late=deepcopy(saved);late['generated_at']=(NOW+timedelta(hours=1)).isoformat()
            db.execute('INSERT INTO event_analyses VALUES (?,?)',(late['generated_at'],json.dumps(late)))
            self.assertEqual(evaluate(db)['evaluated'],0)
            db.execute('INSERT INTO archive_latest_results VALUES (1,?,?)',(NOW.isoformat(),json.dumps({'member1':'A','member2':'B','championship':'League','score':'2:1'})))
            report=evaluate(db)
        self.assertEqual(report['evaluated'],1)
        self.assertAlmostEqual(report['brier_3way'],.38)
    def test_shrinkage_produces_finite_distribution_and_requires_both_teams(self):
        fit=fit_goals(rows(),'a','b',NOW)
        self.assertIsNotNone(fit)
        grid,details=fit
        self.assertAlmostEqual(float(grid.sum()),1)
        self.assertTrue(np.isfinite(grid).all())
        self.assertIsNone(fit_goals(rows(),'a','missing',NOW))
        self.assertIsNone(fit_goals(rows()[:4],'a','b',NOW))
        self.assertEqual(len(details),2)
        repeated=rows()+rows()
        # Team counts are deduplicated across sources.
        self.assertEqual(fit_goals(repeated,'a','b',NOW)[1],details)

    def test_score_grid_outcomes_and_refunds_are_consistent(self):
        grid=np.outer(poisson.pmf(np.arange(40),1.4),poisson.pmf(np.arange(40),.9));grid/=grid.sum()
        outcome=goal_probabilities(market('Исход 1X2',['П1','X','П2']),grid)
        self.assertAlmostEqual(sum(v['probability'] for v in outcome.values()),1)
        double=goal_probabilities(market('Двойной шанс',['1X','12','X2']),grid)
        self.assertAlmostEqual(double['1X']['probability'],outcome['П1']['probability']+outcome['X']['probability'])
        total=goal_probabilities(market('Тотал',['Больше','Меньше'],parameter='2'),grid)
        self.assertAlmostEqual(total['Больше']['probability']+total['Меньше']['probability']+total['Больше']['push_probability'],1)
        self.assertEqual(goal_probabilities(market('Тотал',['Больше','Меньше'],parameter='2.25'),grid),{})
        self.assertEqual(goal_probabilities(market('Исход 1X2',['П1','X','П2'],period='1 тайм'),grid),{})
        self.assertEqual(goal_probabilities(market('Тотал угловых',['Больше','Меньше'],parameter='2.5'),grid),{})

    def test_nonexclusive_double_chance_and_alternate_totals_are_not_normalized(self):
        self.assertEqual(market_probabilities(market('Двойной шанс',['1X','12','X2'])),{})
        self.assertEqual(market_probabilities(market('Тотал',['б 1.5','б 2.5','м 1.5','м 2.5'])),{})
        self.assertEqual(market_probabilities(market('Фора',['П1','П2'],parameter='1.5')),{})
        values=market_probabilities(market('Победитель',['П1','П2']))
        self.assertAlmostEqual(sum(v['probability'] for v in values.values()),1)

    def test_feed_requires_finished_time_competition_and_verified_id(self):
        row={'_id':1,'status':'finished','date':(NOW-timedelta(days=1)).isoformat(),
             'competition':'League','result':{'home':2,'away':1},'teams':{'home':{'_id':10}}}
        context={'event':{'competition':'League'},'team_statistics':{'structured_feed':{'statistics_id':'999',
            'match':{'teams':{'home':{'_id':10}}},'participants':{'home':[{'verified_matches':[row]}]}}}}
        self.assertEqual(feed_sample(context,'home',NOW)[0]['gf'],2)
        for field,value in [('status','scheduled'),('date',(NOW+timedelta(days=1)).isoformat()),('competition','Other'),('_id',999)]:
            modified=deepcopy(context);modified['team_statistics']['structured_feed']['participants']['home'][0]['verified_matches'][0][field]=value
            self.assertEqual(feed_sample(modified,'home',NOW),[])

    def test_short_report_omits_empty_limitations_and_handles_draw(self):
        assessment={'winner':{'participant':'Ничья','probability':.4,'basis':'history','market':'Исход 1X2','period':'осн.время'},
                    'markets':[],'facts':['Первый факт','Второй факт'],'limitations':[],'finished':False}
        text=render_report(assessment)
        self.assertEqual(len(text.split('\n\n')),3)
        self.assertIn('Ничья — 40,0%',text)
        self.assertNotIn('Ограничения:',text)
        self.assertNotIn('Пуассон',text)

    def test_qwen_selects_facts_but_cannot_change_numbers_and_has_safe_fallback(self):
        settings=Settings(Path('/tmp'),'http://127.0.0.1:9/v1','fixture')
        assessment={'winner':{'participant':'Team A','probability':.7,'basis':'history','market':'Исход 1X2','period':'осн.время'},
                    'markets':[],'facts':['Проверенный факт'],'limitations':[],'finished':False}
        context={'team_statistics':{'state':'ok'},'probability_assessment':assessment,'markets':[market('Исход 1X2',['П1','X','П2'])]}
        with patch('matchlab.qwen._completion',return_value='{"fact_indices":[0],"probability":0.99}'):
            text=qwen.explain_event(settings,context)
        self.assertIn('70,0%',text);self.assertNotIn('99',text)
        with patch('matchlab.qwen._completion',side_effect=ValueError('Сервер Qwen не ответил вовремя.')),self.assertLogs('matchlab.qwen') as logs:
            text=qwen.explain_event(settings,context)
        self.assertEqual(context['ai_generation']['reason'],'timeout')
        self.assertIn('70,0%',text);self.assertIn('расчётный отчёт',text)
        self.assertIn('timeout',logs.output[0])
        with patch('matchlab.qwen._completion',return_value='{"fact_indices":[999]}'):
            text=qwen.explain_event(settings,context)
        self.assertIn('Проверенный факт',text)


if __name__=='__main__': unittest.main()
