from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from matchlab import accounts
from matchlab.companion import create_chat_app
from matchlab.config import Settings


class PhotoIntegration(unittest.TestCase):
    def setUp(self):
        state = Path(self.enterContext(TemporaryDirectory()))
        self.site = self.enterContext(TestClient(create_chat_app(Settings(data_dir=state))))
        self.key = state / "photo-worker.key"
        self.key.write_text("test-worker-" * 5)
        self.gpu = {"Authorization": "Bearer " + self.key.read_text()}
        self.register("mira_reader")
        self.assertEqual(self.site.post("/api/photo-worker/claim", headers=self.gpu).status_code, 204)

    def register(self, name):
        self.site.cookies.clear()
        r = self.site.post("/api/auth/register", json=dict(username=name, password="only-local-test",
            terms_accepted=True, personal_data_consent=True, age_confirmed=True, legal_version=accounts.LEGAL_VERSION))
        self.assertEqual(r.status_code, 201, r.text)
        self.site.headers["X-CSRF-Token"] = r.json()["csrf"]

    def create(self, **fields):
        return self.site.post("/api/chat/photos", json={"id":str(uuid4()), "scene":"selfie", **fields})

    def claim(self):
        return self.site.post("/api/photo-worker/claim", headers=self.gpu).json()

    def finish(self, job, data):
        return self.site.post(f"/api/photo-worker/{job['id']}/{job['claim']}", content=data,
                              headers={**self.gpu, "Content-Type":"image/jpeg"})

    def test_delivery_with_parallel_chat_owner_and_clear(self):
        self.assertEqual(self.site.post("/api/photo-worker/claim").status_code, 403)
        self.assertEqual(self.create(scene="nude").status_code, 422)
        self.assertEqual(self.create(scene=["bad"]).status_code, 422)
        key = str(uuid4())
        for _ in range(2):
            self.assertEqual(self.create(id=key, scene="nude", adult=True).status_code, 202)
        self.assertEqual(self.create().status_code, 409)
        job = self.claim()
        self.assertEqual(job["id"], key)
        self.assertEqual(self.site.post("/api/photo-worker/claim", headers=self.gpu).status_code, 204)
        with patch("matchlab.qwen._completion", return_value="Да, помню."):
            self.assertEqual(self.site.post("/api/chat", json={"id":str(uuid4()), "message":"Привет",
                                                              "revision":0}).status_code, 200)
        self.assertEqual(self.finish(job, b"broken").status_code, 422)
        image = b"\xff\xd8\xff\xe0test\xff\xd9"
        for _ in range(2):
            self.assertEqual(self.finish(job, image).status_code, 204)
        url = "/api/chat/photos/" + key + "/image"
        self.assertEqual(self.site.get(url).content, image)
        self.assertEqual(self.site.get("/api/chat/photos").json()["photos"][0]["state"], "ready")
        self.register("other_reader")
        self.assertEqual(self.site.get(url).status_code, 404)
        self.assertEqual(self.site.get("/api/chat/photos").json()["photos"], [])
        self.site.cookies.clear()
        login = self.site.post("/api/auth/login", json={"username":"mira_reader","password":"only-local-test"})
        self.site.headers["X-CSRF-Token"] = login.json()["csrf"]
        self.assertEqual(self.create().status_code, 202)
        active = self.claim()
        self.site.delete("/api/chat", headers={"content-type":"application/json"})
        self.assertEqual(self.finish(active, image).status_code, 410)
        self.assertEqual(self.site.get(url).status_code, 404)
        self.assertEqual(self.site.get("/api/chat/photos").json()["photos"], [])

    def test_contextual_photo_in_message_and_retry(self):
        gpu = {**self.gpu, "X-Mira-Photo-Context": "1"}
        self.site.post("/api/photo-worker/claim", headers=gpu)
        with patch("matchlab.qwen._completion", return_value="Я сейчас на набережной в красном шарфе."):
            self.site.post("/api/chat", json={"id": str(uuid4()), "revision": 0, "message": "Где гуляешь?"})
        send = {"id": str(uuid4()), "revision": 1, "message": "А можно тебя там увидеть?"}
        plan = '[[PHOTO]]{"scene":"portrait","caption":"На набережной","prompt":"red scarf, evening waterfront"}'
        with patch("matchlab.qwen._completion", return_value=plan) as model:
            result = self.site.post("/api/chat", json=send)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["photo"]["caption"], "На набережной")
            self.assertNotIn("[[PHOTO]]", result.text)
            self.site.post("/api/chat", json=send).raise_for_status()
            self.assertEqual(model.call_count, 1)
            self.assertIn("в красном шарфе", model.call_args.args[1][-2]["content"])
        self.assertEqual(self.site.post("/api/photo-worker/claim", headers=self.gpu).status_code, 204)
        job = self.site.post("/api/photo-worker/claim", headers=gpu).json()
        self.assertEqual(job["photo_prompt"], "red scarf, evening waterfront")
        self.finish(job, b"").raise_for_status()
        self.create(retry_of=job["id"]).raise_for_status()
        again = self.site.post("/api/photo-worker/claim", headers=gpu).json()
        self.assertEqual(again["photo_prompt"], job["photo_prompt"])
        self.assertNotEqual(again["id"], job["id"])

    def test_chat_photo_boundaries_and_stale_worker(self):
        adult = '[[PHOTO]]{"scene":"nude","caption":"В студии","prompt":"nude studio portrait"}'
        with patch("matchlab.qwen._completion", return_value=adult):
            msg = {"id": str(uuid4()), "revision": 0, "message": "Фото не надо"}
            self.assertIsNone(self.site.post("/api/chat", json=msg).json()["photo"])
            msg.update(id=str(uuid4()), revision=1, message="Пришли селфи")
            self.assertIsNone(self.site.post("/api/chat", json=msg).json()["photo"])
            msg.update(id=str(uuid4()), revision=2, message="Да, давай")
            self.assertEqual(self.site.post("/api/chat", json=msg).status_code, 503)
            self.site.post("/api/photo-worker/claim", headers={**self.gpu, "X-Mira-Photo-Context":"1"})
            self.assertEqual(self.site.post("/api/chat", json=msg).json()["photo"]["scene"], "nude")

    def test_explicit_request_recovers_text_only_ack(self):
        self.site.post("/api/photo-worker/claim", headers={**self.gpu, "X-Mira-Photo-Context": "1"})
        plan = '[[PHOTO]]{"scene":"portrait","caption":"В синем","prompt":"blue dress, candlelight"}'
        with patch("matchlab.qwen._completion", side_effect=["Сейчас!", plan]):
            r = self.site.post("/api/chat", json={"id": str(uuid4()), "revision": 0,
                               "message": "Пришли фото в синем платье при свечах"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["photo"]["scene"], "portrait")

    def test_queue_failure_timeout_and_retry(self):
        self.create()
        self.assertEqual(self.finish(self.claim(), b"").status_code, 204)
        self.assertEqual(self.create().status_code, 202)
        job = self.claim()
        repo = self.site.app.state.repository
        with repo.connection() as db:
            db.execute("UPDATE companion_photos SET deadline=0 WHERE id=?", (job["id"],))
        self.assertEqual(self.finish(job, b"").status_code, 410)
        self.assertEqual([p['state'] for p in self.site.get("/api/chat/photos").json()['photos']],
                         ["failed","failed"])
        self.assertEqual(self.create().status_code, 202)
