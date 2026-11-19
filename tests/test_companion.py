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

    def test_admin_review_is_private_and_clear_removes_transcript(self):
        self.assertEqual(self.client.get('/admin').status_code, 401)
        self.register('writer')
        user = self.client.get('/api/auth/me').json()['user']
        detail = '/api/admin/companion/users/' + user['id']
        self.assertEqual(self.client.get(detail).status_code, 403)
        self.assertEqual(self.client.get('/api/admin/companion/users').status_code, 403)
        repo = self.client.app.state.repository
        import json
        with repo.connection() as db:
            db.execute("INSERT INTO companion_dialogs VALUES (?,1,?)", (user['id'],json.dumps([
                {'id': str(uuid4()), 'user': 'old private text', 'assistant': 'old reply'}
            ])))
        msg = {'message':'Привет из голосового', 'revision':1, 'id':str(uuid4()), 'channel':'voice'}
        self.assertEqual(self.client.post('/api/chat',json=msg).status_code, 200)
        accounts.create_user(repo, 'moderator', 'local-password-test', 'admin', True)
        with TestClient(self.client.app) as admin:
            admin.post('/api/auth/login',json={'username':'moderator','password':'local-password-test'})
            self.assertEqual(admin.get('/admin').status_code, 200)
            directory = admin.get('/api/admin/companion/users?search=writ').json()
            self.assertEqual([u['username'] for u in directory['users']], ['writer'])
            result = admin.get(detail)
            self.assertNotIn('old private text', result.text)
            self.assertEqual(result.headers['cache-control'], 'no-store')
            self.assertEqual([t['channel'] for t in result.json()['turns']], ['voice'])
            self.assertEqual(result.json()['events'][0]['action'], 'voice_turn')
            self.client.delete('/api/chat', headers={'content-type':'application/json'})
            self.assertEqual(admin.get(detail).json()['turns'], [])
            self.assertEqual(admin.get(detail).json()['events'][0]['action'], 'cleared')
        with repo.connection() as db:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM admin_audit WHERE action='companion.review'").fetchone()[0],0)

    def test_voice_requires_owner_and_consent_validates_pcm(self):
        import base64
        from unittest.mock import AsyncMock
        path='/api/chat/transcribe'
        self.assertEqual(self.client.post(path,json={}).status_code,401)
        self.register('speaker')
        self.assertEqual(self.client.post(path,json={'audio':'bad!'}).status_code,422)
        self.assertEqual(self.client.post(path,json={},headers={'X-CSRF-Token':'wrong'}).status_code,403)
        payload={'audio':base64.b64encode(bytes(3200)).decode()}
        with patch('matchlab.companion_voice.transcribe', new=AsyncMock(return_value='Тест')):
            self.assertEqual(self.client.post(path,json=payload).json(),{'text':'Тест'})
        self.assertEqual(self.client.get('/api/chat').json()['turns'],[])
        self.assertEqual(self.client.post('/api/chat/speak',json={'id':'foreign'}).status_code,404)
        with self.client.app.state.repository.connection() as db:
            db.execute('DELETE FROM registration_agreements')
        self.assertEqual(self.client.post(path,json=payload).status_code,403)


class VoiceWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_whisper_and_kokoro_frames(self):
        import base64
        import io
        import json
        import os
        import wave
        from websockets.asyncio.server import serve
        from matchlab import companion_voice as voice
        pcm = bytes(range(128)) * 256
        async def whisper(ws):
            self.assertEqual(json.loads(await ws.recv()),{'type':'config','language':'ru','sample_rate':16000})
            await ws.send('{"type":"ready"}')
            chunks=[]
            while True:
                msg=json.loads(await ws.recv())
                if msg['type']=='eou': break
                self.assertEqual(msg['type'],'audio')
                chunks.append(base64.b64decode(msg['data']))
            self.assertEqual(b''.join(chunks),pcm)
            await ws.send(json.dumps({'type':'final','text':'  Привет  '}))
        async def kokoro(ws):
            await ws.send('{"type":"ready"}')
            self.assertEqual(json.loads(await ws.recv())['type'],'config')
            await ws.send('{"type":"ready"}')
            self.assertEqual(json.loads(await ws.recv()),{'type':'text','text':'Hello!'})
            await ws.send(json.dumps({'type':'audio','data':base64.b64encode(pcm).decode()}))
            await ws.send('{"type":"done"}')
        async with serve(whisper,'127.0.0.1',0) as stt, serve(kokoro,'127.0.0.1',0) as tts:
            with patch.dict(os.environ,{
                'MIRA_WHISPER_URL':f'ws://127.0.0.1:{stt.sockets[0].getsockname()[1]}/ws',
                'MIRA_KOKORO_URL':f'ws://127.0.0.1:{tts.sockets[0].getsockname()[1]}/ws',
            }):
                self.assertEqual(await voice.transcribe(pcm),'Привет')
                with wave.open(io.BytesIO(await voice.kokoro('Hello!'))) as audio:
                    self.assertEqual(audio.getframerate(),24000)
                    self.assertEqual(audio.readframes(audio.getnframes()),pcm)
