"""No computed chances; bounded risk; abstention and blind random evaluation."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.bookmakers import make_snapshot,instant
from matchlab.markets import validate_event_markets
from matchlab.neural_verdict import build_packet,document_quote,generate,validate_reply,render,encoded,system_prompt,VerdictValidationError
from matchlab.statistics import analysis_statistics
from matchlab.storage import Repository
from matchlab.verdict_trial import prepare,run_trial,metrics,paired_observation,save
from scripts.configure_neural_verdict import configure
from account_helpers import signed_in
from test_product import event
from test_forecast_quality import seed
from verdict_helpers import statistics,reply,complete,EXPLANATION

NOW=datetime.now(timezone.utc)


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name);self.repo=Repository(self.path/'matchlab.sqlite3');self.repo.initialize()
        self.settings=Settings(self.path,'http://localhost:9/v1','verdict-test',event_analysis_mode='neural-verdict')

    def packet(self):
        value=event();value['statistics_url']='https://winline.ru/stats/match/72334410'
        document=validate_event_markets(value)
        scope={**document,'sport_label':'Футбол'}
        stats=analysis_statistics(statistics());at=datetime.now(timezone.utc)
        return build_packet(self.repo,scope,stats,at,document_quote(document,at))

    def test_no_prediction_model_is_called_and_prices_reach_the_neural_input(self):
        with patch('matchlab.chances.assess',side_effect=AssertionError('probability model called')),patch('matchlab.chances.fit_goals',side_effect=AssertionError('goal model called')):
            packet=self.packet()
        self.assertEqual(set(o['id'] for o in packet['options']),{'home','away'})
        self.assertEqual(packet['quote']['odds']['draw'],3.4)
        encoded=json.dumps(packet)
        for forbidden in ('probability_assessment','outcome_probabilities','statistical_model','expected_goals'):
            self.assertNotIn(forbidden,encoded)

    def test_valid_bet_has_no_probability_and_a_server_capped_stake(self):
        packet=self.packet();value=validate_reply(reply(packet),packet)
        self.assertEqual(value['selection'],'home')
        verdict,generation=generate(self.settings,packet,complete)
        self.assertEqual(verdict['decision'],'bet');self.assertEqual(verdict['stake_percent'],.25)
        self.assertEqual(generation['state'],'ready');self.assertEqual(generation['model_calls'],1)
        self.assertNotIn('%',' '.join(a['text'] for a in verdict['arguments']))
        self.assertIn('0,25%',render(verdict));self.assertNotIn('вероятность',render(verdict))

    def test_missing_or_stale_prices_force_skip_without_faking_a_bet(self):
        for change in ('missing','stale'):
            p=self.packet()
            if change=='missing':p['options']=[];p['quote']=None
            p['policy']['force_skip']=True
            with self.subTest(change=change),self.assertRaises(ValueError):validate_reply(reply(p,'bet'),p)
            with patch('matchlab.qwen._completion',side_effect=AssertionError('forced skip must not call Qwen')):
                verdict,generation=generate(self.settings,p)
            self.assertEqual(verdict['decision'],'skip');self.assertEqual(verdict['stake_percent'],0)
            self.assertEqual(verdict['engine'],'data_guard');self.assertTrue(verdict['data_forced_skip'])
            self.assertEqual(generation['model_calls'],0)

    def test_model_skip_and_response_error_are_distinct(self):
        packet=self.packet()
        good=lambda opts,messages:json.dumps(reply(packet,'skip'))
        skip,good_gen=generate(self.settings,packet,good)
        error,bad_gen=generate(self.settings,packet,lambda *args:'not json')
        self.assertEqual(skip['engine'],'qwen');self.assertEqual(good_gen['state'],'ready')
        self.assertEqual(error['engine'],'model_error');self.assertEqual(bad_gen['state'],'unavailable')
        self.assertEqual(error['stake_percent'],0)

    def test_repair_receives_the_failed_answer_and_specific_unsupported_numbers(self):
        packet=self.packet();messages_seen=[]
        bad=reply(packet);bad['comparison']['home']['text']=EXPLANATION+' Забито 9999 голов.'
        def model(opts,messages):
            messages_seen.append(messages)
            return json.dumps(bad if len(messages_seen)==1 else reply(packet))
        verdict,generation=generate(self.settings,packet,model)
        self.assertEqual(verdict['engine'],'qwen');self.assertEqual(generation['model_calls'],2)
        detail=generation['attempts'][0]['validation_details']
        self.assertEqual(detail['unsupported_numbers'],['9999'])
        repair=messages_seen[1]
        self.assertEqual(repair[2]['role'],'assistant')
        self.assertEqual(json.loads(repair[3]['content'])['details'],detail)

    def test_neutral_caveats_are_allowed_but_promises_and_numeric_confidence_are_rejected(self):
        p=self.packet()
        for caveat in ('Это не гарантирует победу.','Это не гарантированная победа.','Гарантий успеха нет.',
                       'Без гарантии выигрыша.','Победу невозможно гарантировать.','У гостей сохраняются шансы на ничью.',
                       'Линия не является гарантией результата.','Эти данные не являются гарантиями победы.'):
            v=reply(p);v['market_context']['text']+=' '+caveat
            with self.subTest(caveat=caveat):validate_reply(v,p)
        for claim in ('Гарантированная победа.','Это гарантирует прибыль.','Не не гарантирует победу.',
                      'Нет гарантий, но выигрыш гарантирован.','Шанс: 70.','Шанс победы 90%.','Это победа без риска.',
                      'Линия является гарантией результата.','Это не не является гарантией победы.',
                      'Линия не является гарантией, но победа гарантирована.'):
            v=reply(p);v['market_context']['text']+=' '+claim
            with self.subTest(claim=claim),self.assertRaisesRegex(ValueError,'verdict_forbidden_claim'):validate_reply(v,p)

    def test_explicit_inline_citations_are_moved_without_guessing_a_source_or_repeating_the_call(self):
        p=self.packet();v=reply(p,'skip')
        home=v['comparison']['home']['fact_ids'][0];away=v['comparison']['away']['fact_ids'][0]
        market=v['market_context']['fact_ids'][0]
        v['counterargument']['text']+=f' ({away}).'
        v['reason']['text']+=f' Учтена исходная линия ({market}); также сохранены факты ({home}, {away}).'
        v['market_context']['text']+=' Линия не является гарантией результата.'
        original=deepcopy(v)
        with patch('matchlab.qwen._completion',return_value=json.dumps(v,ensure_ascii=False)) as model:
            verdict,g=generate(self.settings,p,historical=True)
        self.assertEqual(model.call_count,1);self.assertEqual(g['model_calls'],1)
        self.assertEqual(verdict['engine'],'qwen');self.assertEqual(verdict['preference'],'home')
        self.assertEqual(verdict['decision'],'skip')
        self.assertEqual(verdict['reason']['fact_ids'],[home,away,market])
        self.assertNotIn('('+market+')',render(verdict))
        self.assertEqual(v,original)
        self.assertEqual(g['attempts'][0]['normalizations'],verdict['format_normalizations'])

    def test_unknown_inline_sources_and_unstructured_reference_text_are_still_rejected(self):
        p=self.packet()
        v=reply(p);v['comparison']['home']['text']+=' Подтверждение (F999).'
        with self.assertRaises(VerdictValidationError) as caught:validate_reply(v,p)
        self.assertEqual(str(caught.exception),'verdict_fact_reference')
        self.assertIn('F999',caught.exception.details['unknown_fact_ids'])
        for annotation in (' согласно F1.',' (см. F1).',' (F1 гарантирует победу).'):
            v=reply(p);v['comparison']['home']['text']+=annotation
            with self.subTest(annotation=annotation),self.assertRaisesRegex(ValueError,'verdict_forbidden_claim'):
                validate_reply(v,p)

    def test_normalizing_a_citation_does_not_mask_an_unsupported_number_or_promise(self):
        p=self.packet()
        for wording,reason in [(' Забито 9999 голов (F1).','unsupported_number'),
                               (' Победа гарантирована (F1).','verdict_forbidden_claim')]:
            v=reply(p);v['comparison']['home']['text']+=wording
            with self.subTest(wording=wording),self.assertRaisesRegex(ValueError,reason):validate_reply(v,p)

    def test_reference_count_limit_applies_to_inline_citations_too(self):
        p=self.packet();v=reply(p)
        extra=['F'+str(i) for i in range(104,112)]
        p['facts'].extend({**p['facts'][0],'id':i} for i in extra)
        v['comparison']['home']['text']+=' Дополнительные сведения ('+', '.join(extra)+').'
        with self.assertRaises(VerdictValidationError) as caught:validate_reply(v,p)
        self.assertEqual(caught.exception.details['maximum_references'],8)

    def test_prompt_examples_always_use_existing_facts_for_each_role(self):
        p=self.packet();prompt=system_prompt(p)
        market=next(f['id'] for f in p['facts'] if f['kind']=='market')
        self.assertIn('"market_context":{"fact_ids":["'+market+'"]',prompt)
        self.assertNotIn('"F9"',prompt);self.assertNotIn('__MARKET_FACTS__',prompt)

    def test_reference_layout_is_normalized_without_inventing_evidence(self):
        p=self.packet();v=reply(p)
        v['comparison']['home']['fact_ids']=' f1 '
        v['reason']['fact_ids']='f1; F2'
        accepted=validate_reply(v,p)
        self.assertEqual(accepted['comparison']['home']['fact_ids'],['F1'])
        self.assertEqual(accepted['reason']['fact_ids'],['F1','F2'])
        for i in range(104,107):p['facts'].append({**p['facts'][0],'id':'F'+str(i)})
        v['comparison']['home']['fact_ids']=['F1','F104','F105','F106']
        self.assertEqual(len(validate_reply(v,p)['comparison']['home']['fact_ids']),4)
        v['comparison']['home']['fact_ids']=['F999']
        with self.assertRaises(VerdictValidationError) as caught:validate_reply(v,p)
        self.assertEqual(caught.exception.details['field'],'comparison.home')
        self.assertEqual(caught.exception.details['unknown_fact_ids'],['F999'])
        self.assertIn('F1',caught.exception.details['allowed_fact_ids'])

    def test_repair_gets_all_invalid_fields_not_just_the_first_one(self):
        p=self.packet();v=reply(p);seen=[]
        v['comparison']['home']['text']+=' Победа гарантирована.'
        v['comparison']['away']['fact_ids']=['F999']
        v['market_context']['text']+=' Это гарантирует выигрыш.'
        def model(opts,messages):
            seen.append(messages)
            return json.dumps(v if len(seen)==1 else reply(p))
        verdict,g=generate(self.settings,p,model,capture_rejected=True)
        self.assertEqual(verdict['engine'],'qwen');self.assertEqual(g['model_calls'],2)
        fields={i['field'] for i in g['attempts'][0]['validation_details']['issues']}
        self.assertTrue({'comparison.home','comparison.away','market_context'}<=fields)
        self.assertEqual(json.loads(g['attempts'][0]['rejected_response']),v)
        self.assertNotIn('rejected_response',g['attempts'][1])

    def test_rejected_answers_are_not_logged_or_stored_in_production_attempts(self):
        p=self.packet();bad=json.dumps({'RAW_MODEL_MARKER_NOT_FOR_LOGS':'bad response'})
        with self.assertLogs('uvicorn.error',level='WARNING') as log:
            _,g=generate(self.settings,p,lambda *args:bad)
        self.assertNotIn('RAW_MODEL_MARKER_NOT_FOR_LOGS','\n'.join(log.output))
        self.assertTrue(all('rejected_response' not in a for a in g['attempts']))
        _,debug=generate(self.settings,p,lambda *args:bad,capture_rejected=True)
        self.assertTrue(all(a['rejected_response']==bad for a in debug['attempts']))

    def test_trial_report_is_private_before_any_diagnostic_content_is_written(self):
        original=json.dump;seen=[]
        def dump(value,stream,**kwargs):
            import os
            seen.append(os.fstat(stream.fileno()).st_mode&0o777)
            return original(value,stream,**kwargs)
        path=self.path/'private.json'
        with patch('matchlab.verdict_trial.json.dump',side_effect=dump):save({'debug':'private'},path)
        self.assertEqual(seen,[0o600]);self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_timeout_is_reported_without_a_second_model_call(self):
        with patch('matchlab.qwen._completion',side_effect=ValueError('Сервис не ответил вовремя.')) as model:
            verdict,generation=generate(self.settings,self.packet())
        self.assertEqual(verdict['engine'],'model_error');self.assertEqual(generation['reason'],'timeout')
        self.assertEqual(model.call_count,1)

    def test_fabricated_outcomes_budgets_and_confidence_percentages_are_rejected(self):
        packet=self.packet()
        for mutation in ('draw','budget','percentage','number','reference'):
            value=reply(packet)
            if mutation=='draw':value['preference']='draw'
            elif mutation=='budget':value['stake_percent']=20
            elif mutation=='percentage':value['comparison']['home']['text']=EXPLANATION+' Шанс победы 90%.'
            elif mutation=='number':value['comparison']['home']['text']=EXPLANATION+' Забито 9999 голов.'
            else:value['comparison']['home']['fact_ids']=['F999']
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):validate_reply(value,packet)

    def test_budget_is_not_increased_by_model_text_and_thinking_is_disabled_per_call(self):
        p=self.packet();seen=[]
        def model(opts,messages):seen.append(opts);return json.dumps(reply(p))
        verdict,_=generate(replace(self.settings,verdict_max_stake_pct=.3),p,model)
        self.assertEqual(verdict['stake_percent'],.15);self.assertFalse(seen[0].qwen_enable_thinking)
        value=reply(p);value['stake_level']='high'
        with self.assertRaises(ValueError):validate_reply(value,p)

    def test_preference_survives_a_deliberate_skip_without_becoming_a_stake(self):
        p=self.packet()
        verdict,g=generate(self.settings,p,lambda opts,messages:json.dumps(reply(p,'skip')))
        self.assertEqual(verdict['preference'],'home');self.assertIsNone(verdict['selection'])
        self.assertEqual(verdict['stake_percent'],0);self.assertEqual(g['state'],'ready')
        self.assertIn('Предпочтение:',render(verdict));self.assertIn('Пропустить матч',render(verdict))
        value=reply(p,'skip',preference=None)
        self.assertIsNone(validate_reply(value,p)['preference'])
        value=reply(p,'bet',preference=None)
        with self.assertRaises(ValueError):validate_reply(value,p)

    def test_both_sides_market_and_concrete_reason_are_required(self):
        p=self.packet()
        for mutation in ('side','market','missing_counter','reason','one_sided_reason'):
            v=reply(p,'skip')
            if mutation=='side':v['comparison']['away']['fact_ids']=v['comparison']['home']['fact_ids']
            elif mutation=='market':v['market_context']['fact_ids']=v['comparison']['home']['fact_ids']
            elif mutation=='missing_counter':v.pop('counterargument')
            elif mutation=='reason':v['reason']['code']='supporting_evidence'
            else:v['reason']['fact_ids']=v['comparison']['home']['fact_ids']
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):validate_reply(v,p)

    def test_fixture_start_cancels_live_recommendation_but_not_blind_historical_replay(self):
        p=self.packet();p['event']['kickoff_at']=(NOW-timedelta(days=1)).isoformat()
        p['as_of']=(NOW-timedelta(days=2)).isoformat();p['quote']['observed_at']=p['as_of']
        model=lambda opts,messages:json.dumps(reply(p))
        live,_=generate(self.settings,p,model)
        replay,_=generate(self.settings,p,model,historical=True)
        self.assertEqual(live['engine'],'timing_guard');self.assertEqual(live['decision'],'skip')
        self.assertEqual(replay['decision'],'bet')

    def test_price_expiring_during_generation_cancels_the_stake(self):
        p=self.packet();p['quote']['observed_at']=(NOW-timedelta(minutes=4)).isoformat()
        verdict,generation=generate(self.settings,p,lambda opts,messages:json.dumps(reply(p)))
        self.assertEqual(verdict['engine'],'timing_guard');self.assertEqual(verdict['stake_percent'],0)
        self.assertEqual(generation['reason'],'quote_expired')

    def test_mode_writer_preserves_other_projects_settings_and_rejects_duplicates(self):
        path=self.path/'.env';secret='QWEN_API_KEY=PRIVATE_VALUE\nQWEN_MODEL=existing-alias\n'
        path.write_text(secret)
        configure(path)
        self.assertTrue(path.read_text().startswith(secret))
        self.assertIn('EVENT_ANALYSIS_MODE=neural-verdict',path.read_text())
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        path.write_text(path.read_text()+'EVENT_ANALYSIS_MODE=independent\n')
        before=path.read_bytes()
        with self.assertRaises(ValueError):configure(path)
        self.assertEqual(path.read_bytes(),before)

    def test_api_neural_mode_does_not_call_old_assessment_and_separates_caches(self):
        value=event();value['statistics_url']='https://winline.ru/stats/match/72334410'
        self.repo.import_event_markets(value);self.repo.import_statistics(statistics())
        with TestClient(create_app(self.settings)) as client:
            signed_in(client,self.repo)
            with patch('matchlab.chances.assess',side_effect=AssertionError('old model called')),patch('matchlab.qwen._completion',side_effect=complete):
                first=client.post('/api/bookmakers/events/123/explain',json={})
                self.assertEqual(first.status_code,200,first.text)
                payload=first.json();self.assertEqual(payload['analysis_type'],'neural_verdict')
                self.assertNotIn('probability_assessment',payload['context'])
                self.assertEqual(payload['context']['neural_verdict']['decision'],'bet')
                self.assertEqual(payload['context']['neural_input']['quote']['odds']['home'],2.5)
                from matchlab.model_comparison import historical_quotes
                with self.repo.connection() as db:retained=historical_quotes(db)
                self.assertTrue(any(r['odds']==payload['context']['neural_input']['quote']['odds'] for r in retained))
                self.assertTrue(client.post('/api/bookmakers/events/123/explain',json={}).json()['cached'])

    def test_cached_bet_with_expired_prices_is_not_reused(self):
        value=event();value['statistics_url']='https://winline.ru/stats/match/72334410'
        self.repo.import_event_markets(value);self.repo.import_statistics(statistics())
        with TestClient(create_app(self.settings)) as client,patch('matchlab.qwen._completion',side_effect=complete) as model:
            signed_in(client,self.repo)
            saved=client.post('/api/bookmakers/events/123/explain',json={}).json()
            saved['context']['neural_input']['quote']['observed_at']=(NOW-timedelta(minutes=4)).isoformat()
            with self.repo.connection() as db:
                db.execute('UPDATE event_analyses SET payload=? WHERE id=?',(json.dumps(saved),saved['id']))
            result=client.post('/api/bookmakers/events/123/explain',json={}).json()
            self.assertFalse(result['cached']);self.assertEqual(model.call_count,2)

    def test_empty_archive_cannot_be_presented_as_a_completed_ten_game_test(self):
        with self.repo.connection() as db,patch('matchlab.qwen._completion',side_effect=AssertionError('empty test called model')):
            report=prepare(db,self.settings)
            self.assertEqual(report['selected'],0)
            result=run_trial(db,self.settings,report,self.path/'empty.json')
        self.assertEqual(result['state'],'no_eligible_inputs');self.assertEqual(result['metrics']['model_calls'],0)

    def historical(self,n=12,*,prices=True):
        seed(self.repo,n)
        with self.repo.connection() as db:
            rows=db.execute('SELECT result_key,payload,start_at FROM archive_latest_results ORDER BY start_at').fetchall()
            for row in rows:
                sid=str(10000+int(row['result_key']));data=json.loads(row['payload']);data['idRadar']=int(sid)
                db.execute('UPDATE archive_latest_results SET payload=? WHERE result_key=?',(json.dumps(data),row['result_key']))
                at=(datetime.fromisoformat(row['start_at'])-timedelta(hours=2)).isoformat()
                s=statistics(str(int(row['result_key'])+500),sid,at)
                db.execute('INSERT INTO event_statistics VALUES (?,?,?,?,?)',(s['source_event_id'],sid,'ok',at,json.dumps(s)))
                if prices:
                    q=make_snapshot('winline',data['championship'],data['member1'],data['member2'],row['start_at'],at,
                        [2.5,3.4,2.8],s['event_url'],external_id=s['source_event_id'])
                    db.execute('INSERT INTO bookmaker_quotes VALUES (?,?,?,?,?,?,?)',
                        (q['id'],q['event_key'],q['fixture_key'],q['source'],q['kickoff_at'],q['observed_at'],json.dumps(q)))

    def test_missing_prices_are_rejected_before_sampling_or_model_calls(self):
        self.historical(prices=False)
        with self.repo.connection() as db,patch('matchlab.qwen._completion',side_effect=AssertionError('missing prices called model')):
            plan=prepare(db,self.settings,10,seed=42)
            result=run_trial(db,self.settings,plan,self.path/'no-prices.json')
        self.assertEqual(result['selected'],0);self.assertEqual(result['source_inventory']['eligible_statistics'],12)
        self.assertEqual(result['source_inventory']['excluded_joint_inputs']['no_matching_historical_prices'],12)
        self.assertEqual(result['metrics']['model_calls'],0)

    def test_fewer_eligible_inputs_are_reported_without_padding_the_sample(self):
        self.historical(4)
        with self.repo.connection() as db:plan=prepare(db,self.settings,10,seed=42)
        self.assertEqual(plan['selected'],4);self.assertEqual(plan['inputs_with_fresh_prices'],4)
        self.assertEqual(plan['inputs_requiring_skip'],0)

    def test_reviewed_fixtures_are_excluded_before_a_new_holdout_sample(self):
        self.historical()
        with self.repo.connection() as db:
            old=prepare(db,self.settings,4,seed=42)
            path=self.path/'reviewed-v2.json';path.write_text(json.dumps(old))
            new=prepare(db,self.settings,10,seed=42,exclude_reports=[path])
        self.assertEqual(new['selected'],8)
        self.assertTrue({c['result_key'] for c in old['cases']}.isdisjoint(c['result_key'] for c in new['cases']))
        self.assertEqual(new['source_inventory']['excluded_joint_inputs']['previously_reviewed_fixture'],4)

    def test_targeted_debug_case_is_labelled_and_not_presented_as_random_evaluation(self):
        self.historical(4)
        with self.repo.connection() as db:plan=prepare(db,self.settings,1,event_ids=['501'])
        self.assertEqual(plan['selected'],1);self.assertEqual(plan['cases'][0]['event']['source_event_id'],'501')
        self.assertTrue(plan['diagnostic_only']);self.assertEqual(plan['selection_policy'],'targeted-debug-input-v1')

    def test_later_prematch_prices_join_stats_without_rewinding_the_capture(self):
        packet=self.packet();e=packet['event'];stats_at=NOW-timedelta(minutes=30)
        stats={'observed_at':stats_at.isoformat()}
        q=make_snapshot('winline',e['competition'],e['home'],e['away'],e['kickoff_at'],
            (stats_at+timedelta(minutes=1)).isoformat(),[2.5,3.4,2.8],'https://winline.ru/stavki/sport/futbol/123',external_id='123')
        q['_capture_at']=(stats_at+timedelta(minutes=2)).isoformat()
        at,price,reason=paired_observation(e,stats,stats_at,[q])
        self.assertIsNone(reason);self.assertEqual(at,instant(q['_capture_at']))
        q['_capture_at']=e['kickoff_at']
        self.assertEqual(paired_observation(e,stats,stats_at,[q])[2],'prices_not_available_before_kickoff')

    def test_prices_cannot_resurrect_expired_stats_or_an_old_price_capture(self):
        packet=self.packet();e=packet['event'];at=NOW-timedelta(hours=3)
        q=make_snapshot('winline',e['competition'],e['home'],e['away'],e['kickoff_at'],
            (at+timedelta(hours=2)).isoformat(),[2.5,3.4,2.8],'https://winline.ru/stavki/sport/futbol/123',external_id='123')
        self.assertEqual(paired_observation(e,{'observed_at':at.isoformat()},at,[q])[2],'statistics_expired_before_prices')
        q['observed_at']=at.isoformat();q['_capture_at']=(at+timedelta(minutes=10)).isoformat()
        self.assertEqual(paired_observation(e,{'observed_at':at.isoformat()},at,[q])[2],'no_joint_fresh_observation')

    def test_random_sample_is_fixed_before_calls_and_no_math_model_runs(self):
        self.historical()
        with self.repo.connection() as db,patch('matchlab.chances.assess',side_effect=AssertionError('model called')),patch('matchlab.chances.fit_goals',side_effect=AssertionError('model called')):
            first=prepare(db,self.settings,10,seed=42);second=prepare(db,self.settings,10,seed=42)
            third=prepare(db,self.settings,10,seed=43)
        ids=lambda r:[c['result_key'] for c in r['cases']]
        self.assertEqual(ids(first),ids(second));self.assertNotEqual(ids(first),ids(third))
        self.assertEqual(first['selected'],10)
        for case in first['cases']:
            self.assertNotIn('actual_outcome',case);self.assertNotIn('actual_score',case)
            self.assertNotIn('probabilities',json.dumps(case['input']))

    def test_changing_the_target_result_never_changes_its_model_input(self):
        self.historical()
        with self.repo.connection() as db:
            original=prepare(db,self.settings,12,seed=42)
            target=max(original['cases'],key=lambda c:c['event']['kickoff_at'])
            row=db.execute('SELECT payload FROM archive_latest_results WHERE result_key=?',(target['result_key'],)).fetchone()
            data=json.loads(row['payload']);data['score']='9:0 (4:0 5:0)'
            db.execute('UPDATE archive_latest_results SET payload=? WHERE result_key=?',(json.dumps(data),target['result_key']))
            changed=prepare(db,self.settings,12,seed=42)
        other=next(c for c in changed['cases'] if c['result_key']==target['result_key'])
        self.assertEqual(target['input'],other['input']);self.assertEqual(target['input_sha256'],other['input_sha256'])

    def test_resume_reuses_results_and_waits_when_site_queue_is_busy(self):
        self.historical()
        with self.repo.connection() as db:
            plan=prepare(db,self.settings,2,seed=42)
            db.execute("INSERT INTO users(id,username,password_hash,role,created_at) VALUES ('busy','busy','unused','user',?)",(NOW.isoformat(),))
            db.execute("INSERT INTO ai_usage(id,user_id,at,kind,target,status) VALUES ('usage','busy',?,'event','fixture','pending')",(NOW.isoformat(),))
            db.execute("INSERT INTO ai_analysis_jobs(id,user_id,request_key,event_id,source,state,created_at,usage_id) VALUES ('busy','busy','k','123','bookmakers','queued',?,'usage')",(NOW.isoformat(),))
            paused=run_trial(db,self.settings,plan,self.path/'paused.json',complete)
            self.assertEqual(paused['state'],'paused_site_busy');self.assertEqual(paused['metrics']['model_calls'],0)
            db.execute("DELETE FROM ai_analysis_jobs WHERE id='busy'")
            done=run_trial(db,self.settings,paused,self.path/'paused.json',complete)
            self.assertEqual(done['state'],'completed')
            with patch('matchlab.qwen._completion',side_effect=AssertionError('duplicate generation')):
                again=run_trial(db,self.settings,done,self.path/'paused.json')
            self.assertEqual(again['metrics'],done['metrics'])

    def test_ten_fixture_protocol_calls_are_blind_and_skips_are_not_counted_as_wins(self):
        self.historical();seen=[]
        def model(opts,messages):
            data=json.loads(messages[1]['content']);seen.append(data)
            self.assertNotIn('actual_outcome',json.dumps(data));self.assertNotIn('actual_score',json.dumps(data))
            return json.dumps(reply(data,'skip'))
        with self.repo.connection() as db:
            plan=prepare(db,self.settings,10,seed=42)
            result=run_trial(db,self.settings,plan,self.path/'trial.json',model)
        self.assertEqual(len(seen),10);self.assertEqual(result['metrics']['skipped'],10)
        self.assertEqual(result['metrics']['won'],0);self.assertIsNone(result['metrics']['hit_rate_on_bets'])
        self.assertEqual(result['metrics']['paper_final_bank'],100)

    def test_paper_settlement_counts_draw_as_a_loss_and_uses_fixed_initial_bank(self):
        self.historical()
        with self.repo.connection() as db:
            plan=prepare(db,self.settings,3,seed=42)
            for case,score in zip(plan['cases'],('2:0','0:2','1:1')):
                row=db.execute('SELECT payload FROM archive_latest_results WHERE result_key=?',(case['result_key'],)).fetchone()
                target=json.loads(row['payload']);target['score']=score
                db.execute('UPDATE archive_latest_results SET payload=? WHERE result_key=?',(json.dumps(target),case['result_key']))
                packet=case['input'];packet['quote']={'observed_at':packet['as_of']}
                packet['options']=[{'id':s,'participant':packet['event'][s],'odds':2.0} for s in ('home','away')]
                packet['policy']['force_skip']=False;packet['limitations']=[]
                case['input_sha256']=hashlib.sha256(encoded(packet).encode()).hexdigest()
            result=run_trial(db,self.settings,plan,self.path/'settled.json',complete)
        m=result['metrics']
        self.assertEqual((m['bets'],m['won'],m['lost']),(3,1,2))
        self.assertEqual(m['paper_total_staked'],.75);self.assertEqual(m['paper_final_bank'],99.75)

    def test_preference_accuracy_is_separate_from_bets_and_draw_is_a_miss(self):
        self.historical()
        with self.repo.connection() as db:
            plan=prepare(db,self.settings,3,seed=42)
            for c,score in zip(plan['cases'],('2:0','0:2','1:1')):
                row=db.execute('SELECT payload FROM archive_latest_results WHERE result_key=?',(c['result_key'],)).fetchone()
                target=json.loads(row['payload']);target['score']=score
                db.execute('UPDATE archive_latest_results SET payload=? WHERE result_key=?',(json.dumps(target),c['result_key']))
            model=lambda opts,messages:json.dumps(reply(json.loads(messages[1]['content']),'skip'))
            result=run_trial(db,self.settings,plan,self.path/'preferences.json',model)
        m=result['metrics']
        self.assertEqual((m['preference_evaluated'],m['preference_hits'],m['preference_without_bet']),(3,1,3))
        self.assertAlmostEqual(m['preference_accuracy'],1/3)
        self.assertEqual(m['bets'],0);self.assertIsNone(m['hit_rate_on_bets']);self.assertEqual(m['paper_final_bank'],100)
        result['cases'][0]['verdict']['engine']='timing_guard';result['cases'][0]['generation']['reason']='quote_expired'
        self.assertEqual(metrics(result)['preference_evaluated'],3)
        result['cases'][1]['verdict']['engine']='timing_guard';result['cases'][1]['generation']['reason']='fixture_started'
        self.assertEqual(metrics(result)['preference_evaluated'],2)


if __name__=='__main__':unittest.main()
