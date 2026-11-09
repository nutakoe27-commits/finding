"""Receiver outages must not trigger a tight loop or leak the collector credential."""

import asyncio
import json
import sys
import unittest
from argparse import Namespace
from types import ModuleType
from unittest.mock import AsyncMock, Mock, patch

import httpx

from matchlab import statistics_worker as worker


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_receiver_disables_feed_and_rejected_detailed_feed_falls_back(self):
        self.assertFalse(worker.feed_supported({}))
        self.assertFalse(worker.feed_supported({'capabilities':'statistics_feed_v1'}))
        self.assertTrue(worker.feed_supported({'capabilities':['statistics_feed_v1']}))
        client=AsyncMock()
        request=httpx.Request('POST','http://127.0.0.1:18081/api/bookmakers/statistics/ingest')
        client.post.side_effect=[httpx.Response(422,request=request),httpx.Response(200,request=request,json={'updated':True})]
        snapshot={'state':'ok','capture_kind':'detailed','sections':[{'title':'ok'}],'structured_feed':{'data':'private'}}
        result=await worker.ingest_statistics(client,snapshot)
        self.assertTrue(result['updated'])
        self.assertEqual(client.post.await_count,2)
        self.assertNotIn('structured_feed',client.post.await_args_list[1].kwargs['json'])
        self.assertIn('structured_feed',snapshot)

    async def test_rejected_fast_feed_is_never_retried_without_data(self):
        client=AsyncMock()
        request=httpx.Request('POST','http://127.0.0.1:18081/api/bookmakers/statistics/ingest')
        client.post.return_value=httpx.Response(422,request=request)
        with self.assertRaises(httpx.HTTPStatusError):
            await worker.ingest_statistics(client,{'state':'ok','capture_kind':'fast','structured_feed':{'data':'private'}})
        self.assertEqual(client.post.await_count,1)

    async def test_receiver_failures_wait_and_recover(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        request = httpx.Request(
            "POST", "http://127.0.0.1:18081/api/bookmakers/refresh/claim"
        )
        trace = []
        requests = 0
        waited = []
        reports = []
        stop_event = []

        async def post(*args, **kwargs):
            nonlocal requests
            requests += 1
            trace.append("request")
            return httpx.Response(
                503 if requests <= 2 else 200, request=request, json={"job": None}
            )

        client.post.side_effect = post
        client.get.return_value = httpx.Response(
            200,
            request=httpx.Request(
                "GET", "http://127.0.0.1:18081/api/bookmakers/statistics/queue"
            ),
            json={"jobs": [], "linked_events": 708, "blocked_until": None},
        )

        async def pause(stopped, delay):
            stop_event[:] = [stopped]
            waited.append(delay)
            trace.append("pause")

        def report(args, value):
            reports.append(value)
            if value["state"] == "idle":
                stop_event[0].set()

        browser = AsyncMock()
        browser.is_connected = Mock(return_value=True)
        context = AsyncMock()
        page = Mock()
        page.is_closed.return_value = False
        context.new_page.return_value = page
        browser.new_context.return_value = context
        playwright = Mock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        manager = AsyncMock()
        manager.__aenter__.return_value = playwright
        fake_package = ModuleType("playwright")
        fake_module = ModuleType("playwright.async_api")
        fake_module.async_playwright = Mock(return_value=manager)
        fake_package.async_api = fake_module
        args = Namespace(
            headless=True, interval=60, batch_size=2, once=False, status_file=None
        )
        key = "private-fixture-collector-key-123456789"
        with (
            patch.object(
                worker,
                "runtime_config",
                return_value={
                    "api_url": "http://127.0.0.1:18081",
                    "collector_key": key,
                },
            ),
            patch.object(worker.httpx, "AsyncClient", return_value=client),
            patch.dict(
                sys.modules,
                {"playwright": fake_package, "playwright.async_api": fake_module},
            ),
            patch.object(worker, "wait_or_stop", side_effect=pause),
            patch.object(worker, "report", side_effect=report),
        ):
            result = await worker.run(args)
        self.assertEqual(result, 0)
        self.assertEqual(waited, [5, 10])
        self.assertEqual(trace, ["request", "pause", "request", "pause", "request"])
        self.assertEqual(
            [r["state"] for r in reports],
            ["receiver_unavailable", "receiver_unavailable", "idle"],
        )
        self.assertEqual(reports[0]["http_status"], 503)
        self.assertEqual(reports[0]["operation"], "refresh_claim")
        self.assertNotIn(key, json.dumps(reports))
        browser.close.assert_awaited_once()

    async def test_shutdown_interrupts_the_retry_wait(self):
        stopped = asyncio.Event()
        stopped.set()
        await asyncio.wait_for(worker.wait_or_stop(stopped, 60), timeout=0.5)

    def test_backoff_is_bounded_and_logs_only_safe_error_metadata(self):
        secret = "never-print-this-key"
        error = httpx.ConnectError("socket failed " + secret)
        self.assertEqual(
            [
                worker.receiver_failure(error, "statistics_queue", None, n)[
                    "retry_in_seconds"
                ]
                for n in range(1, 8)
            ],
            [5, 10, 20, 40, 60, 60, 60],
        )
        value = worker.receiver_failure(error, "statistics_queue", None, 1)
        self.assertEqual(value["error_type"], "ConnectError")
        self.assertNotIn(secret, json.dumps(value))
