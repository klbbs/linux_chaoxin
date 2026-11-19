from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch

from chaoxing_linux.assistant_ai import AIClient, AICancelled, AIError, MAX_TOOL_CALLS, responses_endpoint

QUESTION = {'id': 'q1', 'type': 'single', 'text': 'Which number is even?',
            'options': [{'key': 'A', 'text': '1'}, {'key': 'B', 'text': '2'}]}
SOURCE = {'title': 'Source from search', 'url': 'https://example.org/even', 'snippet': 'Two is even.'}


def answer(**changes):
    result = {'id': 'q1', 'type': 'single', 'answer': 'B', 'confidence': .9,
              'reason': 'Two is even.', 'sources': []}
    result.update(changes)
    return json.dumps(result)


def tool(call_id='call_one', query='even number two', name='web_search', arguments=None):
    return {'id': call_id, 'type': 'function', 'function': {'name': name,
            'arguments': json.dumps({'query': query}) if arguments is None else arguments}}


def chat(content=None, calls=None):
    message = {'role': 'assistant', 'content': content}
    if calls is not None:
        message['tool_calls'] = calls
    return {'choices': [{'message': message}]}


def native(content=None, *, calls=None, annotations=None):
    return {'status': 'completed', 'output': (calls or []) + [
        {'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': content or answer(), 'annotations': annotations or []}]}]}


class AIToolTests(unittest.TestCase):
    def setUp(self):
        self.client = AIClient({'api_base': 'https://provider.example/v1', 'api_key': 'fake-private-key',
                                'model': 'configured-model', 'max_retries': 0, 'search_enabled': True})

    def test_default_mode_performs_actual_model_requested_tool_loop(self):
        events, sent = [], []
        replies = iter([chat(calls=[tool(query='precise model query')]), chat(answer(sources=[SOURCE['url']]))])
        def request(url, payload, **kwargs):
            events.append('model')
            sent.append(deepcopy(payload))
            return next(replies)
        def search(question, cancel):
            events.append('search')
            self.assertEqual(question, {'text': 'precise model query', 'options': []})
            return [SOURCE]
        with patch.object(self.client, '_request', side_effect=request), patch.object(self.client, '_search', side_effect=search):
            result = self.client.solve_questions([dict(QUESTION, cookies='account-private')])[0]
        self.assertEqual(events, ['model', 'search', 'model'])
        self.assertEqual(sent[0]['tools'][0]['function']['name'], 'web_search')
        self.assertEqual(sent[0]['tool_choice'], 'auto')
        self.assertNotIn('reference_results', sent[0]['messages'][1]['content'])
        self.assertNotIn('account-private', json.dumps(sent))
        self.assertNotIn('fake-private-key', json.dumps(sent))
        self.assertEqual(sent[1]['messages'][-1]['role'], 'tool')
        self.assertEqual(sent[1]['messages'][-1]['tool_call_id'], 'call_one')
        self.assertEqual(json.loads(sent[1]['messages'][-1]['content'])['results'], [SOURCE])
        self.assertEqual(result['sources'], [SOURCE])
        status = result['search_status']
        self.assertEqual((status['state'], status['mode'], status['count']), ('ok','tools',1))
        self.assertTrue(status['calls'][0]['executed'])
        self.assertEqual(status['calls'][0]['urls'], [SOURCE['url']])

    def test_model_not_using_tool_is_explicitly_not_used(self):
        with patch.object(self.client, '_request', return_value=chat(answer(reason='I already know this.'))) as request, \
             patch.object(self.client, '_search') as search:
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(request.call_count, 1)
        search.assert_not_called()
        self.assertEqual(result['search_status']['state'], 'not_used')
        self.assertEqual(result['search_status']['calls'], [])
        self.assertEqual(result['sources'], [])

    def test_no_search_permission_omits_tools_entirely(self):
        for allowed, enabled in [(False, True), (True, False)]:
            self.client.settings['search_enabled'] = enabled
            with patch.object(self.client, '_request', return_value=chat(answer())) as request, patch.object(self.client, '_search') as search:
                result = self.client.solve_questions([QUESTION], search=allowed)[0]
            self.assertNotIn('tools', request.call_args.args[1])
            search.assert_not_called()
            self.assertEqual(result['search_status']['state'], 'disabled')

    def test_tool_failure_empty_result_and_key_redaction(self):
        for found, state in [(AIError('fake-private-key search failed'), 'failed'), ([], 'empty')]:
            kwargs = {'side_effect': found} if isinstance(found, Exception) else {'return_value': found}
            with patch.object(self.client, '_request', side_effect=[chat(calls=[tool()]), chat(answer())]), \
                 patch.object(self.client, '_search', **kwargs):
                result = self.client.solve_questions([QUESTION])[0]
            self.assertEqual(result['search_status']['state'], state)
            self.assertEqual(result['sources'], [])
            self.assertNotIn('fake-private-key', json.dumps(result))

    def test_invalid_tool_arguments_never_execute_search(self):
        bad = [tool(name='run_shell'), tool(arguments='not-json'), tool(arguments='{"query":"one","query":"two"}'),
               tool(arguments='{"query":["wrong"]}'), tool(arguments='{"query":"ok","url":"https://arbitrary"}'),
               tool(query='x'), tool(query='x'*1001), tool(query='query\ncontrol'), tool(query='fake-private-key')]
        for call in bad:
            with self.subTest(call=call), patch.object(self.client, '_request', side_effect=[chat(calls=[call]),chat(answer())]), \
                 patch.object(self.client, '_search') as search:
                result = self.client.solve_questions([QUESTION])[0]
            search.assert_not_called()
            self.assertEqual(result['search_status']['state'], 'failed')
            self.assertFalse(result['search_status']['calls'][0]['executed'])
            self.assertNotIn('fake-private-key', json.dumps(result))

    def test_unknown_sources_remain_rejected_after_real_tool_calls(self):
        with patch.object(self.client, '_request', side_effect=[chat(calls=[tool()]), chat(answer(sources=['https://invented.example']))]), \
             patch.object(self.client, '_search', return_value=[SOURCE]):
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['answer'])
        self.assertEqual(result['sources'], [])

    def test_tool_round_limit_and_total_call_limit(self):
        replies = [chat(calls=[tool(call_id=f'call_{index}')]) for index in range(10)]
        with patch.object(self.client, '_request', side_effect=replies) as request, patch.object(self.client, '_search', return_value=[]) as search:
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(result['status'], 'unknown')
        self.assertLessEqual(search.call_count, 3)
        self.assertLessEqual(request.call_count, 4)
        calls = [tool(call_id=f'call_{index}') for index in range(MAX_TOOL_CALLS+1)]
        with patch.object(self.client, '_request', return_value=chat(calls=calls)), patch.object(self.client, '_search') as search:
            result = self.client.solve_questions([QUESTION])[0]
        search.assert_not_called()
        self.assertEqual(result['status'], 'unknown')

    def test_replayed_call_id_is_not_executed_twice(self):
        with patch.object(self.client, '_request', side_effect=[chat(calls=[tool()]),chat(calls=[tool()])]), \
             patch.object(self.client, '_search', return_value=[]) as search:
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(search.call_count, 1)
        self.assertEqual(result['status'], 'unknown')

    def test_cancel_between_model_and_tool_avoids_search(self):
        cancel = threading.Event()
        def request(*args, **kwargs):
            cancel.set()
            return chat(calls=[tool()])
        with patch.object(self.client, '_request', side_effect=request), patch.object(self.client, '_search') as search, self.assertRaises(AICancelled):
            self.client.solve_questions([QUESTION], cancel=cancel)
        search.assert_not_called()

    def test_unsupported_tools_do_not_silently_downgrade_to_plain_chat(self):
        payloads = []
        def request(url,payload,**kwargs):
            payloads.append(deepcopy(payload))
            raise AIError('服务返回 HTTP 400；unsupported tools')
        with patch.object(self.client, '_request', side_effect=request), patch.object(self.client, '_search') as search:
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(len(payloads), 2)
        self.assertTrue(all(payload.get('tools') for payload in payloads))
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['search_status']['state'], 'failed')
        search.assert_not_called()

    def test_connection_test_does_not_offer_or_execute_search(self):
        with patch.object(self.client, '_request', return_value=chat('{"ok":true}')) as request, patch.object(self.client, '_search') as search:
            self.assertTrue(self.client.test_connection()['ok'])
        self.assertNotIn('tools', request.call_args.args[1])
        search.assert_not_called()

    def test_responses_endpoint_conversion_keeps_the_configured_host(self):
        self.assertEqual(responses_endpoint('https://api.deepseek.com'), 'https://api.deepseek.com/responses')
        self.assertEqual(responses_endpoint('https://api.deepseek.com/v1'), 'https://api.deepseek.com/v1/responses')
        for base in ['https://gateway.example','https://gateway.example/v1',
                     'https://gateway.example/v1/chat/completions','https://gateway.example/v1/responses']:
            self.assertEqual(responses_endpoint(base), 'https://gateway.example/v1/responses')
        self.assertEqual(responses_endpoint('https://gateway.example/proxy/v1/chat/completions'),
                         'https://gateway.example/proxy/v1/responses')

    def test_native_mode_uses_responses_builtin_tool_and_actual_metadata(self):
        self.client.settings['search_mode'] = 'native'
        call = {'type':'web_search_call','id':'ws_one','status':'completed',
                'action':{'type':'search','sources':[{'type':'url','url':SOURCE['url'],'title':'Actual metadata'}]}}
        with patch.object(self.client, '_request', return_value=native(answer(sources=[SOURCE['url']]), calls=[call])) as request:
            result = self.client.solve_questions([QUESTION])[0]
        endpoint,payload = request.call_args.args[:2]
        self.assertEqual(endpoint, 'https://provider.example/v1/responses')
        self.assertEqual(payload['tools'], [{'type':'web_search'}])
        self.assertEqual(payload['include'], ['web_search_call.action.sources'])
        self.assertEqual(payload['tool_choice'], 'auto')
        self.assertEqual(payload['max_tool_calls'], MAX_TOOL_CALLS)
        self.assertFalse(payload['store'])
        self.assertEqual(result['search_status']['state'], 'ok')
        self.assertEqual(result['sources'][0]['url'], SOURCE['url'])
        self.assertEqual(result['sources'][0]['title'], 'Actual metadata')
        self.assertEqual(result['sources'][0]['snippet'], '')

    def test_native_annotations_require_a_completed_search_call(self):
        self.client.settings['search_mode'] = 'native'
        citation = {'type':'url_citation','url':SOURCE['url'],'title':'Actual citation','start_index':1,'end_index':5}
        call = {'type':'web_search_call','id':'ws_one','status':'completed','action':{'type':'search'}}
        with patch.object(self.client, '_request', return_value=native(calls=[call],annotations=[citation])):
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(result['sources'][0]['title'], 'Actual citation')
        self.assertEqual(result['search_status']['state'], 'ok')
        with patch.object(self.client, '_request', return_value=native(annotations=[citation])):
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(result['search_status']['state'], 'not_used')
        self.assertEqual(result['sources'], [])
        call['status'] = 'in_progress'
        with patch.object(self.client, '_request', return_value=native(calls=[call],annotations=[citation])):
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(result['search_status']['state'], 'failed')
        self.assertEqual(result['sources'], [])

    def test_native_prose_search_claim_without_call_does_not_count_as_browsing(self):
        self.client.settings['search_mode'] = 'native'
        with patch.object(self.client, '_request', return_value=native(answer(reason='I searched the internet.'))):
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(result['search_status']['state'], 'not_used')
        self.assertEqual(result['sources'], [])

    def test_native_incomplete_calls_and_invalid_source_urls_never_count_as_success(self):
        self.client.settings['search_mode'] = 'native'
        for call in [
            {'type':'web_search_call','id':'ws_one','status':'completed','action':{'type':'search','sources':[{'url':'https://bad host.example/page'}]}},
            {'type':'web_search_call','id':'ws_one','status':'completed','action':{'type':'search','sources':[{'url':'https://example.org:invalid/page'}]}},
            {'type':'web_search_call','status':'completed','action':{'type':'search','sources':[SOURCE]}},
        ]:
            with self.subTest(call=call), patch.object(self.client, '_request', return_value=native(calls=[call])):
                result = self.client.solve_questions([QUESTION])[0]
            self.assertNotEqual(result['search_status']['state'], 'ok')
            self.assertEqual(result['sources'], [])

    def test_native_citations_cannot_invent_sources_in_json_answer(self):
        self.client.settings['search_mode'] = 'native'
        call = {'type':'web_search_call','id':'ws_one','status':'completed','action':{'type':'search','sources':[SOURCE]}}
        with patch.object(self.client, '_request', return_value=native(answer(sources=['https://fabricated.example']),calls=[call])):
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['answer'])
        self.assertEqual(result['sources'], [])

    def test_explicit_prefetch_remains_available_with_distinct_evidence(self):
        self.client.settings['search_mode'] = 'prefetch'
        with patch.object(self.client, '_search', return_value=[SOURCE]) as search, \
             patch.object(self.client, '_request', return_value=chat(answer(sources=[SOURCE['url']]))) as request:
            result = self.client.solve_questions([QUESTION])[0]
        search.assert_called_once()
        self.assertNotIn('tools', request.call_args.args[1])
        self.assertEqual(result['search_status']['mode'],'prefetch')
        self.assertEqual(result['search_status']['calls'][0]['tool'],'prefetch_search')
        self.assertEqual(result['sources'],[SOURCE])

    def test_deepseek_official_search_reuses_only_its_own_model_key(self):
        self.client.settings.update(api_base='https://api.deepseek.com/v1', search_provider='deepseek')
        response = {'results': [dict(SOURCE, summary='Full official-search summary', snippet='Short'),
                               {'title':'Second','url':'https://example.org/second','summary':'  ','snippet':'Fallback snippet'},
                               {'title':'Unsafe','url':'javascript:alert(1)','summary':'No'}]}
        with patch.object(self.client, '_request', return_value=response) as request:
            found = self.client._search({'text':'补码 原码','options':[]},None)
        url,payload = request.call_args.args[:2]
        self.assertEqual(url,'https://api.deepseek.com/v1/tools/search')
        self.assertEqual(payload,{'query':'补码 原码','count':5,'need_content':False})
        self.assertEqual(request.call_args.kwargs['headers'],{'Authorization':'Bearer fake-private-key'})
        self.assertNotIn('fake-private-key',json.dumps(payload))
        self.assertEqual(found[0]['snippet'],'Full official-search summary')
        self.assertEqual(found[1]['snippet'],'Fallback snippet')
        self.assertEqual(len(found),2)

    def test_deepseek_never_forwards_a_gateway_key_without_explicit_search_key(self):
        self.client.settings['search_provider']='deepseek'
        for base in ['https://gateway.example/v1','https://api.deepseek.com.evil.test/v1',
                     'https://evil.test/proxy/api.deepseek.com','https://api.deepseek.com@evil.test/v1',
                     'https://api.deepseek.com./v1','http://api.deepseek.com/v1']:
            self.client.settings['api_base']=base
            with self.subTest(base=base), patch.object(self.client, '_request') as request, self.assertRaises(AIError):
                self.client._search({'text':'补码','options':[]},None)
            request.assert_not_called()
        self.client.settings.update(api_base='https://gateway.example/v1',search_key='explicit-search-key')
        with patch.object(self.client, '_request',return_value={'results':[]}) as request:
            self.assertEqual(self.client._search({'text':'补码','options':[]},None),[])
        self.assertEqual(request.call_args.kwargs['headers']['Authorization'],'Bearer explicit-search-key')
        self.assertNotIn('fake-private-key',json.dumps(request.call_args.args))

    def test_deepseek_search_enforces_unicode_query_limit_without_truncating(self):
        self.client.settings.update(api_base='https://api.deepseek.com',search_provider='deepseek')
        for query in ['补'*129,'','   ','unsafe\nquery']:
            with self.subTest(query=query), patch.object(self.client, '_request') as request,self.assertRaisesRegex(AIError,'128'):
                self.client._search({'text':query,'options':[]},None)
            request.assert_not_called()
        query='补'*128
        with patch.object(self.client, '_request',return_value={'results':[]}) as request:
            self.client._search({'text':query,'options':[]},None)
        self.assertEqual(request.call_args.args[1]['query'],query)
        for response in [{}, {'results':{}}, []]:
            with patch.object(self.client, '_request',return_value=response),self.assertRaises(AIError):
                self.client._search({'text':'补码','options':[]},None)

    def test_deepseek_flash_native_is_rejected_explicitly_but_connection_is_plain(self):
        self.client.settings.update(api_base='https://api.deepseek.com',search_mode='native')
        self.client.model='deepseek-flash'
        with patch.object(self.client, '_request') as request:
            result=self.client.solve_questions([QUESTION])[0]
        request.assert_not_called()
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(result['search_status']['state'],'failed')
        self.assertIn('Flash',result['reason'])
        self.assertIn('tools',result['reason'])
        with patch.object(self.client, '_request',return_value=native('{"ok":true}')) as request:
            self.assertTrue(self.client.test_connection()['ok'])
        self.assertTrue(request.call_args.args[0].endswith('/responses'))
        self.assertNotIn('tools',request.call_args.args[1])

    def test_deepseek_tool_continuation_preserves_thinking_and_can_shorten_query(self):
        self.client.settings.update(api_base='https://api.deepseek.com',search_provider='deepseek')
        self.client.model='deepseek-flash'
        first=chat(calls=[tool(call_id='too_long',query='补'*129)])
        first['choices'][0]['message']['reasoning_content']='opaque-thinking-one'
        second=chat(calls=[tool(call_id='shortened',query='补')])
        second['choices'][0]['message']['reasoning_content']='opaque-thinking-two'
        replies=iter([first,second,chat(answer(sources=[SOURCE['url']]))])
        requests=[]
        def request(url,payload,**kwargs):
            requests.append((url,deepcopy(payload)))
            if url.endswith('/v1/tools/search'):
                return {'results':[dict(SOURCE,summary=SOURCE['snippet'])]}
            return next(replies)
        with patch.object(self.client, '_request',side_effect=request):
            result=self.client.solve_questions([QUESTION])[0]
        model_payloads=[payload for url,payload in requests if url.endswith('/chat/completions')]
        search_payloads=[payload for url,payload in requests if url.endswith('/tools/search')]
        self.assertEqual(search_payloads,[{'query':'补','count':5,'need_content':False}])
        tool_schema=model_payloads[0]['tools'][0]['function']
        self.assertEqual(tool_schema['parameters']['properties']['query']['minLength'],1)
        self.assertEqual(tool_schema['parameters']['properties']['query']['maxLength'],128)
        self.assertNotIn('strict',tool_schema)
        self.assertTrue(all('thinking' not in payload and 'reasoning' not in payload for payload in model_payloads))
        self.assertIn('128',json.loads(model_payloads[1]['messages'][-1]['content'])['error'])
        self.assertEqual(model_payloads[1]['messages'][-2]['reasoning_content'],'opaque-thinking-one')
        assistant_messages=[message for message in model_payloads[-1]['messages'] if message['role']=='assistant']
        self.assertEqual([message['reasoning_content'] for message in assistant_messages],
                         ['opaque-thinking-one','opaque-thinking-two'])
        self.assertNotIn('opaque-thinking',json.dumps(result))
        self.assertEqual(result['sources'],[SOURCE])
        self.assertEqual(result['search_status']['state'],'ok')
        self.assertFalse(result['search_status']['calls'][0]['executed'])
        self.assertTrue(result['search_status']['calls'][1]['executed'])

    def test_native_connection_uses_responses_without_tools(self):
        self.client.settings['search_mode'] = 'native'
        with patch.object(self.client, '_request', return_value=native('{"ok":true}')) as request, patch.object(self.client, '_search') as search:
            self.assertTrue(self.client.test_connection()['ok'])
        endpoint,payload = request.call_args.args[:2]
        self.assertTrue(endpoint.endswith('/responses'))
        self.assertNotIn('tools',payload)
        self.assertNotIn('include',payload)
        search.assert_not_called()

    def test_native_unsupported_protocol_is_an_error_without_chat_fallback(self):
        self.client.settings['search_mode'] = 'native'
        with patch.object(self.client, '_request', return_value=chat(answer())) as request:
            result = self.client.solve_questions([QUESTION])[0]
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(result['search_status']['state'],'failed')


if __name__ == '__main__':
    unittest.main()
