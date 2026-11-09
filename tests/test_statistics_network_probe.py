"""Offline network inspection and redaction for the statistics diagnostic."""
import json
import shutil
import unittest

from scripts.inspect_statistics_network import endpoint, inspect_page, shape, widget_url


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
                        fetch('/api/v1/match/123?secret=private-query');</script>'''
                        await route.fulfill(status=200,content_type='text/html',body=html)
                    elif '/api/v1/match/' in request.url:
                        body=json.dumps({'data':{'match':{'token':'private-value','score':2}}})
                        await route.fulfill(status=200,headers={'content-type':'application/json','content-length':str(len(body))},body=body)
                    else:
                        raise AssertionError('Unexpected request: '+request.url)
                await context.route('**/*',fixture)
                page=await context.new_page()
                result=await inspect_page(page,'https://winline.ru/stats/match/123',1)
                self.assertTrue(result['frames'][0]['header'])
                self.assertTrue(result['frames'][0]['next_data'])
                json_entries=[item for item in result['responses'] if item.get('shape')]
                self.assertEqual(json_entries[0]['shape']['keys'],['data'])
                encoded=json.dumps(result)
                for value in ('private-value','private-query','private-next','Private team name','token='):
                    self.assertNotIn(value,encoded)
                await context.close()
            finally:
                await browser.close()


if __name__=='__main__': unittest.main()
