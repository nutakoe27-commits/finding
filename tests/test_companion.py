from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient
from matchlab import accounts
from matchlab.companion import PERSONA, create_chat_app
from matchlab.config import Settings


class CompanionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = Settings(data_dir=Path(tmp.name), qwen_base_url="http://qwen.test/v1",
                            qwen_model="test", qwen_max_tokens=1400)
        self.client = self.enterContext(TestClient(create_chat_app(settings)))
        self.provider = self.enterContext(patch("matchlab.qwen._request", return_value={
            "choices": [{"finish_reason": "stop", "message": {"content": "Привет!"}}],
        }))

    def register(self, name):
        response = self.client.post('/api/auth/register',json=dict(username=name,password="local-password-test",
            terms_accepted=True,personal_data_consent=True,age_confirmed=True,legal_version=accounts.LEGAL_VERSION))
        self.assertEqual(response.status_code,201,response.text)
        self.client.headers['X-CSRF-Token']=response.json()['csrf']

    def test_personal_history_retries_and_clear(self):
        self.register('first')
        req=dict(message="Привет",revision=0,id=str(uuid4()))
        for _ in range(2):
            response=self.client.post('/api/chat',json=req)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['revision'],1)
        self.provider.assert_called_once()
        self.assertEqual(self.client.post('/api/chat',json={**req,'id':str(uuid4())}).status_code,409)
        self.assertEqual(self.client.delete('/api/chat',headers={'content-type':'application/json'}).status_code,200)
        self.assertEqual(self.client.get('/api/chat').json()['turns'],[])
        self.assertEqual(self.client.post('/api/chat',json=req).status_code,409)
        self.assertEqual(self.client.post('/api/chat',json={**req,'id':str(uuid4()),'revision':2}).status_code,200)
        self.client.cookies.clear()
        self.register('second')
        self.assertEqual(self.client.get('/api/chat').json()['turns'],[])

    def test_free_and_protected(self):
        self.assertEqual(self.client.get('/api/chat').status_code,401)
        self.register('account')
        for path in ['/api/billing/orders','/api/billing/subscriptions']:
            self.assertNotIn(self.client.post(path,json={}).status_code,[200,201,202])
        req=dict(message="Как дела?",revision=0,id=str(uuid4()))
        self.assertEqual(self.client.post('/api/chat',json=req,headers={'x-csrf-token':'wrong'}).status_code,403)
        self.provider.side_effect=ValueError('offline')
        self.assertEqual(self.client.post('/api/chat',json=req).status_code,503)
        self.assertEqual(self.client.get('/api/chat').json()['turns'],[])
        self.assertEqual(self.client.get('/api/billing/wallet').json()['orders'],[])

    def test_followup_uses_chat_voice_and_actual_history(self):
        self.register("reader")
        for revision, message in enumerate(["Привет, Мира", "Я к тебе за советом"]):
            response = self.client.post("/api/chat", json={
                "message": message, "revision": revision, "id": str(uuid4()),
            })
            self.assertEqual(response.status_code, 200, response.text)

        payload = self.provider.call_args.kwargs["json"]
        self.assertEqual(payload["messages"], [
            {"role": "system", "content": PERSONA},
            {"role": "user", "content": "Привет, Мира"},
            {"role": "assistant", "content": "Привет!"},
            {"role": "user", "content": "Я к тебе за советом"},
        ])
        self.assertEqual(payload["temperature"], 0.75)
        self.assertEqual(payload["max_tokens"], 1400)
        self.assertFalse(payload["chat_template_kwargs"]["enable_thinking"])
