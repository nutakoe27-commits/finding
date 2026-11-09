"""Authentication, privacy, privilege boundaries, CSRF and transactional growth flows."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_product import event

from matchlab import accounts
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository

PASSWORD = "a-unique-password-12345"


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.settings = Settings(self.root, "http://127.0.0.1:9/v1", "fixture-model")
        self.app = create_app(self.settings)
        self.client = self.stack.enter_context(TestClient(self.app))
        self.repo = Repository(self.settings.database)
        self.repo.import_event_markets(event("123"))

    def tearDown(self):
        self.stack.close()

    def register(self, client=None, username="reader_1"):
        client = client or self.client
        response = client.post(
            "/api/auth/register",
            json={"username": username, "password": PASSWORD, "role": "admin"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        client.headers["X-CSRF-Token"] = response.json()["csrf"]
        return response.json()["user"]

    def second(self):
        return self.stack.enter_context(TestClient(self.app))

    def admin(self):
        user_id = accounts.create_user(self.repo, "admin_local", PASSWORD, "admin")
        response = self.client.post(
            "/api/auth/login", json={"username": "admin_local", "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.client.headers["X-CSRF-Token"] = response.json()["csrf"]
        return user_id

    def elevate(self):
        response = self.client.post("/api/admin/reauth", json={"password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)

    def promo(self, max_uses=10):
        stamp = datetime.now(UTC)
        response = self.client.post(
            "/api/admin/promos",
            json={
                "code": "WELCOME_5",
                "title": "Старт",
                "bonus": 5,
                "max_uses": max_uses,
                "starts_at": (stamp - timedelta(days=1)).isoformat(),
                "ends_at": (stamp + timedelta(days=1)).isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        promo_id = response.json()["id"]
        self.client.patch("/api/admin/promos/" + promo_id, json={"status": "active"})
        return promo_id

    def test_public_catalogue_but_all_ai_routes_require_real_authentication(self):
        self.assertEqual(self.client.get("/api/bookmakers/events").status_code, 200)
        self.assertEqual(self.client.get("/api/archive/results").status_code, 200)
        with patch("matchlab.api.qwen.explain_event") as qwen:
            for path in (
                "/api/archive/events/123/explain",
                "/api/bookmakers/events/123/explain",
                "/api/explain",
            ):
                self.assertEqual(self.client.post(path, json={}).status_code, 401, path)
            qwen.assert_not_called()
        self.assertEqual(self.client.get("/api/analyses").status_code, 401)

    def test_registration_never_accepts_admin_role_and_hashes_passwords_and_tokens(
        self,
    ):
        user = self.register()
        self.assertEqual(user["role"], "user")
        self.assertNotIn("password", json.dumps(user))
        self.assertNotIn("session", json.dumps(user))
        with self.repo.connection() as db:
            stored = db.execute("SELECT password_hash FROM users").fetchone()[0]
            token = db.execute("SELECT token_hash FROM user_sessions").fetchone()[0]
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotEqual(stored, PASSWORD)
        self.assertNotEqual(token, self.client.cookies["prognozich_session"])
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/auth/register",
                json={"username": "READER_1", "password": PASSWORD},
            ).status_code,
            409,
        )

    def test_password_validation_does_not_echo_secret_input(self):
        for password in ("tiny", 1234, "x" * 129):
            response = self.client.post(
                "/api/auth/register",
                json={"username": "reader_1", "password": password},
            )
            self.assertEqual(response.status_code, 422)
            self.assertNotIn('"input"', response.text)
            if isinstance(password, str):
                self.assertNotIn(password, response.text)

    def test_csrf_and_origin_checks_guard_authenticated_mutations(self):
        self.register()
        token = self.client.headers.pop("X-CSRF-Token")
        self.assertEqual(
            self.client.post("/api/bookmakers/events/123/explain", json={}).status_code,
            403,
        )
        self.client.headers["X-CSRF-Token"] = token
        self.assertEqual(
            self.client.post(
                "/api/auth/logout", json={}, headers={"Origin": "https://evil.example"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/logout", json={}, headers={"Sec-Fetch-Site": "cross-site"}
            ).status_code,
            403,
        )
        self.assertIsNotNone(self.client.get("/api/auth/me").json()["user"])

    def test_logout_invalidates_the_server_session(self):
        self.register()
        stolen = self.client.cookies["prognozich_session"]
        self.assertEqual(self.client.post("/api/auth/logout", json={}).status_code, 200)
        other = self.second()
        other.cookies.set("prognozich_session", stolen)
        self.assertEqual(other.get("/api/analyses").status_code, 401)

    def test_password_change_revokes_other_devices_and_rotates_current_session(self):
        self.register()
        other = self.second()
        result = other.post(
            "/api/auth/login", json={"username": "reader_1", "password": PASSWORD}
        )
        self.assertEqual(result.status_code, 200)
        old = self.client.cookies["prognozich_session"]
        response = self.client.post(
            "/api/auth/password",
            json={"password": PASSWORD, "new_password": "another-long-password-999"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(self.client.cookies["prognozich_session"], old)
        self.assertEqual(other.get("/api/analyses").status_code, 401)
        self.assertEqual(
            other.post(
                "/api/auth/login", json={"username": "reader_1", "password": PASSWORD}
            ).status_code,
            401,
        )

    def test_private_analyses_notes_and_cache_are_scoped_to_the_owner(self):
        self.register()
        other = self.second()
        self.register(other, "reader_2")
        with patch(
            "matchlab.api.qwen.explain_event", return_value="Личный разбор"
        ) as model:
            first = self.client.post(
                "/api/bookmakers/events/123/explain", json={"notes": "Личная заметка"}
            )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(other.get("/api/analyses").json()["analyses"], [])
            self.assertEqual(
                other.get("/api/analyses/" + first.json()["id"]).status_code, 404
            )
            second = other.post(
                "/api/bookmakers/events/123/explain", json={"notes": "Личная заметка"}
            )
            self.assertEqual(second.status_code, 200, second.text)
            self.assertNotEqual(first.json()["id"], second.json()["id"])
            self.assertEqual(model.call_count, 2)
            cached = self.client.post(
                "/api/bookmakers/events/123/explain", json={"notes": "Личная заметка"}
            )
            self.assertTrue(cached.json()["cached"])
            self.assertEqual(model.call_count, 2)

    def test_admin_actions_require_step_up_and_cannot_promote_regular_users(self):
        self.admin()
        response = self.client.post("/api/admin/promos", json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers["X-Reauth-Required"], "1")
        self.elevate()
        other = self.second()
        user = self.register(other)
        self.assertEqual(
            self.client.patch(
                "/api/admin/users/" + user["id"], json={"role": "admin"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.patch(
                "/api/admin/users/" + user["id"], json={"enabled": False}
            ).status_code,
            200,
        )
        self.assertIsNone(other.get("/api/auth/me").json()["user"])
        self.assertEqual(
            other.post(
                "/api/auth/login", json={"username": "reader_1", "password": PASSWORD}
            ).status_code,
            401,
        )
        self.assertTrue(
            any(
                row["action"] == "user.disabled"
                for row in self.client.get("/api/admin/audit").json()["events"]
            )
        )

    def test_admin_cannot_disable_itself(self):
        admin = self.admin()
        self.elevate()
        self.assertEqual(
            self.client.patch(
                "/api/admin/users/" + admin, json={"enabled": False}
            ).status_code,
            403,
        )

    def test_promo_redemption_is_atomic_and_single_use(self):
        self.admin()
        self.elevate()
        self.promo(max_uses=1)
        first = self.second()
        second = self.second()
        self.register(first, "reader_1")
        self.register(second, "reader_2")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda client: (
                        client.post(
                            "/api/account/promo", json={"code": "welcome_5"}
                        ).status_code
                    ),
                    (first, second),
                )
            )
        self.assertEqual(sorted(results), [200, 409])
        with self.repo.connection() as db:
            self.assertEqual(
                db.execute("SELECT sum(bonus_analyses) FROM users").fetchone()[0], 5
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM promo_redemptions").fetchone()[0], 1
            )

    def test_failed_ai_request_does_not_spend_promotional_credit(self):
        object.__setattr__(self.settings, "ai_daily_limit", 1)
        user = self.register()
        with self.repo.connection() as db:
            db.execute("UPDATE users SET bonus_analyses=1 WHERE id=?", (user["id"],))
        with patch("matchlab.api.qwen.explain_event", return_value="Первый разбор"):
            self.assertEqual(
                self.client.post(
                    "/api/bookmakers/events/123/explain", json={}
                ).status_code,
                200,
            )
        with patch(
            "matchlab.api.qwen.explain_event", side_effect=ValueError("upstream")
        ):
            self.assertEqual(
                self.client.post(
                    "/api/bookmakers/events/123/explain",
                    json={"notes": "Другой запрос"},
                ).status_code,
                502,
            )
        self.assertEqual(
            self.client.get("/api/account/usage").json()["bonus_analyses"], 1
        )

    def test_limited_summary_returns_reserved_promotional_credit(self):
        object.__setattr__(self.settings, "ai_daily_limit", 1)
        user = self.register()
        with self.repo.connection() as db:
            db.execute("UPDATE users SET bonus_analyses=1 WHERE id=?", (user["id"],))
            db.execute("INSERT INTO ai_usage(id,user_id,at,kind,target,status,used_bonus) VALUES ('used',?,?, 'event','123','success',0)",
                       (user['id'], datetime.now(UTC).isoformat()))
        with patch('matchlab.qwen._completion', side_effect=AssertionError('model called')):
            result = self.client.post('/api/bookmakers/events/123/explain', json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['analysis_type'], 'limited_summary')
        self.assertEqual(self.client.get('/api/account/usage').json()['bonus_analyses'], 1)
        with self.repo.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM ai_usage WHERE status='limited'").fetchone()[0], 1)

    def test_referral_rule_uses_stored_event_and_does_not_accept_open_redirects(self):
        self.admin()
        self.elevate()
        response = self.client.get(
            "/go/winline/123?source=legacy&url=https://evil.example",
            follow_redirects=False,
        )
        self.assertIn("winline.ru/stavki/", response.headers["location"])
        template = "https://affiliate.example/click?target={url}&id={event_id}"
        self.assertEqual(
            self.client.put(
                "/api/admin/links", json={"template": template}
            ).status_code,
            200,
        )
        result = self.client.get(
            "/go/winline/123?source=legacy", follow_redirects=False
        )
        self.assertEqual(result.status_code, 302)
        self.assertTrue(
            result.headers["location"].startswith(
                "https://affiliate.example/click?target=https%3A%2F%2Fwinline.ru"
            )
        )
        for value in (
            "javascript:alert(1)",
            "https://x.example/{password}",
            "http://x.example/click",
        ):
            self.assertEqual(
                self.client.put(
                    "/api/admin/links", json={"template": value}
                ).status_code,
                422,
            )

    def test_real_analytics_do_not_expose_passwords_or_private_notes(self):
        self.admin()
        self.elevate()
        self.client.post(
            "/api/telemetry", json={"name": "page_view", "entity": "#matches"}
        )
        self.client.get("/go/winline/123?source=legacy", follow_redirects=False)
        overview = self.client.get("/api/admin/overview").json()
        self.assertEqual(overview["events"]["page_view"], 1)
        self.assertEqual(overview["events"]["outbound_click"], 1)
        self.assertNotIn("password_hash", json.dumps(overview))

    def test_production_cookie_is_secure_httponly_and_host_scoped(self):
        root = self.root / "secure"
        app = create_app(Settings(root, public_origin="https://testserver"))
        with TestClient(app, base_url="https://testserver") as client:
            response = client.post(
                "/api/auth/register",
                json={"username": "secure_reader", "password": PASSWORD},
            )
            cookie = response.headers["set-cookie"].lower()
            self.assertIn("__host-prognozich=", cookie)
            self.assertIn("secure", cookie)
            self.assertIn("httponly", cookie)
            self.assertIn("samesite=lax", cookie)
            self.assertNotIn("domain=", cookie)
            self.assertIsNotNone(client.get("/api/auth/me").json()["user"])

    def test_login_rate_limit_is_enforced(self):
        for _ in range(10):
            response = self.client.post(
                "/api/auth/login", json={"username": "unknown", "password": PASSWORD}
            )
            self.assertEqual(response.status_code, 401)
        response = self.client.post(
            "/api/auth/login", json={"username": "unknown", "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 429)

    def test_abandoned_ai_reservation_returns_bonus_exactly_once(self):
        user = self.register()
        with self.repo.connection() as db:
            db.execute(
                "INSERT INTO ai_usage(id,user_id,at,kind,target,status,used_bonus) VALUES ('abandoned',?,?,'event','123','pending',1)",
                (user["id"], (datetime.now(UTC) - timedelta(minutes=20)).isoformat()),
            )
        accounts.start_usage(self.repo, user, "event", "123", self.settings)
        accounts.start_usage(self.repo, user, "event", "123", self.settings)
        with self.repo.connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT bonus_analyses FROM users WHERE id=?", (user["id"],)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT status FROM ai_usage WHERE id='abandoned'"
                ).fetchone()[0],
                "failed",
            )
