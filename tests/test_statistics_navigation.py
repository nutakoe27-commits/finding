"""Canonical match navigation and honest unavailable-vs-missing-link states."""
import unittest
from unittest.mock import AsyncMock,Mock,patch

import httpx

from matchlab.winline_markets import event_navigation_url,EventNavigationError
from matchlab import statistics_worker as worker


class NavigationTests(unittest.TestCase):
    def test_old_queued_short_urls_use_the_catalogue_sport_without_guessing(self):
        for sport,slug in [('football','futbol'),('hockey','xokkej'),('tennis','tennis')]:
            with self.subTest(sport=sport):
                self.assertEqual(event_navigation_url('https://winline.ru/stavki/event/17089331',{'source_event_id':'17089331','sport':sport}),f'https://winline.ru/stavki/sport/{slug}/17089331')

    def test_id_and_sport_mismatches_are_not_repaired_into_another_match(self):
        for url,metadata,code in [
            ('https://winline.ru/stavki/event/123',{'source_event_id':'999','sport':'football'},'event_identity_mismatch'),
            ('https://winline.ru/stavki/sport/xokkej/123',{'source_event_id':'123','sport':'football'},'sport_identity_mismatch'),
            ('https://secret@winline.ru/stavki/event/123',{'source_event_id':'123','sport':'football'},'invalid_event_url'),
            ('https://evil.example/stavki/event/123',{'source_event_id':'123'},'invalid_event_url')]:
            with self.subTest(url=url),self.assertRaises(EventNavigationError) as raised:event_navigation_url(url,metadata)
            self.assertEqual(raised.exception.code,code)

    def test_without_verified_sport_the_source_route_is_preserved(self):
        self.assertEqual(event_navigation_url('https://winline.ru/stavki/event/123'), 'https://winline.ru/stavki/event/123')


class WorkerNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_opens_the_canonical_page_even_for_an_old_queued_job(self):
        context=AsyncMock();page=AsyncMock();context.new_page.return_value=page
        page.on=Mock();page.goto.return_value=Mock(status=403)
        event={'source_event_id':'17089331','sport':'football','url':'https://winline.ru/stavki/event/17089331'}
        result=await worker.refresh_market(context,AsyncMock(),event)
        self.assertEqual(result,'blocked')
        self.assertEqual(page.goto.await_args.args[0],'https://winline.ru/stavki/sport/futbol/17089331')
        page.close.assert_awaited_once()

    async def test_navigation_failures_never_claim_that_statistics_link_is_absent(self):
        for refreshed,expected in [('unavailable','unavailable'),('blocked','blocked'),('ok','missing_link'),('partial','missing_link')]:
            with self.subTest(refreshed=refreshed):
                client=AsyncMock();context=AsyncMock()
                client.get.return_value=httpx.Response(200,request=httpx.Request('GET','http://localhost/statistics'),json={'link_status':'unknown'})
                client.post.return_value=httpx.Response(200,request=httpx.Request('POST','http://localhost/finish'),json={})
                job={'job_id':'a'*32,'lease_id':'b'*32,'event':{'source_event_id':'123','source_kind':'stream'}}
                with patch.object(worker,'refresh_market',new_callable=AsyncMock,return_value=refreshed),patch.object(worker,'collect_statistics',new_callable=AsyncMock) as collect:
                    result=await worker.statistics_request_job(context,client,job)
                self.assertEqual(result['statistics_state'],expected);collect.assert_not_awaited()
                self.assertEqual(client.post.await_args.kwargs['json']['statistics_state'],expected)

    async def test_navigation_reason_is_safe_and_access_checks_are_not_bypassed(self):
        context=AsyncMock();page=AsyncMock();context.new_page.return_value=page
        page.on=Mock();page.goto.return_value=Mock(status=200)
        event={'source_event_id':'123','sport':'football','url':'https://winline.ru/stavki/event/123'}
        with patch.object(worker,'ensure_event_page',new_callable=AsyncMock,side_effect=EventNavigationError('access_check')),self.assertLogs(worker.LOG,level='WARNING') as logs:
            result=await worker.refresh_market(context,AsyncMock(),event)
        self.assertEqual(result,'blocked');self.assertIn('reason=access_check',' '.join(logs.output))
        page.evaluate.assert_not_awaited()
