"""Multimodal bytes on all model paths; no paid API or external network."""
import base64
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from chaoxing_linux.assistant_ai import AIClient, AIError
from chaoxing_linux.question_images import image_status, materialize_images
from test_question_images import descriptor, inline, png
from test_ai_tools import answer, chat, native, tool, SOURCE


class AIImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = materialize_images([descriptor()], data_dir=self.root, session=None)[0]
        self.question = {'id': 'q1', 'type': 'single', 'text': '',
            'options': [{'key': 'A', 'text': '示意图相符'}, {'key': 'B', 'text': '不相符'}],
            'images': [self.image], 'image_status': image_status([self.image]), 'cookie': 'private-cookie'}
        self.client = AIClient({'api_base': 'https://api.deepseek.com', 'model': 'deepseek-flash',
            'api_key': 'private-api-key', 'data_dir': str(self.root), 'max_retries': 0, 'search_mode': 'tools'})

    def assert_image_payload(self, payload, responses=False):
        messages = payload['input' if responses else 'messages']
        content = next(message['content'] for message in messages if message['role'] == 'user')
        part = next(part for part in content if part['type'] == ('input_image' if responses else 'image_url'))
        url = part['image_url'] if responses else part['image_url']['url']
        self.assertEqual(base64.b64decode(url.split(',', 1)[1], validate=True), png())
        text = ' '.join(part.get('text', '') for part in content)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn('private-cookie', json.dumps(payload))
        self.assertNotIn('private-api-key', json.dumps(payload))
        self.assertNotIn(self.image['source_url'], text)
        self.assertIn('题干', text)

    def test_no_search_still_sends_image_bytes_as_multimodal_content(self):
        with patch.object(self.client, '_request', return_value=chat(answer())) as request:
            result = self.client.solve_questions([self.question], search=False)[0]
        self.assert_image_payload(request.call_args.args[1])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['image_status'], {'state': 'ready', 'count': 1, 'ready': 1, 'sent': 1})
        self.assertNotIn('base64,', json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_tools_roundtrip_preserves_images_and_reasoning_without_search_upload(self):
        replies = [chat(calls=[tool(query='图形基本概念')]), chat(answer(sources=[SOURCE['url']]))]
        replies[0]['choices'][0]['message']['reasoning_content'] = 'opaque-provider-continuation'
        sent = []
        def request(url, payload, **kwargs):
            sent.append(deepcopy(payload))
            return replies[len(sent) - 1]
        with patch.object(self.client, '_request', side_effect=request), patch.object(self.client, '_search', return_value=[SOURCE]) as search:
            result = self.client.solve_questions([self.question])[0]
        self.assertEqual(len(sent), 2)
        for payload in sent:
            self.assert_image_payload(payload)
        self.assertEqual(sent[1]['messages'][2]['reasoning_content'], 'opaque-provider-continuation')
        self.assertEqual(search.call_args.args[0], {'text': '图形基本概念', 'options': []})
        self.assertEqual(result['image_status']['sent'], 1)
        self.assertEqual(result['sources'], [SOURCE])

    def test_prefetch_receives_only_text_while_model_receives_images(self):
        self.client.settings['search_mode'] = 'prefetch'
        with patch.object(self.client, '_search', return_value=[]) as search, patch.object(self.client, '_request', return_value=chat(answer())) as request:
            result = self.client.solve_questions([self.question])[0]
        query = search.call_args.args[0]
        self.assertNotIn('images', query)
        self.assertNotIn('base64', json.dumps(query))
        self.assertNotIn(str(self.root), json.dumps(query))
        self.assert_image_payload(request.call_args.args[1])
        self.assertEqual(result['status'], 'ok')

    def test_responses_uses_input_image_with_and_without_native_search(self):
        self.client.settings.update(api_base='https://provider.example/v1', search_mode='native')
        for enabled in (False, True):
            with self.subTest(search=enabled), patch.object(self.client, '_request', return_value=native()) as request:
                result = self.client.solve_questions([self.question], search=enabled)[0]
            self.assert_image_payload(request.call_args.args[1], responses=True)
            self.assertEqual(result['image_status']['sent'], 1)

    def test_pure_image_option_is_associated_with_its_key(self):
        self.question['images'][0] = dict(self.image, scope='option', option_key='A')
        self.question['text'] = '选出对应图片'
        self.question['options'][0]['text'] = ''
        with patch.object(self.client, '_request', return_value=chat(answer())) as request:
            result = self.client.solve_questions([self.question], search=False)[0]
        content = request.call_args.args[1]['messages'][1]['content']
        self.assertTrue(any('选项 A' in item.get('text', '') for item in content))
        self.assertEqual(result['status'], 'ok')

    def test_missing_changed_or_failed_images_never_call_model(self):
        path = Path(self.image['local_path'])
        for state in ('failed', 'partial', 'error'):
            question = dict(self.question, image_status={'state': state, 'count': 1, 'ready': 0})
            with patch.object(self.client, '_request') as request:
                result = self.client.solve_questions([question])[0]
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(result['image_status']['sent'], 0)
            request.assert_not_called()
        path.write_bytes(png(222))
        with patch.object(self.client, '_request') as request:
            result = self.client.solve_questions([self.question])[0]
        request.assert_not_called()
        self.assertEqual(result['image_status']['state'], 'failed')
        path.unlink()
        with patch.object(self.client, '_request') as request:
            self.assertEqual(self.client.solve_questions([self.question])[0]['status'], 'unknown')
        request.assert_not_called()

    def test_declared_missing_attachments_and_raw_img_do_not_downgrade(self):
        for question in (dict(self.question, text='文字残片', images=[]),
                         dict(self.question, images=[], image_status={}, text='<img src="https://a/image.png">')):
            with patch.object(self.client, '_request') as request:
                result = self.client.solve_questions([question])[0]
            self.assertEqual(result['status'], 'unknown')
            request.assert_not_called()

    def test_known_incompatible_model_and_unconfigured_cache_are_unknown(self):
        for changes in ({'model': 'deepseek-v4-pro'}, {'data_dir': ''}):
            client = AIClient(dict(self.client.settings, **changes))
            with patch.object(client, '_request') as request:
                result = client.solve_questions([self.question], search=False)[0]
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(result['image_status']['sent'], 0)
            request.assert_not_called()

    def test_api_error_never_retries_without_image_parts(self):
        sent = []
        def request(url, payload, **kwargs):
            sent.append(deepcopy(payload))
            raise AIError('服务返回 HTTP 400 unsupported image')
        with patch.object(self.client, '_request', side_effect=request):
            result = self.client.solve_questions([self.question], search=False)[0]
        self.assertEqual(len(sent), 2)  # JSON-format compatibility retry retains all images.
        for payload in sent:
            self.assert_image_payload(payload)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['image_status']['sent'], 0)

    def test_accepted_request_with_bad_answer_records_sent_but_stays_unknown(self):
        with patch.object(self.client, '_request', return_value=chat('not-json')):
            result = self.client.solve_questions([self.question], search=False)[0]
        self.assertEqual(result['image_status']['sent'], 1)
        self.assertEqual(result['status'], 'unknown')

    def test_tool_query_cannot_upload_image_bytes_or_cache_path(self):
        for query in (inline(), self.image['local_path']):
            with patch.object(self.client, '_request', side_effect=[chat(calls=[tool(query=query)]), chat(answer())]), patch.object(self.client, '_search') as search:
                self.client.solve_questions([self.question])
            search.assert_not_called()

    def test_real_local_http_serialization_contains_image_bytes(self):
        payloads = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payloads.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps(chat(answer())).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = AIClient(dict(self.client.settings, api_base='http://127.0.0.1:' + str(server.server_port)))
            result = client.solve_questions([self.question], search=False)[0]
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(result['status'], 'ok')
        self.assert_image_payload(payloads[0])


if __name__ == '__main__':
    unittest.main()
