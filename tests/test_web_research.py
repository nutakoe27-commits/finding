import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from matchlab import web_research as web
from matchlab.config import Settings

EVENT = {'home': 'Команда A', 'away': 'Команда B', 'sport_label': 'Футбол',
         'competition': 'Лига', 'event_kind': 'match', 'notes': 'PRIVATE'}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        web._cache.clear()
        self.settings = Settings(Path('/tmp'), wigolo_mcp_url='http://127.0.0.1:13333/mcp',
                                 wigolo_mcp_token_file='/tmp/test-token')

    def test_only_match_metadata_in_queries(self):
        queries = web._queries(EVENT)
        self.assertEqual(len(queries), 3)
        self.assertFalse(any('PRIVATE' in q for q in queries))
        self.assertEqual(web._queries({**EVENT, 'event_kind': 'outright'}), [])
        self.assertEqual(web._queries({**EVENT, 'away': ''}), [])

    def test_only_loopback_mcp_and_public_source_links(self):
        self.assertEqual(web._endpoint('http://127.0.0.1:13333/mcp'), 'http://127.0.0.1:13333/mcp')
        for url in ['http://example.com:13333/mcp', 'http://127.0.0.1:13333/mcp?token=x',
                    'https://127.0.0.1:13333/mcp', 'http://user:pw@127.0.0.1:13333/mcp',
                    'http://127.0.0.1:13333/v1/search']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                web._endpoint(url)
        for url in ['javascript:alert(1)', 'http://127.0.0.1/a', 'http://internal.local/a',
                    'http://u:p@example.org/a', 'http://example.org:8080/a']:
            self.assertEqual(web._public_link(url), '')
        self.assertEqual(web._public_link('https://sports.example/news'), 'https://sports.example/news')

    def test_result_references_are_bounded_deduplicated_and_dated_honestly(self):
        payload = {'results': [
            {'title': 'Новость', 'url': 'https://sports.example/1', 'snippet': 'short',
             'freshness_signal': {'published_date': '2026-11-01', 'inferred': True}},
            {'title': 'Повтор', 'url': 'https://sports.example/1', 'snippet': 'again'},
            {'title': 'bad', 'url': 'javascript:alert(1)', 'snippet': 'x'},
            {'title': 'missing', 'url': 'https://sports.example/2'},
            {'title': 'Без даты', 'url': 'https://sports.example/3', 'snippet': 'text'},
        ], 'evidence': [{'url': 'https://sports.example/1', 'excerpt': 'Выдержка'}]}
        rows = web._extract(payload)
        self.assertEqual([r['id'] for r in rows], [1, 2])
        self.assertEqual(rows[0]['excerpt'], 'Выдержка')
        self.assertTrue(rows[0]['date_inferred'])
        self.assertIsNone(rows[1]['published_at'])

    def test_search_is_shared_temporarily_and_failure_does_not_crash(self):
        async def succeed(url, token, queries):
            return ([{'id': 1, 'url': 'https://example.org/a', 'title': 't', 'excerpt': 'x'}], False)
        with patch.object(web, '_search', side_effect=succeed) as search:
            first = web.research(self.settings, EVENT)
            first['sources'].clear()
            second = web.research(self.settings, EVENT)
            self.assertEqual(second['state'], 'ready')
            self.assertEqual(len(second['sources']), 1)
            self.assertEqual(search.call_count, 1)
        web._cache.clear()
        with patch.object(web, '_search', side_effect=RuntimeError('secret')):
            result = web.research(self.settings, EVENT)
            self.assertEqual(result['state'], 'unavailable')
            self.assertNotIn('secret', str(result))
        self.assertEqual(web.research(Settings(Path('/tmp')), EVENT)['state'], 'disabled')

    def test_token_required_before_connecting(self):
        with tempfile.TemporaryDirectory() as root:
            token = Path(root) / 'token'
            token.write_text('invalid')
            settings = Settings(Path(root), wigolo_mcp_url='http://127.0.0.1:13333/mcp',
                                wigolo_mcp_token_file=str(token))
            self.assertEqual(web.research(settings, EVENT)['state'], 'unavailable')
