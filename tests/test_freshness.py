"""Prematch cadence must not inherit the stricter live observation window."""
from datetime import datetime,timedelta,timezone
import unittest

from test_archive import header
from matchlab.archive import project
from matchlab.event_analysis import analysis_context,request_key
from matchlab.freshness import quote_ttl


class FreshnessTests(unittest.TestCase):
    def snapshot(self,live,seconds):
        value=header(live=live)
        at=(datetime.now(timezone.utc)-timedelta(seconds=seconds)).isoformat()
        value['observed_at']=at;value['primary']['observed_at']=at
        return project(value)

    def test_ninety_second_old_prematch_quotes_remain_usable(self):
        doc=self.snapshot(False,90)
        self.assertEqual(quote_ttl(doc),180)
        self.assertFalse(doc['is_stale']);self.assertFalse(doc['markets'][0]['is_stale'])
        self.assertFalse(analysis_context(doc)['snapshot']['is_stale'])

    def test_same_age_in_live_is_stale(self):
        doc=self.snapshot(True,90)
        self.assertEqual(quote_ttl(doc),45)
        self.assertTrue(doc['is_stale']);self.assertTrue(doc['markets'][0]['is_stale'])
        self.assertTrue(analysis_context(doc)['snapshot']['is_stale'])

    def test_old_prematch_quotes_and_cache_identity_expire(self):
        doc=self.snapshot(False,90)
        key=request_key(doc,None,None,'')
        doc['capture_started_at']=(datetime.now(timezone.utc)-timedelta(seconds=240)).isoformat()
        self.assertTrue(analysis_context(doc)['snapshot']['is_stale'])
        self.assertNotEqual(request_key(doc,None,None,''),key)
        self.assertTrue(self.snapshot(False,240)['markets'][0]['is_stale'])


if __name__=='__main__':unittest.main()
