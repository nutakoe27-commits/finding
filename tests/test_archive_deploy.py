import unittest

from scripts.configure_domain import configuration
from scripts.update_archive_nginx import updated


class ArchiveDeploymentTests(unittest.TestCase):
    def test_update_preserves_tls_acme_upstream_and_is_idempotent(self):
        current = configuration(
            "prognozich.online",
            8080,
            "/etc/ssl/cert.pem",
            "/etc/ssl/key.pem",
            "/var/lib/acme",
        )
        previous = current.replace("|archive/(ingest|watch)", "").replace(
            "|archive/events/[0-9]+/explain", ""
        )
        self.assertEqual(updated(previous, "prognozich.online"), current)
        self.assertEqual(updated(current, "prognozich.online"), current)

    def test_refuses_another_site_or_unrecognised_api_rules(self):
        current = configuration("prognozich.online", 8080)
        for text in (
            current.replace("# Managed by Matchlab HTTP domain", "# other site"),
            current.replace("refresh/(claim|finish)", "something-else"),
        ):
            # Force the legacy form, so the missing original rule must be detected.
            text = text.replace("|archive/(ingest|watch)", "")
            with self.assertRaises(ValueError):
                updated(text, "prognozich.online")
        with self.assertRaises(ValueError):
            updated(current, "other.example")
