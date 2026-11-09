import base64
import tempfile
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from account_helpers import signed_in
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from matchlab import push
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository


def subscription():
    public = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )
    return {
        "endpoint": "https://fcm.googleapis.com/fcm/send/fixture-endpoint",
        "keys": {
            "p256dh": base64.urlsafe_b64encode(public).rstrip(b"=").decode(),
            "auth": base64.urlsafe_b64encode(b"a" * 16).rstrip(b"=").decode(),
        },
    }


class PushTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        key = ec.generate_private_key(ec.SECP256R1())
        self.key = self.root / "private.pem"
        self.key.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        public = (
            base64.urlsafe_b64encode(
                key.public_key().public_bytes(
                    serialization.Encoding.X962,
                    serialization.PublicFormat.UncompressedPoint,
                )
            )
            .rstrip(b"=")
            .decode()
        )
        self.settings = Settings(
            self.root,
            vapid_private_key=str(self.key),
            vapid_public_key=public,
            vapid_subject="https://service.example",
        )
        self.app = create_app(self.settings)
        self.client = self.stack.enter_context(TestClient(self.app))
        self.repo = Repository(self.settings.database)
        signed_in(self.client, self.repo, "admin_fixture", "admin")
        with self.repo.connection() as db:
            db.execute(
                "UPDATE user_sessions SET elevated_at=?",
                (datetime.now(UTC).isoformat(),),
            )
        self.sub = subscription()

    def tearDown(self):
        self.stack.close()

    def draft(self):
        response = self.client.post(
            "/api/admin/push",
            json={
                "title": "Новый матч",
                "body": "Подборка матчей",
                "url": "/#matches",
                "segment": "all",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def queue(self):
        self.assertEqual(
            self.client.post("/api/push/subscribe", json=self.sub).status_code, 200
        )
        campaign = self.draft()
        response = self.client.post(
            f"/api/admin/push/{campaign}/send", json={"confirm_audience": 1}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return campaign

    def test_subscription_opt_in_and_known_gateway_validation(self):
        for endpoint in (
            "http://127.0.0.1/private",
            "https://fcm.googleapis.com.evil.example/send",
            "https://fcm.googleapis.com:bad/send",
            123,
        ):
            value = {**self.sub, "endpoint": endpoint}
            self.assertEqual(
                self.client.post("/api/push/subscribe", json=value).status_code, 422
            )
        self.assertEqual(
            self.client.post("/api/push/subscribe", json=self.sub).status_code, 200
        )
        self.assertEqual(
            self.client.get("/api/admin/push").json()["audiences"]["all"], 1
        )
        self.client.post(
            "/api/push/unsubscribe", json={"endpoint": self.sub["endpoint"]}
        )
        self.assertEqual(
            self.client.get("/api/admin/push").json()["audiences"]["all"], 0
        )

    def test_send_requires_preview_count_and_cannot_enqueue_twice(self):
        self.client.post("/api/push/subscribe", json=self.sub)
        campaign = self.draft()
        self.assertEqual(
            self.client.post(
                f"/api/admin/push/{campaign}/send", json={"confirm_audience": 2}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                f"/api/admin/push/{campaign}/send", json={"confirm_audience": 1}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                f"/api/admin/push/{campaign}/send", json={"confirm_audience": 1}
            ).status_code,
            409,
        )
        with self.repo.connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM push_outbox").fetchone()[0], 1
            )

    def test_sender_encrypts_payload_and_disallows_http_redirects(self):
        self.queue()
        row = push.claim(self.repo)
        import requests

        response = requests.Response()
        response.status_code = 201
        response._content = b""
        with patch(
            "requests.sessions.Session.request", return_value=response
        ) as network:
            self.assertEqual(push.deliver(self.settings, row), 201)
        kwargs = network.call_args.kwargs
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("Подборка".encode(), kwargs["data"])
        self.assertIn("Authorization", kwargs["headers"])

    def test_worker_records_acceptance_and_does_not_send_same_row_again(self):
        self.queue()
        sender = lambda *_: 201
        self.assertTrue(push.work_once(self.repo, self.settings, sender))
        self.assertFalse(push.work_once(self.repo, self.settings, sender))
        campaign = self.client.get("/api/admin/push").json()["campaigns"][0]
        self.assertEqual(campaign["sent"], 1)
        self.assertEqual(campaign["status"], "complete")

    def test_expired_subscription_is_disabled_and_transient_errors_retry(self):
        self.queue()
        push.work_once(self.repo, self.settings, lambda *_: 503)
        with self.repo.connection() as db:
            row = db.execute("SELECT * FROM push_outbox").fetchone()
            self.assertEqual(row["status"], "retry")
            self.assertEqual(row["attempts"], 1)
            db.execute(
                "UPDATE push_outbox SET next_at=?",
                ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),),
            )
        push.work_once(self.repo, self.settings, lambda *_: 410)
        with self.repo.connection() as db:
            self.assertEqual(
                db.execute("SELECT enabled FROM push_subscriptions").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT status FROM push_outbox").fetchone()[0], "expired"
            )

    def test_cancel_prevents_pending_delivery_and_disabled_users_receive_nothing(self):
        campaign = self.queue()
        self.client.post(f"/api/admin/push/{campaign}/cancel", json={})
        sender = unittest.mock.Mock(return_value=201)
        self.assertFalse(push.work_once(self.repo, self.settings, sender))
        sender.assert_not_called()

    def test_scheduled_campaign_waits_and_external_notification_urls_are_rejected(self):
        self.client.post("/api/push/subscribe", json=self.sub)
        for url in ("https://evil.example", "//evil.example", "/api/admin/overview"):
            self.assertEqual(
                self.client.post(
                    "/api/admin/push",
                    json={
                        "title": "Title",
                        "body": "Body",
                        "url": url,
                        "segment": "all",
                    },
                ).status_code,
                422,
            )
        response = self.client.post(
            "/api/admin/push",
            json={
                "title": "Позже",
                "body": "Потом",
                "url": "/#archive",
                "segment": "all",
                "scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
        campaign = response.json()["id"]
        self.client.post(
            f"/api/admin/push/{campaign}/send", json={"confirm_audience": 1}
        )
        self.assertIsNone(push.claim(self.repo))

    def test_lease_recovers_after_worker_crash(self):
        self.queue()
        first = push.claim(self.repo)
        self.assertIsNone(push.claim(self.repo))
        with self.repo.connection() as db:
            db.execute(
                "UPDATE push_outbox SET lease_until=?",
                ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),),
            )
        second = push.claim(self.repo)
        self.assertNotEqual(first["lease_id"], second["lease_id"])
        self.assertEqual(second["attempts"], 2)

    def test_repeated_crashes_do_not_bypass_the_attempt_limit(self):
        self.queue()
        push.claim(self.repo)
        with self.repo.connection() as db:
            db.execute(
                "UPDATE push_outbox SET attempts=5,lease_until=?",
                ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),),
            )
        self.assertIsNone(push.claim(self.repo))
        with self.repo.connection() as db:
            self.assertEqual(
                db.execute("SELECT status FROM push_outbox").fetchone()[0], "failed"
            )
