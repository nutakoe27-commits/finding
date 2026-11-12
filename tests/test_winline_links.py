"""Public match redirects keep sport/fixture identity and referral deep links."""
from datetime import datetime,timezone
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs,urlsplit
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.growth import outbound,referral_template
from matchlab.sports import match_url
from matchlab.storage import Repository
from sports_fixtures import document,PROJECTIONS


class WinlineLinksTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.settings=Settings(Path(folder.name),'','')
        self.client=TestClient(create_app(self.settings));self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.repo=Repository(self.settings.database)

    def test_generic_stream_route_becomes_short_sport_match_link(self):
        for sport,slug in [('football','futbol'),('hockey','xokkej'),('tennis','tennis'),('basketball','basketbol'),('volleyball','volejbol'),('table_tennis','nastolijnyj_tennis')]:
            event={'source_event_id':'17130653','sport':sport,'url':'https://winline.ru/stavki/event/17130653'}
            with self.subTest(sport=sport):
                self.assertEqual(match_url(event),f'https://winline.ru/stavki/sport/{slug}/17130653')

    def test_stored_observed_slug_is_preserved_but_queries_and_untrusted_hosts_are_not(self):
        event={'source_event_id':'123','sport':'biatlon','url':'https://winline.ru/stavki/sport/biatlon/country/league/123?track=1'}
        self.assertEqual(match_url(event),'https://winline.ru/stavki/sport/biatlon/123')
        event['url']='https://evil.example/stavki/sport/biatlon/123'
        self.assertIsNone(match_url(event))
        event.update(sport='hockey',url='https://winline.ru/stavki/sport/futbol/999')
        self.assertEqual(match_url(event),'https://winline.ru/stavki/sport/xokkej/123')
        event['url']='https://[malformed'
        self.assertEqual(match_url(event),'https://winline.ru/stavki/sport/xokkej/123')

    def test_legacy_and_stream_buttons_redirect_to_exact_match_ignoring_query_injection(self):
        raw=document(PROJECTIONS[1]);self.repo.import_event_markets(raw)
        response=self.client.get('/go/winline/91002?source=legacy&url=https://evil.example',follow_redirects=False)
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.headers['location'],'https://winline.ru/stavki/sport/xokkej/91002')
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(response.headers['x-robots-tag'],'noindex, nofollow')
        with patch('matchlab.archive.document',return_value={**raw,'url':'https://winline.ru/stavki/event/91002'}):
            response=self.client.get('/go/winline/91002?source=stream',follow_redirects=False)
        self.assertEqual(response.headers['location'],'https://winline.ru/stavki/sport/xokkej/91002')
        self.assertEqual(response.headers['x-robots-tag'],'noindex, nofollow')
        self.assertEqual(self.client.get('/go/winline/999?source=legacy',follow_redirects=False).status_code,404)

    def test_referral_receives_encoded_match_destination(self):
        raw=document(PROJECTIONS[1]);self.repo.import_event_markets(raw)
        template=referral_template('https://partner.example/click?url={url}&event={event_id}')
        with self.repo.connection() as db:db.execute('INSERT INTO site_settings VALUES (?,?,?)',('winline_referral',template,datetime.now(timezone.utc).isoformat()))
        response=self.client.get('/go/winline/91002?source=legacy',follow_redirects=False)
        args=parse_qs(urlsplit(response.headers['location']).query)
        self.assertEqual(args,{'url':['https://winline.ru/stavki/sport/xokkej/91002'],'event':['91002']})
        self.assertEqual(response.headers['x-robots-tag'],'noindex, nofollow')

    def test_head_exposes_the_same_exclusion_and_destination_without_recording_a_click(self):
        self.repo.import_event_markets(document(PROJECTIONS[1]))
        with patch('matchlab.growth.analytics_allowed',return_value=True),patch('matchlab.growth.record') as record:
            response=self.client.head('/go/winline/91002?source=legacy',follow_redirects=False)
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.headers['location'],'https://winline.ru/stavki/sport/xokkej/91002')
        self.assertEqual(response.headers['x-robots-tag'],'noindex, nofollow')
        self.assertEqual(response.content,b'')
        record.assert_not_called()

    def test_old_landing_only_referral_cannot_replace_match_destination(self):
        raw=document(PROJECTIONS[1]);self.repo.import_event_markets(raw)
        with self.repo.connection() as db:db.execute('INSERT INTO site_settings VALUES (?,?,?)',('winline_referral','https://partner.example/landing',datetime.now(timezone.utc).isoformat()))
        self.assertEqual(outbound(self.repo,'91002','legacy'),'https://winline.ru/stavki/sport/xokkej/91002')
        with self.assertRaises(HTTPException):referral_template('https://partner.example/landing')
        self.assertEqual(referral_template(''),'')
