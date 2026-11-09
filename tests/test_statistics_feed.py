import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from matchlab.statistics_feed import PassiveFeedObserver, extract_passive_feed, feed_has_other_matches


def fixture():
    match={'_id':72578418,'teams':{'home':{'_id':10,'name':'Велес'},'away':{'_id':20,'name':'Камаз Н-Ч'}}}
    return [
        {'match':match},
        {'team':{'_id':10,'name':'Велес'},'matches':[{'_id':101,'status':'finished','result':{'home':2,'away':1},'teams':{'home':{'_id':10}}},
                                                     {'_id':102,'status':'scheduled','teams':{'away':{'_id':10}}}]},
        {'team':{'_id':20,'name':'Камаз Н-Ч'},'matches':[{'_id':103,'status':'finished','teams':{'home':{'_id':20}}}]},
    ]


class PassiveFeedTests(unittest.TestCase):
    def test_exact_match_and_team_ids_preserve_results_and_future_events(self):
        result=extract_passive_feed(fixture(),'72578418')
        self.assertEqual(result['binding'],'statistics_id')
        self.assertEqual(len(result['participants']['home'][0]['matches']),2)
        self.assertEqual(result['participants']['home'][0]['matches'][1]['status'],'scheduled')
        self.assertEqual(result['participants']['away'][0]['matches'][0]['_id'],103)
        self.assertIn('future',result['scope'])
        self.assertFalse(result['truncated'])
        self.assertTrue(feed_has_other_matches(result))
        self.assertEqual(result['participants']['home'][0]['verified_participant_indices'],[0,1])

    def test_unverified_rows_are_preserved_but_not_eligible_as_confirmed_history(self):
        docs=fixture()
        docs[1]['matches'].append({'_id':999,'teams':{'home':{'_id':9999,'name':'Велес'}}})
        result=extract_passive_feed(docs,'72578418')
        item=result['participants']['home'][0]
        self.assertEqual(item['verified_participant_indices'],[0,1])
        self.assertEqual(item['unverified_participant_count'],1)
        self.assertEqual(item['matches'][2]['_id'],999)

    def test_anchor_match_alone_is_not_a_useful_fast_snapshot(self):
        match=fixture()[0]['match']
        docs=[{'match':match},{'team':match['teams']['home'],'matches':[match]},
              {'team':match['teams']['away'],'matches':[match]}]
        self.assertFalse(feed_has_other_matches(extract_passive_feed(docs,'72578418')))

    def test_same_teams_in_a_different_match_are_not_accepted(self):
        self.assertIsNone(extract_passive_feed(fixture(),'999999'))

    def test_team_id_conflict_cannot_be_replaced_by_matching_name(self):
        docs=fixture()
        docs[1]['team']['_id']=99
        result=extract_passive_feed(docs,'72578418')
        self.assertEqual(result['participants']['home'],[])
        self.assertEqual(len(result['participants']['away']),1)

    def test_different_id_spaces_require_the_exact_match_as_an_anchor(self):
        match={'_id':72578418,'teams':{'home':{'_id':15184073,'name':'Велес'},'away':{'_id':509591,'name':'Камаз Н-Ч'}}}
        home={'_id':347438,'name':'Велес'}
        away={'_id':7518,'name':'Камаз Н-Ч'}
        docs=[{'match':match},{'team':home,'matches':[{'_id':1}]},{'team':away,'matches':[{'_id':2}]},
              {'team':home,'matches':[match]},{'team':away,'matches':[match]}]
        result=extract_passive_feed(docs,'72578418')
        self.assertEqual([len(result['participants'][side]) for side in ('home','away')],[2,2])
        self.assertEqual(result['team_bindings'],{'home':['same_match_anchor'],'away':['same_match_anchor']})

    def test_wrong_opponent_or_other_event_cannot_anchor_a_conflicting_team_id(self):
        match=fixture()[0]['match']
        team={'_id':99,'name':'Велес'}
        wrong={'_id':72578418,'teams':{'home':{'_id':10,'name':'Велес'},'away':{'_id':999,'name':'Камаз Н-Ч'}}}
        other={'_id':777,'teams':match['teams']}
        result=extract_passive_feed([{'match':match},{'team':team,'matches':[wrong,other]}],'72578418')
        self.assertEqual(result['participants']['home'],[])

    def test_ambiguous_anchors_do_not_bind_either_external_id(self):
        match=fixture()[0]['match']
        docs=[{'match':match}, {'team':{'_id':99,'name':'Велес'},'matches':[match]},
              {'team':{'_id':98,'name':'Велес'},'matches':[match]}]
        self.assertEqual(extract_passive_feed(docs,'72578418')['participants']['home'],[])

    def test_unrelated_teams_and_credentials_are_not_included(self):
        docs=fixture()
        docs[1]['team']['auth_token']='very-private'
        docs[1]['matches'][0]['secret']='private-secret'
        docs[1]['matches'][0]['url']='https://example.test/?token=private'
        docs.append({'team':{'_id':999,'name':'Other Team'},'matches':[{'id':999}]})
        result=extract_passive_feed(docs,'72578418')
        encoded=json.dumps(result,ensure_ascii=False)
        self.assertEqual(len(result['participants']['home']),1)
        for text in ('very-private','private-secret','example.test','Other Team'):
            self.assertNotIn(text,encoded)

    def test_conflicting_match_identity_is_rejected(self):
        docs=fixture()
        docs.append({'match':{'_id':72578418,'teams':{'home':{'_id':99,'name':'Велес'},'away':{'_id':20,'name':'Камаз Н-Ч'}}}})
        self.assertIsNone(extract_passive_feed(docs,'72578418'))

    def test_large_arrays_are_explicitly_truncated(self):
        docs=fixture()
        docs[1]['matches']=[{'id':i} for i in range(110)]
        result=extract_passive_feed(docs,'72578418')
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['participants']['home'][0]['matches']),100)


class ObserverSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_matching_widget_provider_can_signal_denial(self):
        page=Mock()
        observer=PassiveFeedObserver(page,'72578418')
        observer.start()
        request=SimpleNamespace(frame=SimpleNamespace(url='https://statshub.yunai.tech/winline/ru/match/72578418'),resource_type='fetch')
        observer._on_response(SimpleNamespace(url='https://evil.example/api',request=request,status=403))
        self.assertFalse(observer.blocked)
        request.frame.url='https://statshub.yunai.tech/winline/ru/match/999'
        observer._on_response(SimpleNamespace(url='https://sh.fn.yunai.tech/api',request=request,status=403))
        self.assertFalse(observer.blocked)
        request.frame.url='https://statshub.yunai.tech/winline/ru/match/72578418'
        observer._on_response(SimpleNamespace(url='https://sh.fn.yunai.tech/api',request=request,status=403))
        self.assertTrue(observer.blocked)
        await observer.close()
        page.remove_listener.assert_called_once()

    async def test_provider_denial_interrupts_active_page_collection(self):
        from matchlab.statistics import collect_statistics
        stopped=asyncio.Event()
        class Observer:
            def __init__(self,page,statistics_id):
                self.blocked=False; self.blocked_event=asyncio.Event(); self.ready=asyncio.Event(); self.feed=None
            def start(self):
                async def deny():
                    await asyncio.sleep(.01)
                    self.blocked=True; self.blocked_event.set(); self.ready.set()
                asyncio.create_task(deny())
            async def close(self): pass
        async def slow_capture(*args,**kwargs):
            try: await asyncio.sleep(60)
            finally: stopped.set()
        page=Mock(); page.goto=AsyncMock()
        job={'source_event_id':'123','url':'https://winline.ru/stavki/event/123',
             'statistics_url':'https://winline.ru/stats/match/72578418'}
        with patch('matchlab.statistics_feed.PassiveFeedObserver',Observer), \
             patch('matchlab.statistics._collect_statistics_dom',side_effect=slow_capture):
            result=await asyncio.wait_for(collect_statistics(page,job,enable_feed=True),timeout=1)
        self.assertEqual(result['state'],'blocked')
        self.assertTrue(stopped.is_set())
        page.goto.assert_awaited_once_with('about:blank',wait_until='commit',timeout=5000)


if __name__=='__main__': unittest.main()
