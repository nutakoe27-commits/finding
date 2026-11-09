"""Offline network inspection and redaction for the statistics diagnostic."""
import json
import shutil
import unittest
from unittest.mock import patch

from scripts.inspect_statistics_network import direct_request, endpoint, inspect_page, shape, widget_url


class RedactionTests(unittest.TestCase):
    def test_public_endpoint_never_includes_queries_or_unrecognised_segments(self):
        url='https://statshub.yunai.tech/api/v1/match/123/private-secret?token=hidden#private'
        result=endpoint(url)
        self.assertEqual(result,'statshub.yunai.tech/api/v1/match/{id}/{other}')
        self.assertNotIn('secret',result)
        self.assertIsNone(endpoint('https://other.example/api/v1/match/1'))
        self.assertTrue(widget_url('https://winline.ru/stats/match/123'))
        self.assertFalse(widget_url('https://winline.ru/stats/match/123?token=hidden'))

    def test_json_summary_never_contains_values_or_unrecognised_keys(self):
        summary=shape({'data':{'match':{'secret':'hidden-value'}, 'token':'hidden-token'},
                       'private-name': 'private-value'})
        encoded=json.dumps(summary)
        self.assertIn('data',encoded)
        for value in ('hidden-value','hidden-token','private-name','private-value','token'):
            self.assertNotIn(value,encoded)

    def test_provider_doc_shape_keeps_structure_without_values(self):
        result=shape({'doc':[{'event':'private-event', 'data':{'statistics':{'teams':[{'name':'private-name'}]}}}]})
        encoded=json.dumps(result)
        self.assertIn('statistics',encoded)
        self.assertIn('teams',encoded)
        self.assertNotIn('private-event',encoded)
        self.assertNotIn('private-name',encoded)

    def test_direct_request_compares_data_without_exposing_url_or_values(self):
        original={'team':{'id':7},'matches':[{'id':8}]}
        class Reply:
            status=200
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,amount): return json.dumps({'doc':[{'data':original}]}).encode()
        class Opener:
            def open(self,request,timeout): return Reply()
        with patch('scripts.inspect_statistics_network.urllib.request.build_opener',return_value=Opener()):
            result=direct_request('https://sh.fn.yunai.tech/path?token=private',original)
        self.assertTrue(result['same_team'])
        self.assertTrue(result['same_matches'])
        self.assertNotIn('private',json.dumps(result))
        self.assertEqual(direct_request('https://evil.example/path',original)['state'],'invalid_source')


class OfflineBrowserTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which('chromium'), 'Chromium is required')
    async def test_observes_json_without_exposing_secrets(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser=await playwright.chromium.launch(headless=True,executable_path=shutil.which('chromium'))
            try:
                context=await browser.new_context()
                async def fixture(route):
                    request=route.request
                    if '/stats/match/' in request.url:
                        html='<iframe src="https://statshub.yunai.tech/winline/ru/match/123"></iframe>'
                        await route.fulfill(status=200,content_type='text/html',body=html)
                    elif '/winline/ru/match/' in request.url:
                        html='''<div data-testid="match-header">Private team name</div><h3>Statistics</h3>
                        <script>window.__NEXT_DATA__={props:{token:'private-next'}};
                        fetch('https://sh.fn.yunai.tech/api/v1/match/123?secret=private-query');</script>'''
                        await route.fulfill(status=200,content_type='text/html',body=html)
                    elif '/api/v1/match/' in request.url:
                        body=json.dumps({'doc':[{'data':{'team':{'name':'private-value'},'matches':[{'score':2}]}}]})
                        await route.fulfill(status=200,headers={'content-type':'application/json','content-length':str(len(body)),
                                                               'access-control-allow-origin':'*'},body=body)
                    else:
                        raise AssertionError('Unexpected request: '+request.url)
                await context.route('**/*',fixture)
                page=await context.new_page()
                result=await inspect_page(page,'https://winline.ru/stats/match/123',1)
                self.assertTrue(result['frames'][0]['header'])
                self.assertTrue(result['frames'][0]['next_data'])
                json_entries=[item for item in result['responses'] if item.get('shape')]
                self.assertEqual(json_entries[0]['shape']['keys'],['doc'])
                encoded=json.dumps(result)
                for value in ('private-value','private-query','private-next','Private team name','token='):
                    self.assertNotIn(value,encoded)
                with patch('scripts.inspect_statistics_network.direct_request',return_value={'state':'ok','same_matches':True}):
                    provider_result=await inspect_page(page,'https://winline.ru/stats/match/123',1,provider_only=True,test_direct=True)
                self.assertTrue(provider_result['responses'])
                self.assertTrue(all(item['endpoint'].startswith('sh.fn.yunai.tech/') for item in provider_result['responses']))
                self.assertEqual(provider_result['direct_test']['state'],'ok')
                self.assertEqual(provider_result['direct_test']['browser_match_count'],1)
                await context.close()
            finally:
                await browser.close()


if __name__=='__main__': unittest.main()
