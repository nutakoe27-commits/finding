import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from matchlab import manage
from matchlab.config import Settings
from scripts.configure_domain import configuration
from scripts.install_push_worker import unit
from scripts.update_accounts_nginx import updated


class AccountDeploymentTests(unittest.TestCase):
    def test_canonical_origin_requires_https_and_normalizes_default_port(self):
        import os

        with (
            patch("matchlab.config.load_dotenv"),
            patch.dict(
                os.environ,
                {"PUBLIC_ORIGIN": "https://PROGNOZICH.ONLINE:443/"},
                clear=True,
            ),
        ):
            self.assertEqual(
                Settings.from_env().public_origin, "https://prognozich.online"
            )
        for origin in (
            "http://service.example",
            "https://service.example/path",
            "https://user:pass@service.example",
        ):
            with (
                patch("matchlab.config.load_dotenv"),
                patch.dict(os.environ, {"PUBLIC_ORIGIN": origin}, clear=True),
                self.assertRaises(ValueError),
            ):
                Settings.from_env()

    def test_backup_includes_committed_wal_and_does_not_apply_migration(self):
        import sqlite3
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "matchlab.sqlite3"
            with sqlite3.connect(path) as source:
                source.execute("PRAGMA journal_mode=WAL")
                source.execute("CREATE TABLE before_upgrade(value TEXT)")
                source.execute("INSERT INTO before_upgrade VALUES ('committed')")
                source.commit()
                with (
                    patch.object(
                        manage.Settings, "from_env", return_value=Settings(root)
                    ),
                    patch.object(sys, "argv", ["manage", "backup"]),
                    patch.object(manage.Repository, "initialize") as migration,
                ):
                    manage.main()
                    migration.assert_not_called()
                backup = next((root / "backups").glob("matchlab-*.sqlite3"))
                with sqlite3.connect(backup) as copy:
                    self.assertEqual(
                        copy.execute("SELECT value FROM before_upgrade").fetchone()[0],
                        "committed",
                    )
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_existing_tls_acme_worker_guards_and_upstream_survive_account_update(self):
        current = configuration(
            "prognozich.online",
            8080,
            "/etc/ssl/site.pem",
            "/etc/ssl/key.pem",
            "/var/lib/acme",
        )
        import re

        legacy = re.sub(r"(?m)^limit_req_zone .*matchlab_auth_.*\n", "", current)
        legacy = re.sub(r"(?ms)^    location ~ \^/api/\(auth/.*?^    }\n", "", legacy)
        legacy = re.sub(r"(?m)^    if \(\$scheme = http\).*\n", "", legacy)
        result = updated(legacy, "prognozich.online")
        for required in (
            "ssl_certificate /etc/ssl/site.pem",
            "root /var/lib/acme",
            "archive/(ingest|watch)",
            "proxy_pass http://127.0.0.1:8080",
            "return 308 https://prognozich.online$request_uri",
            "auth/(login|register)|admin/reauth",
        ):
            self.assertIn(required, result)
        self.assertEqual(updated(result, "prognozich.online"), result)

    def test_nginx_refuses_unknown_site_and_http_only_account_deployment(self):
        with self.assertRaises(ValueError):
            updated(configuration("prognozich.online", 8080), "prognozich.online")
        tls = configuration(
            "prognozich.online", 8080, "/etc/ssl/site.pem", "/etc/ssl/key.pem"
        )
        with self.assertRaises(ValueError):
            updated(
                tls.replace("# Managed by Matchlab HTTP domain", "# Other project"),
                "prognozich.online",
            )

    def test_push_unit_is_separate_resource_limited_and_preserves_app_identity(self):
        content = unit(
            Path("/root/matchlab"), Path("/root/matchlab/.local"), "root", "root"
        )
        self.assertIn("MemoryMax=256M", content)
        self.assertIn("CPUQuota=20%", content)
        self.assertIn("python -m matchlab.push", content)
        self.assertNotIn("ExecStop=", content)
        with self.assertRaises(ValueError):
            unit(Path("/path with spaces"), Path("/tmp/state"), "root", "root")

    def test_env_update_preserves_model_and_collector_settings_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "QWEN_MODEL=model-name\nBOOKMAKER_INGEST_TOKEN=fixture-key\n"
            )
            manage.write_env(path, {"WEBPUSH_PUBLIC_KEY": "public-key"})
            self.assertIn("QWEN_MODEL=model-name", path.read_text())
            self.assertIn("BOOKMAKER_INGEST_TOKEN=fixture-key", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_push_initialization_is_idempotent_and_never_overwrites_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(root, public_origin="https://service.example")
            with patch.object(manage, "ROOT", root):
                manage.init_push(settings)
            before = (root / "webpush-private.pem").read_bytes()
            self.assertEqual(
                (root / "webpush-private.pem").stat().st_mode & 0o777, 0o600
            )
            with patch.object(manage, "ROOT", root):
                manage.init_push(settings)
            self.assertEqual(before, (root / "webpush-private.pem").read_bytes())
            self.assertNotIn("PRIVATE KEY", (root / ".env").read_text())
