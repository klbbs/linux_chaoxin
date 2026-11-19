"""Official Sign in with ChatGPT for this local desktop installation."""

from __future__ import annotations

import base64
from concurrent.futures import CancelledError
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import webbrowser


ISSUER = 'https://auth.openai.com'
AUTHORIZATION = ISSUER + '/api/accounts/authorize'
TOKEN = ISSUER + '/api/accounts/oauth/token'
DISCOVERY = ISSUER + '/.well-known/openid-configuration'
JWKS = ISSUER + '/.well-known/jwks.json'
REVOCATION_PATH = '/api/accounts/oauth/revoke'
RESOURCE = 'https://api.openai.com/v1'
REGISTRATION_CLIENT = 'dynamic_agent_client'
APP_NAME = '学习通 Linux'
PLAN_SCOPE = 'chatgpt.tokens.use.direct'
SCOPES = f'openid profile email offline_access resource.invoke {PLAN_SCOPE}'
LOGIN_TIMEOUT = 300
HTTP_TIMEOUT = 12
MAX_BODY = 1024 * 1024
CLIENT_ID = re.compile(r'[a-zA-Z0-9._-]{8,200}')
INVALID_REFRESH = frozenset({'invalid_grant', 'invalid_refresh_token', 'token_expired',
                             'refresh_token_expired', 'refresh_token_invalidated', 'refresh_token_reused'})


class ChatGPTAuthError(ValueError):
    """A user-facing OAuth failure containing no secret or remote text."""


class _OAuthError(ChatGPTAuthError):
    def __init__(self, status, code=''):
        self.status = status
        self.code = code
        message = ('ChatGPT 授权已失效，请重新连接。' if code in INVALID_REFRESH else
                   f'OpenAI 授权服务返回 HTTP {status}，请稍后重试。')
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_json(url, form=None, *, expect_empty=False):
    """Use fixed OpenAI HTTPS endpoints. Avoid logging HTTP bodies or callback URLs."""
    if url not in (TOKEN, DISCOVERY, JWKS, ISSUER + REVOCATION_PATH):
        raise ChatGPTAuthError('OpenAI 授权服务地址无法验证。')
    payload = urlencode(form).encode('ascii') if form is not None else None
    headers = {'Accept': 'application/json'}
    if payload is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    try:
        with build_opener(_NoRedirect()).open(Request(url, data=payload, headers=headers),
                                             timeout=HTTP_TIMEOUT) as response:
            if expect_empty:
                if response.status != 200:
                    raise _OAuthError(response.status)
                return None
            data = response.read(MAX_BODY + 1)
    except HTTPError as exc:
        try:
            code = ''
            if url == TOKEN and exc.code in (400, 401):
                try:
                    info = json.loads(exc.read(65536)).get('error')
                    if isinstance(info, str) and info in INVALID_REFRESH:
                        code = info
                except (ValueError, AttributeError, OSError, HTTPException):
                    pass
            raise _OAuthError(exc.code, code) from None
        finally:
            exc.close()
    except (URLError, OSError, HTTPException):
        raise ChatGPTAuthError('暂时无法连接 OpenAI 授权服务。') from None
    try:
        value = json.loads(data) if len(data) <= MAX_BODY else None
    except (ValueError, RecursionError):
        value = None
    if not isinstance(value, dict):
        raise ChatGPTAuthError('OpenAI 授权响应无法识别。')
    return value


def _b64_url(value):
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _decode_b64(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('invalid base64url')
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _oauth_string(value, limit=16384):
    return isinstance(value, str) and 0 < len(value) <= limit and all(32 < ord(char) < 127 for char in value)


def _verify_identity(token, client_id, nonce):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    try:
        header_raw, claims_raw, sig_raw = token.split('.')
        header = json.loads(_decode_b64(header_raw))
        if header['alg'] != 'RS256' or not _oauth_string(header['kid'], 200):
            raise ValueError('unsupported JWT')
        if header.get('crit'):
            raise ValueError('unsupported JWT headers')
        keys = _http_json(JWKS)['keys']
        candidates = [key for key in keys if key.get('kid') == header['kid'] and key.get('kty') == 'RSA'
                      and key.get('use', 'sig') == 'sig' and key.get('alg', 'RS256') == 'RS256']
        if len(candidates) != 1:
            raise ValueError('JWT key missing or ambiguous')
        key = candidates[0]
        modulus = int.from_bytes(_decode_b64(key['n']), 'big')
        exponent = int.from_bytes(_decode_b64(key['e']), 'big')
        if not 2048 <= modulus.bit_length() <= 8192 or exponent not in (3, 65537):
            raise ValueError('invalid RSA key')
        rsa.RSAPublicNumbers(exponent, modulus).public_key().verify(
            _decode_b64(sig_raw), f'{header_raw}.{claims_raw}'.encode('ascii'), padding.PKCS1v15(), hashes.SHA256())
        claims = json.loads(_decode_b64(claims_raw))
        audience = claims['aud'] if isinstance(claims['aud'], list) else [claims['aud']]
        if (claims['iss'] != ISSUER or client_id not in audience
                or ((len(audience) > 1 or 'azp' in claims) and claims.get('azp') != client_id)
                or type(claims['exp']) is not int or claims['exp'] <= time.time()
                or ('nbf' in claims and (type(claims['nbf']) is not int or claims['nbf'] > time.time() + 30))
                or not _oauth_string(claims['nonce'], 200) or not hmac.compare_digest(claims['nonce'], nonce)
                or not isinstance(claims['sub'], str) or not claims['sub'] or len(claims['sub']) > 300):
            raise ValueError('invalid JWT claims')
        email = claims.get('email') if claims.get('email_verified') is True else ''
        if not isinstance(email, str) or len(email) > 254 or '@' not in email or any(not c.isprintable() for c in email):
            email = ''
        return {'subject': claims['sub'], 'email': email}
    except ChatGPTAuthError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError, RecursionError, InvalidSignature):
        raise ChatGPTAuthError('无法验证 ChatGPT 账号身份，请重新授权。') from None


def _token_fields(response, previous_scopes=()):
    access, refresh = response.get('access_token'), response.get('refresh_token')
    scope = response.get('scope')
    scopes = sorted(set(scope.split())) if isinstance(scope, str) else list(previous_scopes)
    expires = response.get('expires_in')
    if (not _oauth_string(access) or not _oauth_string(refresh)
            or str(response.get('token_type', '')).lower() != 'bearer'
            or type(expires) is not int or not 1 <= expires <= 86400):
        raise ChatGPTAuthError('OpenAI 没有返回可用的 ChatGPT 授权信息。')
    return {'access_token': access, 'refresh_token': refresh, 'expires_at': time.time() + expires,
            'scopes': scopes}


def _connected(profile):
    return (isinstance(profile.get('scopes'), list) and PLAN_SCOPE in profile['scopes']
            and _oauth_string(profile.get('access_token')) and _oauth_string(profile.get('refresh_token')))


def _clear_tokens(profile):
    for key in ('access_token', 'refresh_token', 'id_token', 'expires_at', 'scopes'):
        profile.pop(key, None)


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise CancelledError()


class _CallbackServer(HTTPServer):
    allow_reuse_address = False
    timeout = .2

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(1)
        return request, address


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        server = self.server
        try:
            parts = urlsplit(self.path)
            params = parse_qs(parts.query, keep_blank_values=True, max_num_fields=20)
            valid_path = (parts.path == '/auth/callback' and not parts.netloc
                          and self.headers.get('Host') == f'127.0.0.1:{server.server_port}')
            state = params.get('state', [])
            valid_state = (len(state) == 1 and _oauth_string(state[0], 200)
                           and hmac.compare_digest(state[0], server.oauth_state))
            if not valid_path or not valid_state or any(len(values) != 1 for values in params.values()):
                raise ValueError('invalid callback')
        except ValueError:
            self.respond(400, '无法确认这次登录，请回到应用重新开始。')
            return
        server.oauth_callback = {key: values[0] for key, values in params.items()}
        self.respond(200, '已收到 ChatGPT 授权结果，可以关闭此页面并返回学习通 Linux。')

    def respond(self, status, message):
        body = f'<!doctype html><meta charset="utf-8"><title>学习通 Linux</title><p>{message}</p>'.encode('utf-8')
        try:
            self.send_response_only(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'")
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


class ChatGPTAuth:
    """Per-installation ChatGPT authorizations, separate from application settings."""

    def __init__(self, config_dir):
        self.directory = Path(config_dir)
        self.path = self.directory / 'chatgpt-auth.json'

    @staticmethod
    def _check_file(fd):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ChatGPTAuthError('ChatGPT 授权文件需要当前用户独有的访问权限。')

    @contextmanager
    def _lock(self, kind='auth', *, blocking=True):
        path = self.directory / f'.chatgpt-{kind}.lock'
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = self.directory.stat()
            if directory.st_uid != os.getuid() or directory.st_mode & 0o022:
                raise ChatGPTAuthError('ChatGPT 授权目录需要由当前用户独自写入。')
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError:
            raise ChatGPTAuthError('无法打开 ChatGPT 授权存储。') from None
        try:
            self._check_file(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise ChatGPTAuthError('已有一个 ChatGPT 授权窗口正在等待。') from None
            yield
        finally:
            os.close(fd)

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {'active_id': '', 'profiles': {}}
        except OSError:
            raise ChatGPTAuthError('无法读取 ChatGPT 授权存储。') from None
        try:
            with os.fdopen(fd, 'rb') as stream:
                self._check_file(stream.fileno())
                contents = stream.read(MAX_BODY + 1)
            state = json.loads(contents) if len(contents) <= MAX_BODY else None
            if (not isinstance(state, dict) or not isinstance(state.get('profiles'), dict)
                    or not isinstance(state.get('active_id', ''), str)
                    or not all(self._valid_profile(profile) for profile in state['profiles'].values())):
                raise ValueError('invalid credentials file')
            return state
        except (OSError, ValueError, RecursionError) as exc:
            if isinstance(exc, ChatGPTAuthError):
                raise
            raise ChatGPTAuthError('ChatGPT 授权存储内容无法识别。') from None

    @staticmethod
    def _valid_profile(profile):
        return (isinstance(profile, dict) and isinstance(profile.get('label'), str)
                and _oauth_string(profile.get('client_id'), 200)
                and CLIENT_ID.fullmatch(profile['client_id']) is not None
                and type(profile.get('expires_at', 0)) in (float, int))

    def _write(self, value):
        path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.directory,
                                             prefix='.chatgpt-', suffix='.tmp', delete=False) as stream:
                path = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            path.replace(self.path)
        except OSError:
            raise ChatGPTAuthError('无法保存 ChatGPT 授权。') from None
        finally:
            if path:
                path.unlink(missing_ok=True)

    @staticmethod
    def _status(state):
        active = state.get('active_id') or ''
        def fields(profile):
            return {'connected': _connected(profile), 'authenticated': bool(profile.get('id_token'))}
        return {'active_id': active, **fields(state['profiles'].get(active, {})),
                'profiles': [{'id': profile_id, 'label': profile['label'], **fields(profile)}
                             for profile_id, profile in state['profiles'].items()]}

    def status(self):
        # The store is atomically replaced, so reads never see a partial write.
        return self._status(self._read())

    def select(self, profile_id):
        with self._lock():
            state = self._read()
            if profile_id not in state['profiles'] or not state['profiles'][profile_id].get('subject'):
                raise ChatGPTAuthError('请先完成此 ChatGPT 账号的授权。')
            state['active_id'] = profile_id
            self._write(state)
            return self._status(state)

    def sign_in(self, profile_id=None, cancel=None, notify=None, open_browser=webbrowser.open):
        notify = notify or (lambda _message: None)
        with self._lock('login', blocking=False):
            _check_cancel(cancel)
            with self._lock():
                state = self._read()
                host_id = state.get('host_id')
                if not host_id:
                    host_id = state['host_id'] = 'urn:uuid:' + str(uuid.uuid4())
                    self._write(state)
                existing = state['profiles'].get(profile_id)
                if profile_id and existing is None:
                    raise ChatGPTAuthError('没有找到要重新授权的 ChatGPT 账号。')
            return self._sign_in(host_id, profile_id, existing, cancel, notify, open_browser)

    def _sign_in(self, host_id, profile_id, existing, cancel, notify, open_browser):
        state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
        client = existing['client_id'] if existing else REGISTRATION_CLIENT
        try:
            listener = _CallbackServer(('127.0.0.1', 0), _CallbackHandler)
        except OSError:
            raise ChatGPTAuthError('无法接收浏览器授权结果，请重试。') from None
        with listener:
            listener.oauth_state, listener.oauth_callback = state, None
            redirect_uri = f'http://127.0.0.1:{listener.server_port}/auth/callback'
            params = {'client_id': client, 'ext_agent_host_id': host_id, 'response_type': 'code',
                      'redirect_uri': redirect_uri, 'scope': SCOPES, 'resource': RESOURCE,
                      'state': state, 'nonce': nonce, 'code_challenge_method': 'S256',
                      'code_challenge': _b64_url(hashlib.sha256(verifier.encode('ascii')).digest())}
            if not existing:
                params['agent_name_hint'] = APP_NAME
            elif existing.get('consent_missing'):
                params['prompt'] = 'consent'
            # Let OpenAI show the account picker again, keeping the retained ID
            # token out of the URL handled by the system browser launcher.
            url = AUTHORIZATION + '?' + urlencode(params)
            notify('请在浏览器中完成 ChatGPT 授权。')
            try:
                opened = open_browser(url)
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                raise ChatGPTAuthError('无法打开系统浏览器。请检查默认浏览器后重试。')
            deadline = time.monotonic() + LOGIN_TIMEOUT
            while listener.oauth_callback is None and time.monotonic() < deadline:
                _check_cancel(cancel)
                listener.handle_request()
            _check_cancel(cancel)
            callback = listener.oauth_callback

        if callback is None:
            raise ChatGPTAuthError('等待 ChatGPT 授权超时，请重试。')
        if callback.get('error'):
            raise ChatGPTAuthError('ChatGPT 授权未完成。请在浏览器中同意应用使用方案后重试。')
        issued = callback.get('client_id', client)
        code = callback.get('code')
        if (not _oauth_string(code, 4096) or not CLIENT_ID.fullmatch(issued) or issued == REGISTRATION_CLIENT
                or existing and issued != client):
            raise ChatGPTAuthError('浏览器没有返回有效的 ChatGPT 授权结果。')
        notify('正在验证 ChatGPT 账号与方案权限。')
        try:
            result = _http_json(TOKEN, {'grant_type': 'authorization_code', 'client_id': issued, 'code': code,
                                        'redirect_uri': redirect_uri, 'code_verifier': verifier, 'resource': RESOURCE})
        except _OAuthError as exc:
            if exc.code == 'invalid_grant' and not existing:
                with self._lock():
                    state = self._read()
                    if not any(p.get('client_id') == issued for p in state['profiles'].values()):
                        state['profiles'][uuid.uuid4().hex] = {
                            'client_id': issued, 'label': f'待完成授权 · {len(state["profiles"]) + 1}'}
                        self._write(state)
            raise
        _check_cancel(cancel)
        id_token = result.get('id_token')
        if not _oauth_string(id_token):
            raise ChatGPTAuthError('OpenAI 没有返回可验证的 ChatGPT 账号身份。')
        identity = _verify_identity(id_token, issued, nonce)
        if existing and existing.get('subject') and existing['subject'] != identity['subject']:
            raise ChatGPTAuthError('返回的 ChatGPT 账号与选中的授权不一致。')
        granted = result.get('scope')
        scopes = granted.split() if isinstance(granted, str) else []
        consent_missing = PLAN_SCOPE not in scopes
        if consent_missing:
            tokens = {'scopes': scopes}
            if _oauth_string(result.get('refresh_token')):
                tokens['refresh_token'] = result['refresh_token']
        else:
            tokens = _token_fields(result)
        _check_cancel(cancel)
        with self._lock():
            state = self._read()
            profiles = state['profiles']
            matched = next(((key, profile) for key, profile in profiles.items() if profile['client_id'] == issued), None)
            if matched:
                profile_id, stored = matched
                if existing and stored.get('sign_in_revision') != existing.get('sign_in_revision'):
                    raise ChatGPTAuthError('此 ChatGPT 账号的连接已变更，请重新发起授权。')
                if stored.get('subject') and stored['subject'] != identity['subject']:
                    raise ChatGPTAuthError('返回的 ChatGPT 账号与已保存的授权不一致。')
            else:
                profile_id, stored = uuid.uuid4().hex, {}
            label = stored.get('label') if stored.get('subject') else None
            if not label:
                number = len(profiles) + (profile_id not in profiles)
                label = f'{identity["email"] or "ChatGPT 账号"} · {number}'
            profiles[profile_id] = {'client_id': issued, 'label': label, 'id_token': id_token,
                                    'sign_in_revision': uuid.uuid4().hex, 'consent_missing': consent_missing,
                                    **identity, **tokens}
            state['active_id'] = profile_id
            self._write(state)
            return self._status(state)

    def access_token(self, profile_id=None):
        with self._lock():
            state = self._read()
            profile = state['profiles'].get(profile_id or state.get('active_id'), {})
            if not _connected(profile):
                raise ChatGPTAuthError('请先在设置中连接 ChatGPT 账号并授权使用方案。')
            if profile.get('expires_at', 0) > time.time() + 60:
                return profile['access_token']
            try:
                result = _http_json(TOKEN, {'grant_type': 'refresh_token', 'client_id': profile['client_id'],
                                            'refresh_token': profile['refresh_token'], 'resource': RESOURCE})
            except _OAuthError as exc:
                if exc.code in INVALID_REFRESH:
                    _clear_tokens(profile)
                    self._write(state)
                raise
            replacement = _token_fields(result, profile['scopes'])
            profile.update(replacement)
            profile['consent_missing'] = PLAN_SCOPE not in replacement['scopes']
            if profile['consent_missing']:
                profile.pop('access_token', None)
            self._write(state)
            if not _connected(profile):
                raise ChatGPTAuthError('ChatGPT 账号不再有方案用量权限，请重新授权。')
            return profile['access_token']

    def sign_out(self, profile_id=None):
        with self._lock():
            state = self._read()
            active_id = state.get('active_id')
            if profile_id is not None and profile_id != active_id:
                raise ChatGPTAuthError('当前 ChatGPT 账号已切换，请刷新后再退出授权。')
            profile = state['profiles'].get(active_id)
            if not profile:
                return {**self._status(state), 'revocation_confirmed': True}
            refresh_token = profile.get('refresh_token')
            revoked = not refresh_token
            for delay in (0, .25, .5):
                if revoked:
                    break
                if delay:
                    time.sleep(delay)
                try:
                    discovery = _http_json(DISCOVERY)
                    endpoint = discovery.get('revocation_endpoint')
                    if discovery.get('issuer') != ISSUER or endpoint != ISSUER + REVOCATION_PATH:
                        break
                    _http_json(endpoint, {'client_id': profile['client_id'], 'token': refresh_token,
                                          'token_type_hint': 'refresh_token'}, expect_empty=True)
                    revoked = True
                    break
                except _OAuthError as exc:
                    if exc.status < 500:
                        break
                except ChatGPTAuthError:
                    pass
            _clear_tokens(profile)
            profile['sign_in_revision'] = uuid.uuid4().hex
            self._write(state)
            return {**self._status(state), 'revocation_confirmed': revoked}
