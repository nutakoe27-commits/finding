"""Live delivery, identity binding, chronology and AI integration contracts."""

import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from account_helpers import signed_in
from reasoning_helpers import completion as reasoning_completion,leaf_values,record_values

from matchlab import archive
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository
from winline.projection import FORMAT, market
from winline.storage import digest, encode

TOKEN = "archive-test-key-1234567890-1234567890"


def header(event_id="101", live=False, score="0:0", price=1.9):
    current = datetime.now(UTC)
    line = {
        "id": 10,
        "idEvent": int(event_id),
        "idTipMarket": 1,
        "koef": "",
        "isLive": int(live),
        "V": [price, 3.4, 4.2],
        "_display": {
            "valid": True,
            "name": "Исход 1X2 (осн.время)",
            "labels": ["1", "X", "2"],
        },
    }
    return {
        "event_id": event_id,
        "sport_id": 1,
        "sport_label": "Футбол",
        "members": ["Команда А", "Команда Б"],
        "competition": "Тестовая лига",
        "kickoff_at": (current + timedelta(days=-1 if live else 1)).isoformat(),
        "is_live": live,
        "score": score if live else None,
        "clock": "1Т 12'" if live else None,
        "period_scores": None,
        "observed_at": current.isoformat(),
        "primary": market(line, current.isoformat()),
        "expected_markets": 10,
    }


def rekey(row):
    row["result_key"] = digest(
        encode(
            [
                row["sport_id"],
                {
                    k: row["data"].get(k)
                    for k in ("idRadar", "date", "championship", "member1", "member2")
                },
            ]
        )
    )
    return row


def result(cursor=1, score="1:0", sport=1, observed=None):
    current = datetime.now(UTC)
    return rekey(
        {
            "cursor": cursor,
            "sport_id": sport,
            "sport_label": "Футбол" if sport == 1 else "Баскетбол",
            "observed_at": observed or (current - timedelta(seconds=10)).isoformat(),
            "data": {
                "idRadar": 999,
                "date": (current - timedelta(days=3)).isoformat(),
                "member1": "Команда А",
                "member2": "Команда Б",
                "score": score,
                "championship": "Тестовая лига",
            },
        }
    )


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.path = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.settings = Settings(
            self.path,
            "http://127.0.0.1:9/v1",
            "test-model",
            bookmaker_ingest_token=TOKEN,
        )
        self.client = self.stack.enter_context(
            TestClient(create_app(self.settings), client=("127.0.0.1", 1000))
        )
        self.repo = Repository(self.path / "matchlab.sqlite3")
        signed_in(self.client,self.repo)
        self.headers = {"X-Matchlab-Collector-Key": TOKEN}

    def tearDown(self):
        self.stack.close()

    def send(self, mode, **body):
        return self.client.post(
            "/api/bookmakers/archive/ingest",
            json={"format": FORMAT, "mode": mode, **body},
            headers=self.headers,
        )

    def feed(self, items):
        cycle = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
        response = self.send("feed", cycle=cycle, events=items)
        self.assertEqual(response.status_code, 200, response.text)
        response = self.send(
            "commit",
            cycle=cycle,
            source_at=max(i["observed_at"] for i in items),
            expected=len(items),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return cycle

    def test_ingestion_requires_local_client_and_key(self):
        self.assertEqual(
            self.client.post("/api/bookmakers/archive/ingest", json={}).status_code, 401
        )
        with TestClient(
            create_app(self.settings), client=("203.0.113.7", 1000)
        ) as external:
            self.assertEqual(
                external.get(
                    "/api/bookmakers/archive/watch", headers=self.headers
                ).status_code,
                403,
            )
        self.assertEqual(
            self.client.get("/api/bookmakers/archive/watch").status_code, 401
        )

    def test_stream_match_queues_statistics_only_when_user_requests_it(self):
        self.feed([header('101'),header('102')])
        self.assertIsNone(self.repo.claim_statistics_request())
        response=self.client.post('/api/archive/events/101/statistics/request',json={})
        self.assertEqual(response.status_code,202,response.text)
        job=self.repo.claim_statistics_request()
        self.assertEqual(job['event']['source_event_id'],'101')
        self.assertEqual(job['event']['source_kind'],'stream')
        self.assertEqual(job['event']['url'],'https://winline.ru/stavki/event/101')
        self.assertIsNone(self.repo.claim_statistics_request())
        self.assertTrue(self.repo.finish_statistics_request(job['job_id'],job['lease_id'],'missing_link'))
        status=self.client.get(f"/api/archive/events/101/statistics/request/{job['job_id']}").json()
        self.assertEqual((status['state'],status['statistics_state']),('finished','missing_link'))

    def test_incomplete_feed_does_not_replace_good_catalog(self):
        old = header()
        self.feed([old])
        new = header("102")
        cycle = "c" * 64
        self.send("feed", cycle=cycle, events=[new])
        self.assertEqual(
            self.send(
                "commit", cycle=cycle, source_at=new["observed_at"], expected=2
            ).status_code,
            422,
        )
        rows = self.client.get("/api/bookmakers/events?include_started=true").json()[
            "events"
        ]
        self.assertEqual([r["source_event_id"] for r in rows], ["101"])

    def test_future_and_live_catalog_update_score_prices_and_staleness(self):
        live = header(live=True)
        future = header("102")
        self.feed([live, future])
        data = self.client.get(
            "/api/bookmakers/events?include_started=true&phase=live&sport=football"
        ).json()
        self.assertEqual(data["events"][0]["live_score"], "0:0")
        self.assertEqual(data["summary"]["live"], 1)
        self.assertEqual(data["summary"]["upcoming"], 1)
        old = self.client.get("/api/archive/events/101/markets").json()
        live["score"] = "1:0"
        live["primary"]["outcomes"][0]["odds"] = 2.4
        self.feed([live, future])
        new = self.client.get("/api/archive/events/101/markets").json()
        self.assertEqual(new["live_score"], "1:0")
        self.assertEqual(new["headline"]["outcomes"][0]["odds"], 2.4)
        self.assertNotEqual(old["content_version"], new["content_version"])
        stale = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        live["observed_at"] = stale
        # A new old observation must not roll back the complete catalogue.
        self.assertFalse(
            self.send("commit", cycle="a" * 64, source_at=stale, expected=1).status_code
            == 200
        )

    def test_history_reaches_ai_and_new_facts_invalidate_cache(self):
        e = header()
        self.feed([e])
        with patch("matchlab.api.qwen.explain_event", return_value="Разбор") as model:
            first = self.client.post("/api/archive/events/101/explain", json={}).json()
            self.assertEqual(
                model.call_args.args[1]["archive_history"]["state"], "empty"
            )
        response = self.send("results", source="a" * 64, rows=[result()])
        self.assertEqual(response.status_code, 200, response.text)
        with patch(
            "matchlab.api.qwen.explain_event", return_value="Разбор с историей"
        ) as model:
            second = self.client.post("/api/archive/events/101/explain", json={}).json()
            context = model.call_args.args[1]
            self.assertEqual(
                context["archive_history"]["teams"][0]["recent_results"][0][
                    "score_text"
                ],
                "1:0",
            )
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(second["cached"])
        with patch("matchlab.api.qwen.explain_event") as model:
            again = self.client.post("/api/archive/events/101/explain", json={}).json()
            self.assertTrue(again["cached"])
            model.assert_not_called()

    def test_live_analysis_receives_observed_score_and_clock(self):
        self.feed([header(live=True, score="2:1")])
        self.send("results", source="a" * 64, rows=[result()])
        with patch(
            "matchlab.api.qwen.explain_event", return_value="Разбор Live"
        ) as model:
            reply = self.client.post("/api/archive/events/101/explain", json={})
            self.assertEqual(reply.status_code, 200, reply.text)
            context = model.call_args.args[1]
            self.assertEqual(context["event"]["live_score"], "2:1")
            self.assertEqual(context["archive_history"]["mode"], "live")
            self.assertIsNone(context["statistical_model"])
            self.assertNotIn("current_score_and_match_clock", context["missing_data"])

    def test_missing_evidence_returns_quote_summary_without_model_or_usage_charge(self):
        self.feed([header()])
        with patch('matchlab.qwen._completion', side_effect=AssertionError('model called')):
            response = self.client.post('/api/archive/events/101/explain', json={})
            self.assertEqual(response.status_code, 200, response.text)
            value = response.json()
            self.assertEqual(value['analysis_type'], 'limited_summary')
            self.assertIn('нейросеть не вызывалась', value['text'])
            self.assertIn('по линии', value['text'])
            self.assertNotIn('волатильность', value['text'])
            self.assertNotIn('в момент начала', value['text'])
            cached = self.client.post('/api/archive/events/101/explain', json={}).json()
            self.assertTrue(cached['cached'])
        with self.repo.connection() as db:
            statuses = [row[0] for row in db.execute('SELECT status FROM ai_usage ORDER BY rowid')]
        self.assertEqual(statuses, ['limited', 'limited'])

    def test_selected_first_half_market_receives_complete_market_calculation(self):
        live = header(live=True, score='0:0')
        live['clock'] = "1Т 27'"
        live['primary']['name'] = '1 тайм исход 1X2'
        for outcome, odd in zip(live['primary']['outcomes'], [10, 1.87, 2.1]):
            outcome['odds'] = odd
        self.feed([live])
        with patch('matchlab.qwen._completion', side_effect=AssertionError('model called')):
            response = self.client.post('/api/archive/events/101/explain', json={})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value['analysis_type'], 'limited_summary')
        self.assertTrue(value['context']['markets'][0]['outcomes_complete'])
        for part in ("1Т 27'", 'Победитель: оценить по доступным данным нельзя', 'не применя'):
            self.assertIn(part, value['text'])

    def test_search_is_not_called_and_live_clock_stays_verbatim(self):
        live = header(live=True, score='2:0')
        live['clock'] = "2Т 13'"
        self.feed([live])
        with patch('matchlab.web_research.research', side_effect=AssertionError('Search disabled')) as search, \
             patch('matchlab.qwen._completion', side_effect=AssertionError('model called')):
            response = self.client.post('/api/archive/events/101/explain', json={'notes': 'private note'})
            self.assertEqual(response.status_code, 200, response.text)
            search.assert_not_called()
            result = response.json()
            self.assertEqual(result['analysis_type'], 'limited_summary')
            self.assertIn('Счёт в снимке: 2:0', result['text'])
            self.assertIn("2Т 13'", result['text'])
            self.assertNotIn('13-я минута второго тайма', result['text'])
            self.assertNotIn('28.10.2026', result['text'])
            self.assertIn('по линии', result['text'])
            self.assertEqual(result['context']['web_research'], {'state':'disabled','sources':[]})
            saved = self.client.get('/api/analyses/' + result['id']).json()
            self.assertEqual(saved['context']['web_research'], {'state':'disabled','sources':[]})

    def test_confirmed_history_and_all_markets_are_sent_without_web_search(self):
        self.feed([header()])
        self.send('results', source='a' * 64, rows=[result()])
        with patch('matchlab.web_research.research', side_effect=AssertionError('Search disabled')) as search, \
             patch('matchlab.qwen._completion', side_effect=reasoning_completion) as model:
            response = self.client.post('/api/archive/events/101/explain', json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['analysis_type'], 'model')
        payloads=[json.loads(call.args[1][1]['content']) for call in model.call_args_list]
        values=record_values([record for payload in payloads for record in payload.get('records',[])])
        self.assertEqual(values['/archive_history/state'],'ready')
        search.assert_not_called()
        self.assertEqual(values['/web_research/state'],'disabled')
        self.assertEqual(values['/web_research/sources'],[])
        self.assertTrue(any(path.startswith('/markets/') for path in values))

    def test_admin_corrects_old_quotation_error_without_rewriting_the_saved_report(self):
        self.feed([header()]);self.send('results',source='a'*64,rows=[result()])
        def invalid(settings,messages):
            return '{"observations":[{"text":"Факт","quotes":[{"source":"missing","quote":"invented"}]}]}'
        with patch('matchlab.qwen._completion',side_effect=invalid):
            saved=self.client.post('/api/archive/events/101/explain',json={}).json()
        self.assertEqual(saved['context']['ai_generation']['state'],'unavailable')
        saved['context']['ai_generation']['reason']='context_limit'
        saved['context']['ai_generation']['attempts']=[{'call':1,'stage':'evidence','status':'invalid_response','validation':'quotation_mismatch'}]
        with self.repo.connection() as db:
            db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(saved),saved['id']))
            row=db.execute('SELECT id,details FROM ai_usage ORDER BY rowid DESC LIMIT 1').fetchone()
            details=json.loads(row['details']);details['reason']='context_limit'
            db.execute('UPDATE ai_usage SET details=? WHERE id=?',(json.dumps(details),row['id']))
        signed_in(self.client,self.repo,'admin_fixture','admin')
        item=self.client.get('/api/admin/overview').json()['recent_ai'][0]
        self.assertEqual(item['generation']['reason'],'quotation_mismatch')
        self.assertTrue(item['generation']['corrected_from_saved'])
        self.assertEqual(self.repo.event_analysis(analysis_id=saved['id'])['context']['ai_generation']['reason'],'context_limit')

    def test_independent_chances_use_only_past_same_competition_and_keep_parameters_private(self):
        self.feed([header()])
        rows=[]
        for i in range(45):
            item=result(cursor=i+1)
            item['data'].update(date=(datetime.now(UTC)-timedelta(days=i+1)).isoformat(),score=f'{i%4}:{i%3}')
            rows.append(rekey(item))
        self.assertEqual(self.send('results',source='a'*64,rows=rows).status_code,200)
        with patch('matchlab.qwen._completion',side_effect=reasoning_completion):
            response=self.client.post('/api/archive/events/101/explain',json={})
        self.assertEqual(response.status_code,200,response.text)
        saved=response.json(); assessment=saved['context']['probability_assessment']
        self.assertEqual(assessment['winner']['basis'],'history')
        self.assertAlmostEqual(sum(v['probability'] for v in assessment['markets']),1)
        self.assertEqual(len(saved['text'].split('\n\n')),4)
        for private in ('half_life','expected_goals','intensities','Gamma','Пуассон'):
            self.assertNotIn(private,json.dumps(saved,ensure_ascii=False))

    def test_qwen_failure_returns_saved_calculation_and_safe_category(self):
        self.feed([header()])
        self.send('results',source='a'*64,rows=[result()])
        with patch('matchlab.qwen._completion',side_effect=ValueError('Полный контекст матча превышает лимит сервера ИИ.')):
            response=self.client.post('/api/archive/events/101/explain',json={})
        self.assertEqual(response.status_code,200,response.text)
        data=response.json()
        self.assertEqual(data['analysis_type'],'statistical_report')
        self.assertEqual(data['context']['ai_generation']['reason'],'context_limit')
        self.assertIn('контекст нейросети',data['text'])
        self.assertTrue(data['text'].startswith('Победитель:'))

    def test_admin_sees_actual_calls_and_cache_does_not_claim_another_inference(self):
        self.feed([header()]);self.send('results',source='a'*64,rows=[result()])
        with patch('matchlab.qwen._completion',side_effect=reasoning_completion):
            first=self.client.post('/api/archive/events/101/explain',json={}).json()
            cached=self.client.post('/api/archive/events/101/explain',json={}).json()
        self.assertTrue(cached['cached'])
        with self.repo.connection() as db:
            details=[json.loads(row['details']) for row in db.execute('SELECT details FROM ai_usage ORDER BY rowid')]
        self.assertEqual(details[0]['state'],'ready')
        self.assertGreaterEqual(details[0]['model_calls'],2)
        self.assertEqual(details[0]['parts_completed'],details[0]['parts_total'])
        self.assertTrue(details[1]['cache_hit']);self.assertEqual(details[1]['model_calls'],0)
        signed_in(self.client,self.repo,'admin_fixture','admin')
        overview=self.client.get('/api/admin/overview').json()
        self.assertEqual(overview['recent_ai'][0]['generation']['analysis_id'],first['id'])
        self.assertEqual(overview['ai']['cached'],1)

    def test_old_summary_status_is_resolved_only_from_its_matching_saved_analysis(self):
        self.feed([header()]);self.send('results',source='a'*64,rows=[result()])
        with patch('matchlab.qwen._completion',side_effect=ValueError('Полный контекст матча превышает лимит сервера ИИ.')):
            response=self.client.post('/api/archive/events/101/explain',json={})
        self.assertEqual(response.status_code,200,response.text)
        with self.repo.connection() as db:db.execute('UPDATE ai_usage SET details=NULL')
        signed_in(self.client,self.repo,'admin_fixture','admin')
        item=self.client.get('/api/admin/overview').json()['recent_ai'][0]
        self.assertEqual(item['status'],'limited')
        self.assertEqual(item['generation']['reason'],'context_limit')
        self.assertTrue(item['generation']['derived_from_saved'])

    def test_history_is_exact_and_never_uses_future_score_corrections(self):
        self.feed([header()])
        early = datetime.now(UTC)
        original = result(observed=(early - timedelta(seconds=10)).isoformat())
        self.send("results", source="a" * 64, rows=[original])
        change = deepcopy(original)
        change.update(cursor=2, observed_at=(early + timedelta(seconds=30)).isoformat())
        change["data"]["score"] = "3:0"
        self.send("results", source="a" * 64, rows=[change])
        doc = archive.document(self.repo, "101")
        before = archive.history(self.repo, doc, early.isoformat())
        # The receipt itself happens just after early; backtesting must not include it yet.
        self.assertEqual(before["state"], "empty")
        known = archive.history(
            self.repo, doc, (early + timedelta(seconds=1)).isoformat()
        )
        self.assertEqual(known["teams"][0]["recent_results"][0]["score_text"], "1:0")
        after = archive.history(
            self.repo, doc, (early + timedelta(seconds=31)).isoformat()
        )
        self.assertEqual(after["teams"][0]["recent_results"][0]["score_text"], "3:0")
        wrong = deepcopy(doc)
        wrong["home"] = "Команда А резерв"
        self.assertEqual(archive.history(self.repo, wrong)["state"], "mismatch")

    def test_duplicate_cursor_is_idempotent_but_conflicting_replay_is_rejected(self):
        row = result()
        self.send("results", source="a" * 64, rows=[row])
        self.send("results", source="a" * 64, rows=[row])
        self.assertEqual(self.client.get("/api/archive/results").json()["total"], 1)
        forged = deepcopy(row)
        forged["data"]["score"] = "9:9"
        self.assertEqual(
            self.send("results", source="a" * 64, rows=[forged]).status_code, 422
        )
        self.assertEqual(
            self.client.get("/api/archive/results").json()["results"][0]["score"], "1:0"
        )

    def test_other_sports_and_target_match_result_do_not_become_prior_form(self):
        e = header(live=True)
        e["radar_id"] = 999
        self.feed([e])
        row = result(sport=2)
        self.send("results", source="a" * 64, rows=[row])
        self.assertEqual(
            archive.history(self.repo, archive.document(self.repo, "101"))["state"],
            "empty",
        )
        self.assertEqual(
            self.send("results", source="a" * 64, rows=[result(cursor=2)]).status_code,
            200,
        )
        self.assertEqual(
            archive.history(self.repo, archive.document(self.repo, "101"))["state"],
            "empty",
        )

    def test_details_cannot_attach_another_events_market(self):
        e = header()
        self.feed([e])
        m = deepcopy(e["primary"])
        m["source_event_id"] = "999"
        self.assertEqual(self.send("detail", event=e, markets=[m]).status_code, 422)
        self.assertEqual(
            self.client.post("/api/archive/events/101/watch", json={}).status_code, 200
        )
        self.assertEqual(
            self.client.get(
                "/api/bookmakers/archive/watch", headers=self.headers
            ).json()["events"],
            [101],
        )

    def test_departed_events_are_not_inferred_finished_without_a_result(self):
        e = header(live=True)
        self.feed([e])
        self.feed([header("102")])
        gone = self.client.get("/api/archive/events/101/markets").json()
        self.assertFalse(gone["source_present"])
        self.assertEqual(gone["phase"], "unknown")
        self.assertEqual(
            self.client.post("/api/archive/events/101/watch", json={}).status_code, 404
        )
        row = result()
        row["data"]["date"] = e["kickoff_at"]
        self.assertEqual(
            self.send("results", source="a" * 64, rows=[rekey(row)]).status_code, 200
        )
        finished = self.client.get("/api/archive/events/101/markets").json()
        self.assertEqual(finished["phase"], "finished")
        self.assertEqual(finished["final_result"]["score_text"], "1:0")

    def test_model_request_rejects_a_changed_market_version(self):
        self.feed([header()])
        response = self.client.post(
            "/api/archive/events/101/explain", json={"content_version": "0" * 32}
        )
        self.assertEqual(response.status_code, 409)

    def test_result_search_is_case_insensitive_for_cyrillic(self):
        self.send("results", source="a" * 64, rows=[result()])
        self.assertEqual(
            self.client.get("/api/archive/results?search=КОМАНДА&sport=1").json()[
                "total"
            ],
            1,
        )

    def test_future_results_and_result_identity_collisions_are_rejected(self):
        row = result()
        row["data"]["date"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        self.assertEqual(
            self.send("results", source="a" * 64, rows=[rekey(row)]).status_code, 422
        )
        row = result()
        row["data"]["member1"] = "Другая команда"
        self.assertEqual(
            self.send("results", source="a" * 64, rows=[row]).status_code, 422
        )
        self.assertEqual(self.client.get("/api/archive/results").json()["total"], 0)

    def test_source_placeholder_is_preserved_without_becoming_a_completed_match(self):
        row = result()
        row["data"].update(date=None, idRadar=None, member1="", member2="", score="")
        response = self.send(
            "results", source="a" * 64, rows=[rekey(row), result(cursor=2)]
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/archive/results").json()["total"], 1)
        self.assertEqual(
            self.client.get("/api/archive/status").json()["unidentified_results"], 1
        )
        with self.repo.connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM archive_versions").fetchone()[0], 2
            )

    def test_empty_complete_feed_clears_catalogue(self):
        self.feed([header()])
        response = self.send(
            "commit",
            cycle="d" * 64,
            source_at=datetime.now(UTC).isoformat(),
            expected=0,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get("/api/bookmakers/events?include_started=true").json()[
                "events"
            ],
            [],
        )

    def test_verified_provider_statistics_reach_stream_ai_but_another_pair_does_not(
        self,
    ):
        from test_product import event as legacy_event
        from test_statistics import stats

        legacy = legacy_event("101")
        legacy.update(
            home="Спартак М",
            away="Акрон Тольятти",
            statistics_url="https://winline.ru/stats/match/72334410",
        )
        self.repo.import_event_markets(legacy)
        self.repo.import_statistics(stats("101"))
        e = header()
        e["members"] = [legacy["home"], legacy["away"]]
        e["kickoff_at"] = legacy["kickoff_at"]
        self.feed([e])
        with patch("matchlab.api.qwen.explain_event", return_value="Разбор") as model:
            response = self.client.post("/api/archive/events/101/explain", json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                model.call_args.args[1]["team_statistics"]["statistics_id"], "72334410"
            )
        e["members"][0] = "Спартак М резерв"
        self.feed([e])
        with patch(
            "matchlab.api.qwen.explain_event", return_value="Без чужой статистики"
        ) as model:
            response = self.client.post("/api/archive/events/101/explain", json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNone(model.call_args.args[1]["team_statistics"])


if __name__ == "__main__":
    unittest.main()
