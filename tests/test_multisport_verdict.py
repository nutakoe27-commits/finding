"""Sport/market boundaries, historical input isolation and admin settlement."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.markets import validate_event_markets
from matchlab.neural_verdict import build_packet, document_quote, generate, system_prompt, validate_reply
from matchlab.storage import Repository
from matchlab.verdict_outcomes import run
from matchlab.verdict_sports import PROFILES, market_matches, result_score
from matchlab.multisport_evidence import archive_results
from account_helpers import signed_in
from outcome_helpers import forecast
from sports_fixtures import document as sports_document


SPORTS = ('hockey', 'basketball', 'tennis', 'volleyball', 'table_tennis')
SCORES = {'hockey': '3:1 (1:0 1:1 1:0)', 'basketball': '90:80 (20:20 20:20 25:20 25:20)',
          'tennis': '2:0 (6:3 6:4)', 'volleyball': '3:0 (25:20 25:21 25:19)',
          'table_tennis': '3:0 (11:6 11:7 11:8)'}
SLUGS = {'hockey': 'xokkej', 'basketball': 'basketbol', 'tennis': 'tennis',
         'volleyball': 'volejbol', 'table_tennis': 'nastolnyj_tennis'}


def fixture(sport, now):
    p = PROFILES[sport]
    name = 'Исход 1X2 (осн.время)' if p.draw else 'Исход 12 (с ОТ)' if sport == 'basketball' else 'Исход 12 (матч)'
    outcomes = [('П1', 1.9), ('X', 3.3), ('П2', 2.1)] if p.draw else [('П1', 1.9), ('П2', 2.1)]
    raw = sports_document({'sport': sport, 'label': p.label, 'slug': SLUGS[sport], 'id': '91001',
        'home': 'Альфа', 'away': 'Бета', 'competition': 'Тестовый турнир',
        'markets': [{'name': name, 'period': None, 'outcomes': outcomes}]}, now)
    return validate_event_markets(raw)


def answer(packet):
    ids = {s: next(f['id'] for f in packet['facts'] if f['kind'] in ('history', 'statistics') and f.get('side') == s) for s in ('home', 'away')}
    market = next(f['id'] for f in packet['facts'] if f['kind'] == 'market')
    return {'preference': 'home', 'decision': 'bet', 'stake_level': 'small',
        'comparison': {s: {'fact_ids': [ids[s]], 'text': 'Сохранённая история участника даёт основу для сравнения, хотя полнота календаря не подтверждена.'} for s in ids},
        'counterargument': {'fact_ids': [ids['away']], 'text': 'Результаты соперника показывают сопротивление, поэтому выбранная сторона может не реализовать преимущество.'},
        'market_context': {'fact_ids': [market], 'text': 'Исходные коэффициенты описывают цену выбора и сами по себе не подтверждают надёжность решения.'},
        'reason': {'code': 'supporting_evidence', 'fact_ids': list(ids.values()), 'text': 'Сопоставление подтверждённых результатов даёт предпочтение первому участнику с учётом встречного довода.'}}


class MultisportVerdictTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name); self.repo = Repository(self.root/'matchlab.sqlite3'); self.repo.initialize()
        self.now = datetime.now(timezone.utc)
        self.settings = Settings(self.root, 'http://localhost:9/v1', 'model', event_analysis_mode='neural-verdict')

    def observation(self, sport, home, away, score=None, at=None, observed=None, received=None, cursor=1):
        p = PROFILES[sport]; at = at or self.now-timedelta(days=1)
        observed = observed or self.now-timedelta(minutes=10); received = received or observed
        value = {'date': at.isoformat(), 'member1': home, 'member2': away,
                 'championship': 'Тестовый турнир', 'score': score or SCORES[sport]}
        with self.repo.connection() as db:
            db.execute('INSERT INTO archive_versions VALUES (?,?,?,?,?,?,?,?)',
                ('test', cursor, f'row-{cursor}', p.archive_id, p.label, observed.isoformat(), received.isoformat(), json.dumps(value)))
        return value

    def packet(self, sport):
        self.observation(sport, 'Альфа', 'Соперник А', cursor=PROFILES[sport].archive_id*10)
        self.observation(sport, 'Бета', 'Соперник Б', cursor=PROFILES[sport].archive_id*10+1)
        d = fixture(sport, self.now)
        return build_packet(self.repo, d, None, self.now, document_quote(d, self.now))

    def test_only_the_exact_full_event_market_can_supply_prices(self):
        for sport in SPORTS:
            with self.subTest(sport=sport):
                d = fixture(sport, self.now); original = deepcopy(d['markets'][0])
                self.assertIsNotNone(document_quote(d, self.now))
                for period in ('1 сет', '1 период', '1 четверть', '1 тайм', 'Серия'):
                    changed = deepcopy(original); changed['period'] = period
                    self.assertFalse(market_matches(changed, sport), period)
                for field, value in [('parameter', '+1.5'), ('is_stale', True), ('outcomes_complete', False)]:
                    changed = deepcopy(original); changed[field] = value
                    self.assertFalse(market_matches(changed, sport))
                d['markets'][0]['outcomes'][0]['state'] = 'suspended'
                self.assertIsNone(document_quote(d, self.now))
        market = fixture('basketball', self.now)['markets'][0]
        market['name'] = 'Победитель'; self.assertFalse(market_matches(market, 'basketball'))
        market = fixture('hockey', self.now)['markets'][0]
        market['name'] += ' с ОТ'; self.assertFalse(market_matches(market, 'hockey'))

    def test_all_five_sports_generate_from_compact_bound_history_without_math(self):
        for sport in SPORTS:
            with self.subTest(sport=sport), patch('matchlab.chances.assess', side_effect=AssertionError('math called')):
                packet = self.packet(sport)
                self.assertFalse(packet['policy']['force_skip'], packet['limitations'])
                self.assertLess(len(json.dumps(packet, ensure_ascii=False).encode()), 16000)
                self.assertEqual(set(packet['quote']['odds']), {'home', 'draw', 'away'} if sport == 'hockey' else {'home', 'away'})
                self.assertIn(PROFILES[sport].unit, system_prompt(packet))
                verdict, generation = generate(self.settings, packet, lambda *_: json.dumps(answer(packet)), historical=True)
                self.assertEqual((verdict['engine'], verdict['decision'], generation['model_calls']), ('qwen', 'bet', 1))
                self.assertEqual(verdict['scope'], PROFILES[sport].scope)
                self.assertEqual(verdict['stake_percent'], .25)

    def test_future_corrections_receipts_and_wrong_sport_never_enter_history(self):
        self.observation('tennis', 'Альфа', 'Соперник', cursor=1)
        for cursor, changed in [(2, {'observed': self.now+timedelta(seconds=1)}), (3, {'received': self.now+timedelta(seconds=1)}), (4, {'at': self.now+timedelta(days=1)})]:
            self.observation('tennis', 'Бета', 'Соперник', cursor=cursor, **changed)
        self.observation('table_tennis', 'Бета', 'Соперник', cursor=5)
        d = fixture('tennis', self.now)
        packet = build_packet(self.repo, d, None, self.now, document_quote(d, self.now))
        self.assertTrue(packet['policy']['force_skip'])
        self.assertFalse(any(f.get('side') == 'away' for f in packet['facts']))

    def test_wrong_sport_quote_and_no_history_force_clear_skip_without_a_call(self):
        d = fixture('tennis', self.now); quote = document_quote(d, self.now)
        quote['sport'] = 'table_tennis'
        packet = build_packet(self.repo, d, None, self.now, quote)
        verdict, generation = generate(self.settings, packet, lambda *_: self.fail('called model'))
        self.assertEqual((verdict['preference'], verdict['decision'], generation['model_calls']), (None, 'skip', 0))

    def test_two_way_markets_reject_draw_risk_and_retry_invalid_answer(self):
        packet = self.packet('tennis'); bad = answer(packet)
        bad['decision'] = 'skip'; bad['stake_level'] = None; bad['reason']['code'] = 'draw_risk'
        with self.assertRaisesRegex(ValueError, 'verdict_reason'):
            validate_reply(bad, packet)
        calls = []
        def model(*_):
            calls.append(1)
            return json.dumps(bad if len(calls) < 3 else answer(packet))
        verdict, generation = generate(self.settings, packet, model, historical=True)
        self.assertEqual((verdict['engine'], generation['model_calls']), ('qwen', 3))

    def test_verified_widget_history_needs_matching_anchor_sport_and_final_status(self):
        d = fixture('tennis', self.now)
        targets = {'home': {'_id': 11, 'name': 'Альфа'}, 'away': {'_id': 22, 'name': 'Бета'}}
        groups = {}
        for i, side in enumerate(('home', 'away')):
            row = {'_id': 800+i, '_sid': 5, 'status': {'_id': 100}, 'date': (self.now-timedelta(days=1)).isoformat(),
                'teams': {'home': targets[side], 'away': {'_id': 33, 'name': 'Соперник'}},
                'result': {'home': 2, 'away': 0}, 'competition': d['competition']}
            groups[side] = [{'verified_matches': [row]}]
        stats = {'statistics_id': '700', 'match_phase': 'prematch', 'structured_feed': {
            'statistics_id': '700', 'match': {'_id': 700, '_sid': 5, 'teams': targets}, 'participants': groups}}
        packet = build_packet(self.repo, d, stats, self.now, document_quote(d, self.now))
        self.assertFalse(packet['policy']['force_skip'], packet['limitations'])
        self.assertEqual(len([f for f in packet['facts'] if f['kind']=='history']), 2)
        for mutation in ('anchor', 'sport', 'status', 'unverified', 'future', 'retired'):
            changed = deepcopy(stats); feed = changed['structured_feed']; row = feed['participants']['away'][0]['verified_matches'][0]
            if mutation == 'anchor':feed['match']['_id'] = 999
            if mutation == 'sport':row['_sid'] = 20
            if mutation == 'status':row.pop('status')
            if mutation == 'unverified':feed['participants']['away'][0]['unverified_matches'] = feed['participants']['away'][0].pop('verified_matches')
            if mutation == 'future':row['date'] = (self.now+timedelta(days=1)).isoformat()
            if mutation == 'retired':row['retired'] = True
            with self.subTest(mutation=mutation):
                guarded = build_packet(self.repo, d, changed, self.now, document_quote(d, self.now))
                self.assertTrue(guarded['policy']['force_skip'])

    def test_api_calls_neural_engine_for_supported_sport_and_retains_market_scope(self):
        self.packet('tennis')
        self.repo.import_event_markets(fixture('tennis', self.now))
        def model(settings, messages):
            return json.dumps(answer(json.loads(messages[1]['content'])))
        with TestClient(create_app(self.settings)) as client, patch('matchlab.qwen._completion', side_effect=model) as calls:
            signed_in(client, self.repo)
            response = client.post('/api/bookmakers/events/91001/explain', json={})
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json(); verdict = payload['context']['neural_verdict']
            self.assertEqual((payload['analysis_type'], verdict['engine'], verdict['scope']), ('neural_verdict', 'qwen', 'match_winner'))
            self.assertEqual(calls.call_count, 1)
            self.assertIn('Победа в матче', payload['text'])

    def test_live_and_unknown_sports_do_not_spend_model_tokens(self):
        for sport, phase in [('tennis', 'live'), ('hockey', 'finished'), ('boxing', 'prematch')]:
            d = fixture('tennis', self.now); d.update(sport=sport, phase=phase)
            packet = build_packet(self.repo, d, None, self.now)
            verdict, generation = generate(self.settings, packet, lambda *_: self.fail('model called'))
            self.assertEqual((verdict['decision'], verdict['preference'], generation['model_calls']), ('skip', None, 0))

    def test_final_result_parsing_distinguishes_regulation_overtime_and_sets(self):
        examples = [
            ('hockey', '4:3 (1:2 0:1 2:0 1:0)', (3, 3)),
            ('hockey', '4:3', None), ('hockey', '3:1 (1:0 1:1 1:0)', (3, 1)),
            ('hockey', '4:2 (1:0 1:0 1:1 1:1)', None),
            ('basketball', '100:92 (41:32 42:51 17:9)', (100, 92)),
            ('basketball', '83:83', None), ('basketball', '100:92 (41:32 42:51)', None),
            ('tennis', '1:2 (6:7 6:4 5:10)', (1, 2)),
            ('tennis', '2:0 (6:3 6:4)', (2, 0)), ('tennis', '1:0 (6:4)', None),
            ('tennis', '2:0 (6:4 3:1)', None), ('tennis', '2:0 отказ', None),
            ('volleyball', '3:2 (25:20 20:25 25:20 20:25 15:12)', (3, 2)),
            ('volleyball', '2:0', None), ('table_tennis', '3:1 (11:8 8:11 11:9 12:10)', (3, 1)),
            ('table_tennis', '3:0 (11:8 11:7 8:6)', None),
            ('table_tennis', '3:0 (11:8 11:7 14:5)', None)]
        for sport, score, expected in examples:
            with self.subTest(sport=sport, score=score):
                self.assertEqual(result_score(score, sport), expected)

    def saved(self, sport, score, keyname):
        kickoff = self.now-timedelta(days=1)
        e = {'source_event_id': keyname, 'sport': sport, 'home': 'Альфа', 'away': 'Бета',
             'competition': 'Тестовый турнир', 'phase': 'prematch', 'kickoff_at': kickoff.isoformat()}
        payload = forecast(self.repo, keyname, event=e, kickoff=kickoff, score=None)
        p = PROFILES[sport]; context = payload['context']; q = context['neural_input']['quote']
        q.update(sport=sport, scope=p.scope)
        if not p.draw:q['odds'].pop('draw')
        context['neural_verdict'].update(scope=p.scope)
        with self.repo.connection() as db:
            db.execute('UPDATE event_analyses SET payload=? WHERE id=?', (json.dumps(payload), keyname))
            data = {'member1': e['home'], 'member2': e['away'], 'date': e['kickoff_at'], 'championship': e['competition'], 'score': score}
            db.execute('INSERT INTO archive_latest_results VALUES (?,?,?,?,?,?,?,?,?,?)',
                (keyname, p.archive_id, p.label, e['kickoff_at'], e['home'], e['away'], score, self.now.isoformat(), self.now.isoformat(), json.dumps(data)))
        return payload

    def test_admin_settles_each_sport_independently_including_regulation_draw_loss(self):
        for sport in SPORTS:
            self.saved(sport, SCORES[sport], sport)
        forecast(self.repo, 'football', event={'source_event_id': 'football', 'sport': 'football',
            'home': 'Альфа', 'away': 'Бета', 'competition': 'Тестовый турнир', 'phase': 'prematch',
            'kickoff_at': (self.now-timedelta(days=1)).isoformat()}, kickoff=self.now-timedelta(days=1), score='0:2')
        report = run(self.repo, now=self.now)
        self.assertEqual((report['metrics']['fixtures'], report['metrics']['settled'], report['metrics']['bet_wins']), (6, 6, 5))
        self.assertEqual(run(self.repo, sport='tennis', now=self.now)['metrics']['fixtures'], 1)
        with self.repo.connection() as db:
            data = json.loads(db.execute("SELECT payload FROM archive_latest_results WHERE result_key='hockey'").fetchone()[0])
            data['score'] = '4:3 (1:2 0:1 2:0 1:0)'
            db.execute("UPDATE archive_latest_results SET score=?,payload=? WHERE result_key='hockey'", (data['score'], json.dumps(data)))
        hockey = run(self.repo, sport='hockey', now=self.now)['matches'][0]
        self.assertEqual((hockey['actual_score'], hockey['decision_result']), ('3:3', 'bet_lost'))

    def test_uninterpretable_finished_score_is_unresolved_instead_of_pending_forever(self):
        self.saved('tennis', '1:0 отказ', 'retired')
        row = run(self.repo, now=self.now)['matches'][0]
        self.assertEqual((row['state'], row['decision_result'], row['preference_hit']), ('unresolved', 'unsupported_result_scope', None))

    def test_api_capabilities_and_sport_filter_remain_admin_only_for_outcomes(self):
        self.saved('tennis', SCORES['tennis'], 'tennis')
        with TestClient(create_app(self.settings)) as client:
            self.assertIn('tennis', client.get('/api/status').json()['ai']['verdict_sports'])
            self.assertEqual(client.get('/api/admin/verdict-outcomes?sport=tennis').status_code, 401)
            repo = Repository(self.settings.database); signed_in(client, repo, role='admin')
            response = client.get('/api/admin/verdict-outcomes?sport=tennis')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['sport'], 'tennis')
            self.assertEqual(response.json()['metrics']['fixtures'], 1)
            self.assertEqual(client.get('/api/admin/verdict-outcomes?sport=unknown').status_code, 422)
