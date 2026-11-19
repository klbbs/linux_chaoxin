"""Model catalog and streaming inference for an authorized ChatGPT plan."""
from __future__ import annotations

from http.client import HTTPException
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .assistant_ai import AIClient, AIError, _check_cancel, _urlopen


BASE = 'https://api.openai.com/v1'
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_STREAM_BYTES = 12 * 1024 * 1024


def _http_error(code):
    messages = {
        401: 'ChatGPT 登录已失效，请重新授权。',
        403: 'ChatGPT 未授权此请求，请检查账号套餐和应用权限。',
        429: 'ChatGPT 套餐用量达到限制，请在 ChatGPT 设置中查看用量后再试。',
    }
    return AIError(messages.get(code, f'ChatGPT 官方服务返回 HTTP {code}。'))


def list_chatgpt_models(auth, *, profile_id=None, timeout=20):
    token = auth.access_token(profile_id=profile_id)
    request = Request(BASE + '/models', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    try:
        with _urlopen(request, timeout=timeout) as reply:
            raw = reply.read(MAX_EVENT_BYTES + 1)
        if len(raw) > MAX_EVENT_BYTES:
            raise AIError('ChatGPT 模型列表过大。')
        data = json.loads(raw)
    except HTTPError as exc:
        exc.close()
        raise _http_error(exc.code) from None
    except (OSError, URLError, HTTPException, ValueError, RecursionError):
        raise AIError('无法读取 ChatGPT 模型列表，请检查网络后重试。') from None
    if not isinstance(data, dict) or not isinstance(data.get('models'), list):
        raise AIError('ChatGPT 返回了无法识别的模型列表。')
    models = []
    seen = set()
    for item in data['models']:
        if not isinstance(item, dict) or item.get('visibility') != 'list':
            continue
        slug, label = item.get('slug'), item.get('display_name')
        if not isinstance(slug, str) or not slug.strip() or len(slug) > 128 or slug in seen:
            continue
        seen.add(slug)
        models.append({'slug': slug, 'display_name': label.strip()[:120] if isinstance(label, str) and label.strip() else slug})
    if not models:
        raise AIError('此 ChatGPT 账号暂未返回可用模型，请核对套餐和授权。')
    return models


def _response_error(event):
    response = event.get('response')
    error = response.get('error') if isinstance(response, dict) else None
    if not isinstance(error, dict):
        error = event.get('error')
    code = error.get('code') if isinstance(error, dict) else event.get('code')
    if code == 'subscription_sharing_usage_limit_exceeded':
        return AIError('ChatGPT 套餐用量达到限制，请在 ChatGPT 设置中查看用量。')
    if code == 'subscription_sharing_usage_unavailable':
        return AIError('该 ChatGPT 账号当前无法使用套餐额度，请核对套餐和授权。')
    return AIError('ChatGPT 未能完成此次生成，请稍后重试或切换账号可用的模型。')


def read_completed_response(reply, cancel=None, *, timeout=180):
    """Read the terminal Responses SSE event, never use an unfinished answer."""
    parts = []
    event_size = total = 0
    deadline = time.monotonic() + timeout
    while True:
        _check_cancel(cancel)
        if time.monotonic() >= deadline:
            raise AIError('ChatGPT 输出等待超时。')
        line = reply.readline(MAX_EVENT_BYTES + 1)
        if not line:
            raise AIError('ChatGPT 连接提前结束，未获得完整回答。')
        total += len(line)
        if len(line) > MAX_EVENT_BYTES or total > MAX_STREAM_BYTES:
            raise AIError('ChatGPT 响应超过本机读取上限。')
        line = line.rstrip(b'\r\n')
        if line.startswith(b'data:'):
            data = line[5:].lstrip(b' ')
            event_size += len(data) + 1
            if event_size > MAX_EVENT_BYTES:
                raise AIError('ChatGPT 响应事件过大。')
            parts.append(data)
        elif not line and parts:
            try:
                event = json.loads(b'\n'.join(parts))
            except (ValueError, UnicodeError, RecursionError):
                raise AIError('ChatGPT 返回了无法读取的输出事件。') from None
            parts.clear()
            event_size = 0
            if not isinstance(event, dict):
                continue
            kind = event.get('type')
            if kind == 'response.completed':
                response = event.get('response')
                if not isinstance(response, dict) or response.get('status') != 'completed':
                    raise AIError('ChatGPT 未返回已完成的回答。')
                return response
            if kind in ('response.failed', 'response.incomplete', 'error'):
                raise _response_error(event)


class ChatGPTPlanClient(AIClient):
    def __init__(self, settings, auth):
        super().__init__({**settings, 'api_base': BASE, 'api_key': '', 'search_key': '',
                          'model': settings.get('chatgpt_model') or '', 'search_mode': 'native'})
        self.auth = auth
        self.profile_id = None
        self._model_selected = False

    def check_review_settings(self):
        self._select_model()

    def _select_model(self):
        if self._model_selected:
            return
        profile = self.auth.status()
        if not profile.get('connected') or not profile.get('active_id'):
            raise AIError('请在设置中选择账号，并使用 ChatGPT 登录和授权套餐用量。')
        self.profile_id = profile['active_id']
        models = list_chatgpt_models(self.auth, profile_id=self.profile_id, timeout=self.timeout)
        if self.model and not any(item['slug'] == self.model for item in models):
            raise AIError('当前 ChatGPT 账号无法使用所选模型，请在设置中选择可用模型。')
        if not self.model:
            self.model = models[0]['slug']
        self._model_selected = True

    def _responses(self, messages, cancel=None, **options):
        _check_cancel(cancel)
        self._select_model()
        return super()._responses(messages, cancel, **options)

    def _send_responses(self, endpoint, payload, cancel):
        if endpoint != BASE + '/responses':
            raise AIError('ChatGPT 账号授权只可用于 OpenAI 官方服务。')
        token = self.auth.access_token(profile_id=self.profile_id)
        body = {**payload, 'stream': True, 'store': False, 'input': [
            {**message, 'role': 'developer'} if message.get('role') == 'system' else message
            for message in payload['input']
        ]}
        body.pop('max_tool_calls', None)
        request = Request(endpoint, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'Accept': 'text/event-stream',
        })
        _check_cancel(cancel)
        try:
            with _urlopen(request, timeout=self.timeout) as reply:
                return read_completed_response(reply, cancel)
        except HTTPError as exc:
            exc.close()
            raise _http_error(exc.code) from None
        except (OSError, URLError, HTTPException):
            raise AIError('ChatGPT 网络连接失败或等待超时，请重试。') from None


def configured_ai(settings, auth=None):
    if settings.get('ai_provider') != 'chatgpt':
        return AIClient(settings)
    if auth is None:
        from .chatgpt_auth import ChatGPTAuth
        config_dir = settings.get('config_dir')
        if not config_dir:
            raise AIError('缺少 ChatGPT 授权目录，请从桌面工作台打开。')
        auth = ChatGPTAuth(config_dir)
    return ChatGPTPlanClient(settings, auth)
