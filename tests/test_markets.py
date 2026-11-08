from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.markets import FORMAT, validate_event_markets
from matchlab.storage import Repository
from matchlab.winline_markets import distinguish_market_sections, exclude_invalid_market_labels, primary_quote
from matchlab.winline_browser import event_statistics_link

FROZEN = datetime(2026, 11, 8, 9, 30, tzinfo=timezone.utc)
KEY = 'test-market-receiver-only-1234567890'


def example():
    # Prices/labels are a minimal projection of the user's public event report.
    # The observation clock and completeness flags are synthetic test inputs.
    def outcomes(values):
        return [{'name': name, 'odds': price, 'state': 'open'} for name, price in values]
    return {'format': FORMAT, 'source': 'winline', 'sport': 'football', 'source_event_id': '17006967',
            'url': 'https://winline.ru/stavki/sport/futbol/angliya/apl/17006967',
            'home': 'Сандерленд', 'away': 'Челси', 'competition': 'Англия. Премьер-лига',
            'phase': 'prematch', 'kickoff_at': '2026-11-08T14:00:00+00:00',
            'observed_at': FROZEN.isoformat(), 'parser_version': 'test-public-market-projection',
            'markets': [
                {'name': 'Исход 1X2', 'group': 'Исход', 'period': 'осн.время',
                 'outcomes': outcomes([('П1', 3.9), ('X', 3.75), ('П2', 1.89)])},
                {'name': 'Исход 1X2', 'group': 'Исход', 'period': '1 тайм',
                 'outcomes': outcomes([('П1', 4.3), ('X', 2.29), ('П2', 2.42)])},
                {'name': 'Тотал (осн.время)', 'group': 'Тотал', 'period': 'осн.время',
                 'outcomes': outcomes([('б 2.5', 1.78), ('м 2.5', 2.02)])}],
            'coverage': {'expected_outcomes': 8, 'stable_passes': 2, 'hidden_groups': 0,
                         'scroll_exhausted': True, 'all_tab_selected': True}}


class MarketValidationTests(unittest.TestCase):
    def test_statistics_ids_are_observed_separately_from_market_ids(self):
        value=example()
        value['statistics_url']='https://winline.ru/stats/match/72334410'
        normalized=validate_event_markets(value)
        self.assertNotEqual(normalized['source_event_id'],'72334410')
        self.assertEqual(normalized['statistics_link_status'],'found')
        self.assertEqual(event_statistics_link(['https://winline.ru/stats/match/72334410?tab=h2h','https://www.winline.ru/stats/match/72334410']),('https://winline.ru/stats/match/72334410','found'))
        self.assertEqual(event_statistics_link(['https://winline.ru/stats/match/1','https://winline.ru/stats/match/2']),(None,'ambiguous'))
        self.assertEqual(event_statistics_link(['https://example.com/stats/match/72334410']),(None,'missing'))
        value['statistics_url']='https://example.com/stats/match/72334410'
        with self.assertRaises(ValueError): validate_event_markets(value)

    def test_periods_lines_and_prices_remain_separate(self):
        document = validate_event_markets(example())
        self.assertTrue(document['coverage']['complete'])
        self.assertEqual(document['coverage']['captured_outcomes'], 8)
        self.assertNotEqual(document['markets'][0]['key'], document['markets'][1]['key'])
        self.assertEqual(document['markets'][2]['outcomes'][0]['name'], 'б 2.5')

    def test_missing_all_tab_unparsed_rows_and_hidden_groups_cannot_claim_complete(self):
        for change in ({'all_tab_selected': False}, {'expected_outcomes': 9}, {'unparsed_outcomes': 1},
                       {'hidden_groups': 1}, {'scroll_exhausted': False}, {'stable_passes': 1}):
            document = example()
            document['coverage'].update(change, complete=True)
            self.assertFalse(validate_event_markets(document)['coverage']['complete'])

    def test_suspended_outcomes_are_retained_without_an_invented_price(self):
        document = example()
        document['markets'][0]['outcomes'][0].update(odds=None, state='suspended')
        normalized = validate_event_markets(document)
        self.assertIsNone(normalized['markets'][0]['outcomes'][0]['odds'])
        self.assertIsNone(primary_quote(normalized))

    def test_bad_prices_empty_sets_wrong_event_and_inconsistent_duplicates_reject(self):
        bad = []
        for odds in (True, float('nan'), -1, 0.5):
            value = example()
            value['markets'][0]['outcomes'][0]['odds'] = odds
            bad.append(value)
        value = example()
        value['markets'] = []
        bad.append(value)
        value = example()
        value['url'] = value['url'].replace('17006967', '17006968')
        bad.append(value)
        value = example()
        value['url'] = value['url'].replace('/futbol/', '/hokkey/')
        bad.append(value)
        value = example()
        duplicate = deepcopy(value['markets'][0])
        duplicate['outcomes'][0]['odds'] = 7.7
        value['markets'].append(duplicate)
        bad.append(value)
        for value in bad:
            with self.assertRaises(ValueError):
                validate_event_markets(value)

    def test_distinct_observed_variants_have_stable_independent_selection_keys(self):
        value=example()
        first=deepcopy(value['markets'][0]); second=deepcopy(first)
        first['variant_id']='a'*32; second['variant_id']='b'*32
        second['outcomes'][0]['odds']=7.7
        value['markets']=[first,second]
        document=validate_event_markets(value)
        self.assertEqual(len(document['markets']),2)
        self.assertNotEqual(document['markets'][0]['key'],document['markets'][1]['key'])
        value['markets'][0]['variant_id']='not-a-variant'
        with self.assertRaises(ValueError): validate_event_markets(value)

    def test_equal_or_subset_selection_sets_remain_ambiguous(self):
        base={'name':'Кол-во голов','group':'Тотал','period':'осн.время','parameter':None,'source_market_id':None}
        def outcomes(labels): return [{'name':name,'parameter':None,'source_outcome_id':None,'odds':2.0,'state':'open'} for name in labels]
        for second in (['0-1','2-3'],['0-1','2-3','4-5']):
            snapshot={'unparsed_outcomes':0,'hidden_groups':0,'markets':[
                {**base,'_scope':0,'outcomes':outcomes(['0-1','2-3'])},
                {**base,'_scope':1,'outcomes':outcomes(second)}]}
            distinguish_market_sections(snapshot)
            self.assertEqual(len(snapshot['markets']),1)
            self.assertNotIn('variant_id',snapshot['markets'][0])

    def test_variant_identity_ignores_header_order_and_price_changes(self):
        base={'name':'Кол-во голов','group':'Тотал','period':'осн.время','parameter':None,'source_market_id':None}
        def market(scope,names,price):
            return {**base,'_scope':scope,'outcomes':[{'name':name,'parameter':None,'source_outcome_id':None,'odds':price,'state':'open'} for name in names]}
        first={'markets':[market(0,['0-1','2-3','4-5','6+'],2),market(1,['0-1','2-3','4-6','7+'],3)],'unparsed_outcomes':0,'hidden_groups':0}
        second={'markets':[market(0,['7+','4-6','2-3','0-1'],9),market(1,['6+','4-5','2-3','0-1'],8)],'unparsed_outcomes':0,'hidden_groups':0}
        distinguish_market_sections(first); distinguish_market_sections(second)
        variants=lambda snapshot:{frozenset(outcome['name'] for outcome in market['outcomes']):market['variant_id'] for market in snapshot['markets']}
        self.assertEqual(variants(first),variants(second))

    def test_invalid_group_label_excludes_only_affected_market(self):
        base={'name':'Исход','group':'Футбол','period':'осн.время','parameter':None,'source_market_id':None,
              'outcomes':[{'name':'П1','parameter':None,'source_outcome_id':None,'odds':2.0,'state':'open'}]}
        snapshot={'markets':[deepcopy(base),{**deepcopy(base),'group':'х'*201}], 'unparsed_outcomes':0,'unsupported':[]}
        exclude_invalid_market_labels(snapshot)
        self.assertEqual(len(snapshot['markets']),1)
        self.assertEqual(snapshot['unparsed_outcomes'],1)
        self.assertEqual(snapshot['unsupported'][0]['field'],'market.group')
        self.assertEqual(snapshot['unsupported'][0]['length'],201)

    def test_primary_quote_uses_regular_time_only_and_canonical_source(self):
        document = validate_event_markets(example())
        quote = primary_quote(document)['snapshots'][0]
        self.assertEqual(quote['odds'], {'home': 3.9, 'draw': 3.75, 'away': 1.89})
        self.assertEqual(quote['source_event_id'], '17006967')
        document['markets'] = document['markets'][1:]
        self.assertIsNone(primary_quote(document))


class MarketReceiverTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(directory)
        self.client = self.stack.enter_context(TestClient(create_app(Settings(self.path, bookmaker_ingest_token=KEY)), client=('127.0.0.1', 1234)))
        for module in ('storage', 'api'):
            clock = self.stack.enter_context(patch(f'matchlab.{module}.datetime', wraps=datetime))
            clock.now.return_value = FROZEN
        self.repo = Repository(self.path / 'matchlab.sqlite3')
        self.headers = {'X-Matchlab-Collector-Key': KEY}

    def tearDown(self):
        self.stack.close()

    def post(self, body):
        return self.client.post('/api/bookmakers/events/ingest', json=body, headers=self.headers)

    def test_authorized_ingest_pagination_and_lazy_detail_preserve_training_revision(self):
        revision = self.repo.revision()
        self.assertEqual(self.client.post('/api/bookmakers/events/ingest', json=example()).status_code, 401)
        self.assertEqual(self.post(example()).status_code, 200)
        listing = self.client.get('/api/bookmakers/events?limit=1').json()
        self.assertEqual(listing['total'], 1)
        self.assertNotIn('markets', listing['events'][0])
        detail = self.client.get('/api/bookmakers/events/17006967/markets').json()
        self.assertEqual(len(detail['markets']), 3)
        self.assertTrue(detail['coverage']['complete'])
        self.assertEqual(self.repo.revision(), revision)

    def test_unchanged_refresh_does_not_archive_full_payload_but_updates_observation(self):
        self.post(example())
        later = example()
        later['observed_at'] = (FROZEN + timedelta(seconds=30)).isoformat()
        response = self.post(later).json()
        self.assertFalse(response['changed'])
        with self.repo.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bookmaker_event_markets').fetchone()[0], 1)
        detail = self.repo.event_markets('17006967')
        self.assertEqual(detail['changed_at'], '2026-11-08T09:30:00.000000+00:00')
        self.assertEqual(detail['observed_at'], '2026-11-08T09:30:30.000000+00:00')

    def test_empty_partial_older_and_future_batches_preserve_the_last_full_snapshot(self):
        self.post(example())
        previous = self.repo.event_markets('17006967')
        value = example()
        value['markets'] = []
        self.assertEqual(self.post(value).status_code, 422)
        partial = example()
        partial['markets'].pop()
        self.assertEqual(self.post(partial).json()['reason'], 'incomplete_snapshot')
        older = example()
        older['observed_at'] = (FROZEN - timedelta(minutes=1)).isoformat()
        self.assertEqual(self.post(older).json()['reason'], 'older_observation')
        future = example()
        future['observed_at'] = (FROZEN + timedelta(minutes=3)).isoformat()
        self.assertEqual(self.post(future).status_code, 422)
        self.assertEqual(self.repo.event_markets('17006967'), previous)

    def test_freshness_uses_the_oldest_time_in_the_capture_window(self):
        value = example()
        value['capture_started_at'] = (FROZEN - timedelta(minutes=4)).isoformat()
        self.post(value)
        event = self.client.get('/api/bookmakers/events').json()['events'][0]
        self.assertTrue(event['is_stale'])
        self.assertEqual(event['age_seconds'], 240)
