import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from chaoxing_linux.assistant_ai import AIClient, AICancelled, AIError, _NoRedirect, chat_endpoint, normalize_question


QUESTION = {'id': 'q1', 'type': 'single', 'text': 'Which number is even?',
            'options': [{'key': 'A', 'text': '1'}, {'key': 'B', 'text': '2'}]}
SOURCE = {'title': 'Actual source', 'url': 'https://example.org/even', 'snippet': 'Two is even.'}


def model_answer(**changes):
    data = {'id': 'q1', 'type': 'single', 'answer': 'B', 'confidence': 0.9,
            'reason': 'Two is divisible by two.', 'sources': []}
    data.update(changes)
    return json.dumps(data)


class Response(io.BytesIO):
    pass


class AIClientTests(unittest.TestCase):
    def setUp(self):
        self.client = AIClient({'api_base': 'https://model.example/v1', 'api_key': 'super-secret-token',
                                'model': 'test-model', 'max_retries': 0, 'search_enabled': False, 'search_mode': 'prefetch'})

    def solve(self, reply=None, question=None):
        with patch.object(self.client, '_chat', return_value=reply or model_answer()):
            return self.client.solve_questions([question or QUESTION])[0]

    def test_endpoint_accepts_root_v1_full_and_local(self):
        self.assertEqual(chat_endpoint('https://api.deepseek.com'), 'https://api.deepseek.com/chat/completions')
        self.assertEqual(chat_endpoint('https://api.deepseek.com/v1'), 'https://api.deepseek.com/v1/chat/completions')
        for base in ('https://example.org', 'https://example.org/v1/', 'https://example.org/v1/chat/completions'):
            self.assertEqual(chat_endpoint(base), 'https://example.org/v1/chat/completions')
        self.assertEqual(chat_endpoint('http://127.0.0.1:9000'), 'http://127.0.0.1:9000/v1/chat/completions')
        for base in ('http://example.org', 'https://key@example.org', 'https://example.org?key=secret', ''):
            with self.subTest(base=base), self.assertRaises(ValueError):
                chat_endpoint(base)

    def test_input_metadata_is_never_forwarded(self):
        question = dict(QUESTION, cookie='account-cookie', token='private-token', phone='private-phone')
        with patch.object(self.client, '_chat', return_value=model_answer()) as chat:
            result = self.client.solve_questions([question])[0]
        sent = json.dumps(chat.call_args.args[0])
        self.assertNotIn('account-cookie', sent)
        self.assertNotIn('private-token', sent)
        self.assertNotIn('private-phone', sent)
        self.assertNotIn('super-secret-token', sent)
        self.assertEqual(result['status'], 'ok')

    def test_backend_type_aliases_are_normalized(self):
        for original, expected in [('completion', 'fill'), ('judgement', 'judge'), ('shortanswer', 'text')]:
            self.assertEqual(normalize_question(dict(QUESTION, type=original))['type'], expected)

    def test_normalizes_only_supported_fields_and_aliases(self):
        original = dict(QUESTION, type='单选题', secret='no')
        clean = normalize_question(original)
        self.assertEqual(clean['type'], 'single')
        self.assertNotIn('secret', clean)

    def test_invalid_input_is_unknown_before_network(self):
        for question in (dict(QUESTION, type='random'), dict(QUESTION, text=''),
                         dict(QUESTION, options=[]), dict(QUESTION, options=[{'key': 'A', 'text': 'x'}] * 2)):
            with self.subTest(question=question), patch.object(self.client, '_chat') as chat:
                answer = self.client.solve_questions([question])[0]
                self.assertEqual(answer['status'], 'unknown')
                chat.assert_not_called()
        with self.assertRaises(ValueError):
            self.client.solve_questions([QUESTION, QUESTION])

    def test_rejects_wrong_ids_types_invalid_options_empty_and_bad_confidence(self):
        bad = [{'id': 'other'}, {'id': 1}, {'type': 'multiple'}, {'answer': 'C'}, {'answer': None},
               {'answer': ''}, {'confidence': 99}, {'confidence': float('nan')}, {'confidence': True}]
        for changes in bad:
            with self.subTest(changes=changes):
                answer = self.solve(model_answer(**changes))
                self.assertEqual(answer['status'], 'unknown')
                self.assertIsNone(answer['answer'])

    def test_single_json_fence_list_wrapper_and_trailing_text(self):
        for reply in ('```json\n' + model_answer() + '\n```', '[' + model_answer() + ']',
                      '{"answers":[' + model_answer() + ']}'):
            self.assertEqual(self.solve(reply)['answer'], 'B')
        self.assertEqual(self.solve(model_answer() + ' dangerous trailing text')['status'], 'unknown')
        self.assertEqual(self.solve('not json')['status'], 'unknown')

    def test_multiple_judge_fill_text_validation(self):
        for kind, answer, expected in [('multiple', ['B', 'A'], ['A', 'B']), ('judge', '正确', True),
                                       ('judge', False, False), ('fill', ['one', 'two'], ['one', 'two']),
                                       ('text', 'An explanation', 'An explanation')]:
            question = dict(QUESTION, type=kind)
            result = self.solve(model_answer(type=kind, answer=answer), question)
            self.assertEqual(result['answer'], expected)
        for kind, answer in [('multiple', ['A', 'A']), ('multiple', []), ('judge', 1), ('judge', 'A'),
                             ('fill', ['']), ('text', [])]:
            self.assertEqual(self.solve(model_answer(type=kind, answer=answer), dict(QUESTION, type=kind))['status'], 'unknown')

    def test_actual_search_citations_are_bound_to_returned_sources(self):
        self.client.settings['search_enabled'] = True
        with patch.object(self.client, '_search', return_value=[SOURCE]), \
             patch.object(self.client, '_chat', return_value=model_answer(sources=[{'url': SOURCE['url'], 'title': 'Fabricated title'}])):
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(result['sources'], [SOURCE])
        self.assertEqual(result['search_status']['state'], 'ok')
        with patch.object(self.client, '_search', return_value=[SOURCE]), \
             patch.object(self.client, '_chat', return_value=model_answer(sources=['https://fake.example'])):
            self.assertEqual(self.client.solve_questions([QUESTION])[0]['status'], 'unknown')
        self.assertEqual(self.solve(model_answer(reason='See https://fake.example'))['status'], 'unknown')

    def test_search_failure_and_empty_result_are_explicit(self):
        self.client.settings['search_enabled'] = True
        for outcome, state in [(AIError('Tavily key unavailable'), 'failed'), ([], 'empty')]:
            with patch.object(self.client, '_search', side_effect=outcome if isinstance(outcome, Exception) else None,
                              return_value=outcome), patch.object(self.client, '_chat', return_value=model_answer()) as chat:
                result = self.client.solve_questions([QUESTION])[0]
            self.assertEqual(result['search_status']['state'], state)
            self.assertEqual(result['sources'], [])
            sent = json.loads(chat.call_args.args[0][1]['content'])
            self.assertEqual(sent['search_status']['state'], state)

    def test_duckduckgo_extracts_redirect_and_text(self):
        self.client.settings['search_provider'] = 'duckduckgo'
        markup = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Feven">Real &amp; title</a><div class="result__snippet">Even number.</div>'
        with patch.object(self.client, '_request', return_value=markup):
            sources = self.client._search(QUESTION, None)
        self.assertEqual(sources[0]['url'], SOURCE['url'])
        self.assertEqual(sources[0]['title'], 'Real & title')
        self.assertEqual(sources[0]['snippet'], 'Even number.')

    def test_duckduckgo_challenge_and_unrecognized_page_are_failed(self):
        self.client.settings.update(search_enabled=True, search_provider='duckduckgo')
        for page in ('<form id="challenge-form" action="/anomaly.js"></form>',
                     '<div class="anomaly-modal__mask">Verify</div>', '<html>Unexpected page</html>'):
            with patch.object(self.client, '_request', return_value=page), \
                 patch.object(self.client, '_chat', return_value=model_answer()):
                result = self.client.solve_questions([QUESTION])[0]
            self.assertEqual(result['search_status']['state'], 'failed')
            self.assertEqual(result['sources'], [])
        with patch.object(self.client, '_request', return_value='<div class="no-results">No results found</div>'):
            self.assertEqual(self.client._search(QUESTION, None), [])

    def test_bing_rss_default_extracts_real_urls_and_strips_snippet_markup(self):
        page = ('<rss version="2.0"><channel><item><title>Actual &amp; title</title>'
                '<link>https://example.org/even</link><description>&lt;b&gt;Two&lt;/b&gt; is even.</description></item>'
                '<item><title>Duplicate</title><link>https://example.org/even</link></item>'
                '<item><title>Unsafe</title><link>javascript:alert(1)</link></item>'
                '<item><title>Credentials</title><link>https://secret@example.org/</link></item>'
                '<item><title>Other</title><link>http://example.org/other</link></item>'
                '</channel></rss>')
        with patch.object(self.client, '_request', return_value=page) as request:
            result = self.client._search(QUESTION, None)
        self.assertTrue(request.call_args.args[0].startswith('https://cn.bing.com/search?'))
        self.assertIn('format=rss', request.call_args.args[0])
        self.assertEqual([s['url'] for s in result], [SOURCE['url'], 'http://example.org/other'])
        self.assertEqual(result[0]['title'], 'Actual & title')
        self.assertEqual(result[0]['snippet'], 'Two is even.')
        with patch.object(self.client, '_request', return_value=page), \
             patch.object(self.client, '_chat', return_value=model_answer(sources=[SOURCE['url']])):
            self.client.settings['search_enabled'] = True
            answer = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(answer['sources'], [result[0]])
        self.assertEqual(answer['search_status']['state'], 'ok')
        self.assertEqual(answer['search_status']['mode'], 'prefetch')
        self.assertEqual(answer['search_status']['count'], 2)
        self.assertEqual(answer['search_status']['calls'][0]['tool'], 'prefetch_search')

    def test_bing_rss_rejects_dtd_entities_html_and_unrecognized_xml(self):
        for page in ('<!DOCTYPE rss [<!ENTITY secret "expanded">]><rss><channel/></rss>',
                     '<!ENTITY secret SYSTEM "file:///etc/passwd"><rss><channel/></rss>',
                     '<html><body>Captcha challenge</body></html>', '<notrss/>', '<rss>broken', '<rss/>'):
            with self.subTest(page=page), patch.object(self.client, '_request', return_value=page), self.assertRaises(AIError):
                self.client._search(QUESTION, None)
        with patch.object(self.client, '_request', return_value='<rss><channel/></rss>'):
            self.assertEqual(self.client._search(QUESTION, None), [])

    def test_tavily_requires_key_and_sends_only_query(self):
        self.client.settings.update(search_provider='tavily', search_key='search-secret')
        with patch.object(self.client, '_request', return_value={'results': [{'title': 'Source', 'url': SOURCE['url'], 'content': 'Snippet'}]}) as request:
            result = self.client._search(QUESTION, None)
        self.assertEqual(len(result), 1)
        payload = request.call_args.args[1]
        self.assertEqual(payload['api_key'], 'search-secret')
        self.assertNotIn('super-secret-token', json.dumps(payload))
        self.client.settings['search_key'] = ''
        with self.assertRaises(AIError):
            self.client._search(QUESTION, None)

    def test_http_payload_authorization_and_connection(self):
        output = {'choices': [{'message': {'content': '{"ok":true}'}}]}
        with patch('chaoxing_linux.assistant_ai._urlopen', return_value=Response(json.dumps(output).encode())) as opener:
            self.assertTrue(self.client.test_connection()['ok'])
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer super-secret-token')
        self.assertEqual(json.loads(request.data)['response_format'], {'type': 'json_object'})
        self.assertEqual(opener.call_args.kwargs['timeout'], 30)

    def test_unsupported_response_format_falls_back_without_it(self):
        failure = HTTPError('https://model.example/v1/chat/completions', 400, 'Bad Request', {}, io.BytesIO())
        output = {'choices': [{'message': {'content': '{"ok":true}'}}]}
        with patch('chaoxing_linux.assistant_ai._urlopen', side_effect=[failure, Response(json.dumps(output).encode())]) as opener:
            self.assertTrue(self.client.test_connection()['ok'])
        self.assertNotIn('response_format', json.loads(opener.call_args.args[0].data))
        self.assertEqual(opener.call_count, 2)

    def test_retry_rate_limit_and_cancel_during_backoff(self):
        self.client.retries = 1
        failure = HTTPError('https://model.example/v1/chat/completions', 429, 'Limited', {'Retry-After': '0.1'}, io.BytesIO())
        with patch('chaoxing_linux.assistant_ai._urlopen', side_effect=[failure, Response(b'{"yes":true}')]) as opener:
            self.assertEqual(self.client._request('https://model.example'), {'yes': True})
            self.assertEqual(opener.call_count, 2)
        event = threading.Event()
        def fail_and_cancel(*args, **kwargs):
            event.set()
            raise URLError('temporary')
        with patch('chaoxing_linux.assistant_ai._urlopen', side_effect=fail_and_cancel), self.assertRaises(AICancelled):
            self.client._request('https://model.example', cancel=event)

    def test_cancel_prevents_any_request_and_progress_is_per_question(self):
        event = threading.Event()
        event.set()
        with patch.object(self.client, '_chat') as chat, self.assertRaises(AICancelled):
            self.client.solve_questions([QUESTION], cancel=event)
        chat.assert_not_called()
        updates = []
        with patch.object(self.client, '_chat', return_value=model_answer()):
            self.client.solve_questions([QUESTION], progress=lambda *args: updates.append(args))
        self.assertEqual(updates[0][:2], (1, 1))
        self.assertEqual(updates[0][2]['answer'], 'B')

    def test_redirect_handler_refuses_https_downgrade_and_cross_host(self):
        from urllib.request import Request
        request = Request('https://provider.example/v1/chat/completions', data=b'{}',
                          headers={'Authorization': 'Bearer fake-key'})
        for target in ('http://provider.example/steal', 'https://other.example/steal',
                       'https://provider.example/changed'):
            self.assertIsNone(_NoRedirect().redirect_request(request, None, 302, 'Moved', {}, target))

    def test_real_local_redirect_never_forwards_model_or_search_credentials(self):
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                observed.append((self.path, self.headers.get('Authorization'),
                                 self.rfile.read(int(self.headers.get('Content-Length', '0')))))
                self.send_response(302)
                self.send_header('Location', f'http://localhost:{self.server.server_port}/collect')
                self.end_headers()
            def do_GET(self):
                observed.append((self.path, self.headers.get('Authorization'), b''))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f'http://127.0.0.1:{server.server_port}/redirect'
            for payload, headers in [({'messages': []}, {'Authorization': 'Bearer fake-model-key'}),
                                     ({'api_key': 'fake-search-key', 'query': 'question'}, {})]:
                with self.assertRaisesRegex(AIError, '重定向已拒绝'):
                    self.client._request(endpoint, payload, headers=headers)
            self.assertEqual([item[0] for item in observed], ['/redirect', '/redirect'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_errors_redact_credentials_and_reject_malformed_responses(self):
        with patch.object(self.client, '_chat', side_effect=AIError('Bad super-secret-token')):
            result = self.client.solve_questions([QUESTION])[0]
        self.assertNotIn('super-secret-token', result['reason'])
        for body in (b'[]', b'{}', b'not-json'):
            with patch('chaoxing_linux.assistant_ai._urlopen', return_value=Response(body)):
                result = self.client.test_connection()
                self.assertFalse(result['ok'])
                self.assertNotIn('super-secret-token', result['error'])


if __name__ == '__main__':
    unittest.main()
