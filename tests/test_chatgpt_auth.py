from concurrent.futures import CancelledError, ThreadPoolExecutor
from http.client import HTTPConnection
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from chaoxing_linux import chatgpt_auth as oauth


CLIENT = 'oaiapp_test_chatgpt_client'


class ChatGPTAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError as exc:
            raise unittest.SkipTest('cryptography is required for ChatGPT sign-in') from exc
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(data_dir.cleanup)
        self.auth = oauth.ChatGPTAuth(data_dir.name)

    def signed_identity(self, default_nonce, **changes):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        claims = {'iss': oauth.ISSUER, 'aud': CLIENT, 'sub': 'chatgpt-account-1',
                  'exp': int(time.time()) + 300, 'nonce': default_nonce, 'email': 'learner@example.test',
                  'email_verified': True, **changes}
        parts = [oauth._b64_url(json.dumps(value).encode())
                 for value in ({'alg': 'RS256', 'kid': 'test-key'}, claims)]
        signature = self.key.sign('.'.join(parts).encode(), padding.PKCS1v15(), hashes.SHA256())
        return '.'.join([*parts, oauth._b64_url(signature)])

    def jwks(self):
        def b64_number(number):
            return oauth._b64_url(number.to_bytes((number.bit_length() + 7)//8, 'big'))
        key = self.key.public_key().public_numbers()
        return {'keys': [{'kty': 'RSA', 'alg': 'RS256', 'kid': 'test-key', 'use': 'sig',
                          'n': b64_number(key.n), 'e': b64_number(key.e)}]}

    @staticmethod
    def tokens(**changes):
        return {'access_token': 'access-token-1', 'refresh_token': 'refresh-token-1',
                'scope': oauth.SCOPES, 'token_type': 'Bearer', 'expires_in': 3600, **changes}

    def authorize(self, profile_id=None, *, claims=None, grant=True, deny=False, switch_client=False):
        observed = {}
        callback_results = []
        callback_threads = []

        def post_callback(params):
            uri = urlsplit(params['redirect_uri'])
            with_connection = HTTPConnection(uri.hostname, uri.port, timeout=1)
            try:
                for fake_state in ('incorrect-state', params['state']):
                    callback = {'state': fake_state}
                    if deny:
                        callback['error'] = 'access_denied'
                    else:
                        callback['code'] = 'opaque-test-code'
                        if params['client_id'] == oauth.REGISTRATION_CLIENT or switch_client:
                            callback['client_id'] = 'oaiapp_wrong_client' if switch_client else CLIENT
                    with_connection.request('GET', uri.path + '?' + urlencode(callback))
                    response = with_connection.getresponse()
                    callback_results.append(response.status)
                    response.read()
            finally:
                with_connection.close()

        def browser(url):
            parts = urlsplit(url)
            self.assertEqual(parts.hostname, 'auth.openai.com')
            self.assertEqual(parts.path, '/api/accounts/authorize')
            observed.update({k: v[0] for k, v in parse_qs(parts.query).items()})
            thread = threading.Thread(target=post_callback, args=(observed,), daemon=True)
            thread.start()
            self.addCleanup(thread.join, 2)
            callback_threads.append(thread)
            return True

        def request(url, form=None, **_kwargs):
            if url == oauth.JWKS:
                return self.jwks()
            self.assertEqual(url, oauth.TOKEN)
            self.assertFalse(deny or switch_client)
            self.assertEqual(form['client_id'], CLIENT)
            self.assertEqual(form['redirect_uri'], observed['redirect_uri'])
            self.assertEqual(form['code'], 'opaque-test-code')
            self.assertEqual(form['resource'], oauth.RESOURCE)
            challenge = oauth._b64_url(hashlib.sha256(form['code_verifier'].encode()).digest())
            self.assertEqual(challenge, observed['code_challenge'])
            return self.tokens(scope=oauth.SCOPES if grant else 'openid email',
                               id_token=self.signed_identity(observed['nonce'], **(claims or {})))

        with patch.object(oauth, '_http_json', side_effect=request), patch.object(oauth, 'LOGIN_TIMEOUT', 2):
            status = self.auth.sign_in(profile_id, open_browser=browser)
        callback_threads[0].join(2)
        self.assertEqual(callback_results, [400, 200])
        return status, observed

    def test_official_pkce_loopback_saves_verified_registration_and_reuses_client(self):
        status, first = self.authorize()
        self.assertTrue(status['connected'])
        self.assertNotIn('access-token-1', json.dumps(status))
        self.assertEqual(first['client_id'], oauth.REGISTRATION_CLIENT)
        self.assertEqual(first['agent_name_hint'], oauth.APP_NAME)
        self.assertEqual(first['code_challenge_method'], 'S256')
        self.assertEqual(self.auth.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(urlsplit(first['redirect_uri']).path.endswith('/auth/callback'))
        new_status, returning = self.authorize(status['active_id'])
        self.assertEqual(returning['client_id'], CLIENT)
        self.assertNotIn('agent_name_hint', returning)
        self.assertEqual(first['ext_agent_host_id'], returning['ext_agent_host_id'])
        self.assertNotEqual(first['state'], returning['state'])
        self.assertNotEqual(first['nonce'], returning['nonce'])
        self.assertNotEqual(first['code_challenge'], returning['code_challenge'])
        self.assertEqual(new_status, status)
        self.assertEqual(self.auth.access_token(status['active_id']), 'access-token-1')

    def test_bad_identity_mismatched_client_and_denied_grant_preserve_active_account(self):
        initial, _ = self.authorize()
        for changes in ({'claims': {'nonce': 'bad'}}, {'claims': {'aud': 'another-app'}},
                        {'claims': {'exp': 1}}, {'claims': {'sub': 'different-account'}},
                        {'deny': True}, {'switch_client': True}):
            with self.subTest(changes=changes), self.assertRaises(oauth.ChatGPTAuthError):
                self.authorize(initial['active_id'], **changes)
            self.assertEqual(self.auth.status(), initial)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(CancelledError):
            self.auth.sign_in(cancel=cancelled, open_browser=lambda _: self.fail('browser opened on cancel'))

    def test_identity_without_plan_permission_cannot_infer_and_reprompts_for_consent(self):
        identity, first = self.authorize(grant=False)
        self.assertTrue(identity['authenticated'])
        self.assertFalse(identity['connected'])
        self.assertNotIn('prompt', first)
        with self.assertRaises(oauth.ChatGPTAuthError), patch.object(oauth, '_http_json') as request:
            self.auth.access_token()
        request.assert_not_called()
        status, returning = self.authorize(identity['active_id'])
        self.assertTrue(status['connected'])
        self.assertEqual(returning['client_id'], CLIENT)
        self.assertEqual(returning['prompt'], 'consent')

    def test_renewal_is_serialized_and_only_latest_rotation_is_used_to_revoke(self):
        initial, _ = self.authorize()
        state = self.auth._read()
        profile_id = initial['active_id']
        state['profiles'][profile_id]['expires_at'] = time.time() - 10
        self.auth._write(state)
        forms = []

        def request(url, form=None, **kwargs):
            forms.append((url, form, kwargs))
            if url == oauth.TOKEN:
                self.assertEqual(form['refresh_token'], 'refresh-token-1')
                time.sleep(.05)
                return self.tokens(access_token='replaced-access', refresh_token='replaced-refresh')
            if url == oauth.DISCOVERY:
                return {'issuer': oauth.ISSUER, 'revocation_endpoint': oauth.ISSUER + oauth.REVOCATION_PATH}
            self.assertEqual(form['token'], 'replaced-refresh')
            self.assertTrue(kwargs['expect_empty'])

        with patch.object(oauth, '_http_json', side_effect=request):
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.auth.access_token)
                other = oauth.ChatGPTAuth(self.auth.directory)
                second = pool.submit(other.access_token, profile_id)
                self.assertEqual((first.result(1), second.result(1)), ('replaced-access', 'replaced-access'))
            self.assertEqual(len(forms), 1)
            with self.assertRaisesRegex(oauth.ChatGPTAuthError, '账号已切换'):
                self.auth.sign_out('stale-profile-id')
            self.assertEqual(len(forms), 1)
            signed_out = self.auth.sign_out(profile_id)
        self.assertFalse(signed_out['connected'])
        self.assertTrue(signed_out['revocation_confirmed'])
        self.assertEqual(self.auth._read()['profiles'][profile_id]['client_id'], CLIENT)
        self.assertNotIn('replaced-refresh', self.auth.path.read_text())

    def test_symlink_and_nonprivate_credentials_are_rejected(self):
        path = self.auth.path
        target = Path(path.parent) / 'other-file.json'
        target.write_text('{"profiles":{}}')
        path.symlink_to(target)
        with self.assertRaises(oauth.ChatGPTAuthError):
            self.auth.status()
        path.unlink()
        path.write_text('{"profiles":{}}')
        path.chmod(0o644)
        with self.assertRaises(oauth.ChatGPTAuthError):
            self.auth.status()
