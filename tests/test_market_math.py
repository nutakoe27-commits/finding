import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from matchlab.config import Settings
from matchlab.market_math import normalized_1x2
from matchlab.qwen import explain_event


def market():
    return {'key': 'first_half', 'name': '1 тайм исход 1X2', 'period': None,
            'parameter': None, 'outcomes_complete': True, 'is_stale': False,
            'observed_at': '2026-11-09T05:58:56Z',
            'outcomes': [{'name': '1', 'parameter': None, 'odds': 10, 'state': 'open'},
                         {'name': 'X', 'parameter': None, 'odds': 1.87, 'state': 'open'},
                         {'name': '2', 'parameter': None, 'odds': 2.1, 'state': 'open'}]}


class MarketMathTests(unittest.TestCase):
    def test_given_first_half_prices_show_normalized_market_weights(self):
        result = normalized_1x2(market())
        self.assertAlmostEqual(result['weights']['1'], .0900130653)
        self.assertAlmostEqual(result['weights']['X'], .4813532904)
        self.assertAlmostEqual(result['weights']['2'], .4286336443)
        self.assertAlmostEqual(result['overround'], .1109498345)
        self.assertAlmostEqual(sum(result['weights'].values()), 1.0)
        context = {'event': {'phase': 'live', 'live_score': '0:0', 'live_clock': "1Т 27'"},
                   'request_time': {}, 'snapshot': {}, 'selection': {'market_key': 'first_half'},
                   'markets': [market()], 'archive_history': {'state': 'empty'}, 'web_research': {}}
        with patch('matchlab.qwen._completion', side_effect=AssertionError('model called')):
            text = explain_event(Settings(Path('/tmp')), context)
        for part in ('1 тайм исход 1X2', '1 — хозяева: 9,0%', 'X — ничья: 48,1%', '2 — гости: 42,9%',
                     '11,1 п.п.', 'не независимый прогноз', "1Т 27'"):
            self.assertIn(part, text)

    def test_ambiguous_or_incomplete_markets_are_not_normalized(self):
        cases = []
        for field, value in [('name', 'Двойной шанс'), ('name', '1X2 с форой'),
                             ('parameter', '1'), ('outcomes_complete', False)]:
            value_case = market()
            value_case[field] = value
            cases.append(value_case)
        value_case = market()
        value_case['outcomes'][1]['state'] = 'suspended'
        cases.append(value_case)
        value_case = market()
        value_case['outcomes'][1]['name'] = '1'
        cases.append(value_case)
        value_case = market()
        value_case['outcomes'][0]['odds'] = float('nan')
        cases.append(value_case)
        value_case = market()
        value_case['outcomes'].append(deepcopy(value_case['outcomes'][0]))
        cases.append(value_case)
        value_case = market()
        del value_case['outcomes_complete']
        cases.append(value_case)
        for case in cases:
            with self.subTest(case=case):
                self.assertIsNone(normalized_1x2(case))
