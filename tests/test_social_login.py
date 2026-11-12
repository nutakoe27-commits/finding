"""OAuth/PKCE at a fake provider boundary; never contact real login accounts."""
import base64
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs

import httpx
from fastapi.testclient import TestClient
from matchlab import accounts,social_login
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.storage import Repository


class SocialLoginTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.settings=Settings(Path(folder.name),public_origin='https://testserver',yandex_client_id='a'*32,
            yandex_client_secret='fixture-yandex-secret',vkid_client_id='51400001')
        self.repo=Repository(self.settings.database);self.repo.initialize();self.subject='12345';self.requests=[];self.bad_state=False;self.offline=False
        self.app=create_app(self.settings);self.logged_queries=[]
        @self.app.middleware('http')
        async def log_probe(request,call_next):
            response=await call_next(request)
            if '/callback' in request.scope['path']:self.logged_queries.append(request.scope['query_string'])
            return response
        self.client=TestClient(self.app,base_url='https://testserver');self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.addCleanup(patch.stopall);patch('matchlab.social_login.httpx.request',side_effect=self.provider).start()

    def provider(self,method,url,**kw):
        self.requests.append((method,url,kw))
        if self.offline:raise httpx.ReadTimeout('FAKE_PRIVATE_TOKEN_DO_NOT_LOG')
        if url.endswith('/token'):
            data=kw['data'];self.assertTrue(data['code_verifier']);self.assertEqual(kw['auth'],('a'*32,'fixture-yandex-secret'))
            value={'access_token':'PRIVATE_YANDEX_TOKEN','token_type':'bearer'}
        elif url=='https://login.yandex.ru/info':
            self.assertEqual(kw['headers'],{'Authorization':'OAuth PRIVATE_YANDEX_TOKEN'})
            value={'id':self.subject,'client_id':'a'*32,'display_name':'Михаил','default_email':'same@example.ru'}
        elif url.endswith('/oauth2/auth'):
            value={'state':'wrong-state' if self.bad_state else kw['data']['state'],'access_token':'PRIVATE_VK_TOKEN','user_id':self.subject}
        elif url.endswith('/oauth2/user_info'):
            self.assertEqual(kw['data']['access_token'],'PRIVATE_VK_TOKEN');value={'user':{'user_id':self.subject,'first_name':'Михаил'}}
        else:raise AssertionError('Unexpected external OAuth request')
        return httpx.Response(200,json=value,request=httpx.Request(method,url))

    def start(self,provider='yandex',**extra):
        response=self.client.post('/api/auth/social/'+provider+'/start',json={'return_to':'#account',**extra})
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn('fixture-yandex-secret',response.text);self.assertNotIn('verifier',response.text)
        params={k:v[0] for k,v in parse_qs(urlsplit(response.json()['authorization_url']).query).items()}
        with self.repo.connection() as db:
            row=db.execute('SELECT * FROM social_login_attempts WHERE state_hash=?',(social_login.digest(params['state']),)).fetchone()
            challenge=base64.urlsafe_b64encode(hashlib.sha256(row['verifier'].encode()).digest()).decode().rstrip('=')
            self.assertEqual(challenge,params['code_challenge']);self.assertEqual(params['code_challenge_method'],'S256')
        return params

    def callback(self,params,**extra):
        query={'state':params['state'],'code':'PRIVATE_AUTH_CODE','device_id':'device-binding',**extra}
        return self.client.get(urlsplit(params['redirect_uri']).path,params=query,follow_redirects=False)

    def complete(self):
        result=self.client.post('/api/auth/social/complete',json={'terms_accepted':True,'personal_data_consent':True,'age_confirmed':True,
            'legal_version':accounts.LEGAL_VERSION,'role':'admin','user_id':'injected'})
        self.assertEqual(result.status_code,200,result.text);self.client.headers['X-CSRF-Token']=result.json()['csrf'];return result.json()['user']

    def me(self):
        data=self.client.get('/api/auth/me').json()
        if data.get('csrf'):self.client.headers['X-CSRF-Token']=data['csrf']
        return data['user']

    def logout(self):self.me();self.assertEqual(self.client.post('/api/auth/logout',json={}).status_code,200)

    def native(self,name='native_user'):
        result=self.client.post('/api/auth/register',json={'username':name,'password':'native-fixture-password-123','terms_accepted':True,
            'personal_data_consent':True,'age_confirmed':True,'legal_version':accounts.LEGAL_VERSION})
        self.assertEqual(result.status_code,201,result.text);self.client.headers['X-CSRF-Token']=result.json()['csrf'];return result.json()['user']

    def test_new_social_account_requires_current_explicit_consent_and_never_accepts_role_or_subject_from_browser(self):
        response=self.callback(self.start())
        self.assertEqual(response.status_code,303);self.assertIn('social=complete',response.headers['location']);self.assertIsNone(self.me())
        pending=self.client.get('/api/auth/social/pending').json();self.assertEqual(pending['display_name'],'Михаил')
        self.assertNotIn('12345',json.dumps(pending));self.assertNotIn('TOKEN',json.dumps(pending))
        self.assertEqual(self.client.post('/api/auth/social/complete',json={}).status_code,422)
        user=self.complete();self.assertEqual(user['role'],'user');self.assertFalse(user['local_login_available']);self.assertEqual(user['display_name'],'Михаил')
        self.assertEqual(self.me()['id'],user['id'])
        with self.repo.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM social_login_attempts').fetchone()[0],0)
            raw=json.dumps([dict(r) for r in db.execute('SELECT * FROM users')]);self.assertNotIn('PRIVATE_',raw)

    def test_known_identity_logs_in_immediately_and_vk_ok_mail_share_vk_id_subject_without_duplicate_accounts(self):
        expected=None
        for provider in ('vk','ok','mail'):
            params=self.start(provider);self.assertEqual(params['provider'],social_login.VK_PROVIDERS[provider])
            response=self.callback(params)
            if expected is None:self.assertIn('social=complete',response.headers['location']);expected=self.complete()['id']
            else:self.assertIn('social=success',response.headers['location']);self.assertEqual(self.me()['id'],expected)
            self.assertEqual(self.me()['login_provider'],provider);self.logout()
        with self.repo.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],1)

    def test_state_is_browser_bound_single_use_and_never_leaks_codes_into_access_log_scope(self):
        params=self.start()
        with TestClient(self.app,base_url='https://testserver') as stranger:
            result=stranger.get('/api/auth/social/yandex/callback',params={'state':params['state'],'code':'PRIVATE_AUTH_CODE'},follow_redirects=False)
        self.assertIn('social=expired',result.headers['location']);self.assertEqual(self.requests,[])
        self.assertIn('social=expired',self.callback(params,state='X'*43).headers['location'])
        self.assertIn('social=complete',self.callback(params).headers['location'])
        calls=len(self.requests);self.assertIn('social=expired',self.callback(params).headers['location']);self.assertEqual(len(self.requests),calls)
        self.assertEqual(self.client.get('/api/auth/social/pending').status_code,200)
        self.assertTrue(self.logged_queries);self.assertTrue(all(q==b'' for q in self.logged_queries))

    def test_wrong_callback_issuer_and_expired_state_never_exchange_code(self):
        params=self.start()
        result=self.client.get('/api/auth/social/vkid/callback',params={'state':params['state'],'code':'secret'},follow_redirects=False)
        self.assertIn('social=expired',result.headers['location']);self.assertEqual(self.requests,[])
        params=self.start()
        with self.repo.connection() as db:db.execute('UPDATE social_login_attempts SET expires_at=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
        self.assertIn('social=expired',self.callback(params).headers['location']);self.assertEqual(self.requests,[])

    def test_provider_cancellation_and_outage_return_a_safe_local_retry_without_creating_user(self):
        params=self.start();result=self.callback(params,error='access_denied',error_description='PRIVATE_ERROR')
        self.assertIn('social=cancelled',result.headers['location']);self.assertEqual(self.requests,[])
        self.offline=True;result=self.callback(self.start())
        self.assertIn('social=unavailable',result.headers['location']);self.assertNotIn('PRIVATE_',result.text)
        with self.repo.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],0)

    def test_vk_token_state_must_match_and_return_target_cannot_be_an_external_redirect(self):
        self.bad_state=True;params=self.start('vk',return_to='https://evil.example/#account')
        self.assertIn('social=unavailable',self.callback(params).headers['location']);self.assertIsNone(self.me())
        self.assertEqual(len(self.requests),1)
        self.assertNotIn('evil.example',self.callback(params).headers['location'])

    def test_link_requires_password_and_preserves_account_id_bonus_and_existing_local_login(self):
        user=self.native()
        with self.repo.connection() as db:db.execute('UPDATE users SET bonus_analyses=7 WHERE id=?',(user['id'],))
        rejected=self.client.post('/api/auth/social/yandex/start',json={'action':'link','password':'wrong-password-123'})
        self.assertEqual(rejected.status_code,403)
        params=self.start(action='link',password='native-fixture-password-123')
        self.assertIn('social=success',self.callback(params).headers['location'])
        self.assertEqual(self.me()['id'],user['id']);self.assertEqual(self.me()['bonus_analyses'],7)
        self.assertTrue(self.client.get('/api/auth/social/connections').json()['connections'][0]['linked'])
        self.logout();self.assertIn('social=success',self.callback(self.start()).headers['location'])
        self.assertEqual(self.me()['id'],user['id']);self.logout()
        response=self.client.post('/api/auth/login',json={'username':'native_user','password':'native-fixture-password-123'})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['user']['login_provider'],'local')

    def test_identity_cannot_be_linked_to_another_account_and_email_never_merges_accounts(self):
        self.callback(self.start());first=self.complete();self.logout()
        second=self.native('second_native')
        result=self.callback(self.start(action='link',password='native-fixture-password-123'))
        self.assertIn('social=identity_in_use',result.headers['location']);self.assertEqual(self.me()['id'],second['id'])
        with self.repo.connection() as db:
            self.assertEqual(db.execute("SELECT user_id FROM user_identities WHERE provider='yandex'").fetchone()[0],first['id'])

    def test_native_login_or_logout_revokes_an_outstanding_oauth_attempt(self):
        params=self.start();native=self.native()
        self.assertIn('social=expired',self.callback(params).headers['location']);self.assertEqual(self.me()['id'],native['id']);self.assertEqual(self.requests,[])
        params=self.start(action='link',password='native-fixture-password-123');self.logout()
        self.assertIn('social=expired',self.callback(params).headers['location']);self.assertIsNone(self.me())

    def test_disabled_account_cannot_return_via_social_login(self):
        self.callback(self.start());user=self.complete();self.logout()
        with self.repo.connection() as db:db.execute('UPDATE users SET enabled=0 WHERE id=?',(user['id'],))
        self.assertIn('social=account_disabled',self.callback(self.start()).headers['location']);self.assertIsNone(self.me())

    def test_provider_configuration_and_admin_login_method_never_reveal_credentials(self):
        config=self.client.get('/api/auth/social/providers')
        self.assertTrue(all(p['enabled'] for p in config.json()['providers']));self.assertNotIn('fixture-yandex-secret',config.text)
        self.assertNotIn('fixture-yandex-secret',repr(self.settings))
        self.callback(self.start());user=self.complete()
        self.assertEqual(self.client.get('/api/admin/users').status_code,403)
        with self.repo.connection() as db:db.execute("UPDATE users SET role='admin' WHERE id=?",(user['id'],))
        result=self.client.get('/api/admin/users').json()['users'][0]
        self.assertEqual(result['last_login_provider'],'yandex');self.assertNotIn('password_hash',result)


if __name__=='__main__':unittest.main()
