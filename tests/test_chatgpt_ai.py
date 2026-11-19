from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from chaoxing_linux.assistant_ai import AICancelled, AIClient, AIError
from chaoxing_linux.chatgpt_ai import ChatGPTPlanClient, configured_ai, list_chatgpt_models, read_completed_response
from chaoxing_linux.service import AppService


def sse(event):
    return b'data: ' + json.dumps(event, ensure_ascii=False).encode() + b'\r\n\r\n'


def completed(text, extra=()):
    output = list(extra) + [{'type': 'message', 'role': 'assistant',
                             'content': [{'type': 'output_text', 'text': text}]}]
    return BytesIO(sse({'type': 'response.completed', 'response': {'status': 'completed', 'output': output}}))


MODEL = {'visibility': 'list', 'slug': 'gpt-example', 'display_name': 'ChatGPT Example'}
PROFILE = {'connected': True, 'active_id': 'test-profile',
           'profiles': [{'id': 'test-profile', 'label': 'account@example.test', 'connected': True}]}


class ChatGPTPlanTests(unittest.TestCase):
    def auth(self):
        auth = Mock()
        auth.status.return_value = PROFILE
        auth.sign_in.return_value = PROFILE
        auth.access_token.return_value = 'scoped-mock-token'
        return auth

    def test_catalog_uses_selected_account_only_and_preserves_offered_order(self):
        auth = self.auth()
        rows = [{'visibility': 'hidden', 'slug': 'hidden'}, MODEL,
                {**MODEL, 'slug': 'gpt-example-two'}, MODEL]
        with patch('chaoxing_linux.chatgpt_ai._urlopen', return_value=BytesIO(json.dumps({'models': rows}).encode())) as call:
            listed = list_chatgpt_models(auth, profile_id='test-profile')
        self.assertEqual([model['slug'] for model in listed], ['gpt-example', 'gpt-example-two'])
        auth.access_token.assert_called_once_with(profile_id='test-profile')
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.openai.com/v1/models')
        self.assertEqual(request.get_header('Authorization'), 'Bearer scoped-mock-token')

    def test_review_uses_terminal_stream_and_pins_account_across_requests(self):
        auth = self.auth()
        client = ChatGPTPlanClient({'api_base': 'https://gateway.invalid', 'api_key': 'other-key'}, auth)
        listing = BytesIO(json.dumps({'models': [MODEL]}).encode())
        replies = [listing, completed('{"1":["概括"]}'), completed('{"1":["另一个概括"]}')]
        with patch('chaoxing_linux.chatgpt_ai._urlopen', side_effect=replies) as call:
            self.assertEqual(client.summarize_review([{'id': '1', 'text': '资料'}]), {'1': ['概括']})
            auth.status.return_value = {'active_id': 'other-account', 'connected': True}
            self.assertEqual(client.summarize_review([{'id': '1', 'text': '新资料'}]), {'1': ['另一个概括']})
        for entry in call.call_args_list[1:]:
            request = entry.args[0]
            self.assertEqual(request.full_url, 'https://api.openai.com/v1/responses')
            self.assertEqual(request.get_header('Authorization'), 'Bearer scoped-mock-token')
            body = json.loads(request.data)
            self.assertTrue(body['stream'])
            self.assertFalse(body['store'])
            self.assertEqual(body['input'][0]['role'], 'developer')
            self.assertNotIn('tools', body)
            self.assertEqual(body['model'], 'gpt-example')
        self.assertEqual([call.kwargs['profile_id'] for call in auth.access_token.call_args_list], ['test-profile'] * 3)

    def test_native_assignment_tool_uses_oauth_compatible_payload(self):
        auth = self.auth()
        client = ChatGPTPlanClient({'chatgpt_model': 'gpt-example'}, auth)
        listing = BytesIO(json.dumps({'models': [MODEL]}).encode())
        answer = '{"id":"q1","type":"single","answer":"A","confidence":0.8,"reason":"示例","sources":[]}'
        with patch('chaoxing_linux.chatgpt_ai._urlopen', side_effect=[listing, completed(answer)]) as call:
            result = client.solve_questions([{'id': 'q1', 'type': 'single', 'text': '请选择一项。',
                'options': [{'key': 'A', 'text': '对'}, {'key': 'B', 'text': '错'}]}], search=True)
        self.assertEqual(result[0]['status'], 'ok')
        body = json.loads(call.call_args.args[0].data)
        self.assertEqual(body['tools'], [{'type': 'web_search'}])
        self.assertNotIn('max_tool_calls', body)

    def test_incomplete_usage_limit_and_cancel_never_accept_partial_text(self):
        stream = sse({'type': 'response.output_text.delta', 'delta': 'partial'})
        with self.assertRaisesRegex(AIError, '提前结束'):
            read_completed_response(BytesIO(stream))
        limited = {'type': 'response.failed', 'response': {'error': {'code': 'subscription_sharing_usage_limit_exceeded'}}}
        with self.assertRaisesRegex(AIError, '用量达到限制'):
            read_completed_response(BytesIO(stream + sse(limited)))
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AICancelled):
            read_completed_response(completed('example'), cancel)

    def test_service_keeps_old_api_configuration_and_does_not_log_account_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AppService(Path(tmp) / 'config', Path(tmp) / 'data')
            service._chatgpt_auth = self.auth()
            self.assertIs(type(service._ai()), AIClient)
            service.save_settings({'api_key': 'old-api-key', 'ai_provider': 'chatgpt'})
            self.assertIsInstance(service._ai(), ChatGPTPlanClient)
            self.assertEqual(service.settings()['api_key'], 'old-api-key')
            started = service.start('chatgpt_sign_in', {'profile_id': ''})
            with service._lock:
                thread = service._threads.get(started['id'])
            if thread:
                thread.join(timeout=3)
            record = service.jobs()[-1]
            self.assertEqual(record['status'], 'success')
            self.assertEqual(record['result']['active_id'], 'test-profile')
            history = (service.data_dir / 'jobs.json').read_text()
            self.assertNotIn('account@example.test', history)
            self.assertNotIn('test-profile', history)
            service.shutdown()

    def test_disconnected_account_cannot_fall_back_to_saved_api_key(self):
        auth = self.auth()
        auth.status.return_value = {'connected': False, 'active_id': ''}
        client = configured_ai({'ai_provider': 'chatgpt', 'api_key': 'old-api-key'}, auth)
        with patch('chaoxing_linux.chatgpt_ai._urlopen') as connect:
            self.assertFalse(client.test_connection()['ok'])
        connect.assert_not_called()
