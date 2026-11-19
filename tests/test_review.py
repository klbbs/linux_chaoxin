from concurrent.futures import CancelledError
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from chaoxing_linux.assistant_ai import AIClient, AIError
from chaoxing_linux.review import build_review
from chaoxing_linux.service import AppService


ARCHIVE = {
    'ok': True, 'status': 'partial', 'course': {'title': '软件设计'},
    'chapters': [{'title': '状态机', 'text': '状态转换由事件触发。守卫条件决定转换能否执行。',
                  'url': 'https://mooc1.chaoxing.com/chapter?courseid=7&enc=hidden',
                  'children': [{'title': '转换', 'text': '定义不同状态之间的合法转换。'}]}],
    'assignments': [{'title': '第一课练习', 'status': '已提交',
                     'questions': [{'text': '什么是守卫条件？', 'options': []}]}],
    'materials': [{'title': '课件.pdf', 'text': '状态图用节点表示状态，使用有向边表示转换。'}],
    'issues': ['一项资料仅能读取标题。'],
}


class ReviewTests(unittest.TestCase):
    @staticmethod
    def summarize(items, _cancel):
        return {item['id']: [item['text']] for item in items}

    def test_ai_review_preserves_structure_and_removes_source_signature(self):
        review = build_review(ARCHIVE, summarize=self.summarize, cancel=threading.Event())
        self.assertEqual(review['counts'], {'chapters': 2, 'assignments': 1, 'materials': 1})
        chapters, assignments, materials = (section['items'] for section in review['sections'])
        self.assertEqual(chapters[0]['source'], 'https://mooc1.chaoxing.com/chapter?courseid=7')
        self.assertIn('守卫条件', ' '.join(chapters[0]['summary']))
        self.assertEqual(chapters[1]['title'], '状态机 / 转换')
        self.assertIn('什么是守卫条件？', assignments[0]['practice'][0])
        self.assertIn('已提交', assignments[0]['note'])
        self.assertIn('状态图', materials[0]['summary'][0])

    def test_model_can_mark_source_insufficient_without_local_summary(self):
        def summarize(items, _cancel):
            self.assertEqual(set(items[0]), {'id', 'title', 'text'})
            self.assertNotIn('hidden', str(items))
            return {'1': ['概括状态转换。'], **{item['id']: [] for item in items[1:]}}
        review = build_review(ARCHIVE, summarize=summarize, cancel=threading.Event())
        self.assertEqual(review['summary_mode'], 'ai')
        self.assertEqual(review['sections'][0]['items'][0]['summary'], ['概括状态转换。'])
        self.assertEqual(review['sections'][0]['items'][1]['summary'], [])

    def test_model_failure_propagates_and_cancel_prevents_model_call(self):
        model = Mock(side_effect=AIError('service unavailable'))
        event = threading.Event()
        with self.assertRaisesRegex(AIError, 'service unavailable'):
            build_review(ARCHIVE, summarize=model, cancel=event)
        event.set()
        model.reset_mock()
        with self.assertRaises(CancelledError):
            build_review(ARCHIVE, summarize=model, cancel=event)
        model.assert_not_called()

    def test_service_reads_then_calls_configured_ai_before_export(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = Mock()
            backend.execute.return_value = ARCHIVE
            service = AppService(folder + '/config', folder + '/data', backend_factory=lambda _: backend)
            files = {'docx': folder + '/output.docx', 'pdf': folder + '/output.pdf'}
            with patch('chaoxing_linux.review_documents.require_export_tools'), \
                 patch('chaoxing_linux.review_documents.export_review', return_value=files) as export, \
                 patch.object(service, '_ai') as ai:
                ai.return_value.summarize_review.side_effect = self.summarize
                result = service.request('review_export', {'course': {'courseId': '7', 'clazzId': '1'}, 'output_dir': folder})
            backend.execute.assert_called_once()
            self.assertEqual(backend.execute.call_args.args[0], 'review_collect')
            ai.return_value.summarize_review.assert_called_once()
            self.assertEqual(export.call_args.args[0]['title'], '软件设计')
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(result['files'], files)
            self.assertNotIn('chapters', result)
            service.shutdown()

    def test_service_never_exports_without_ai_or_on_model_error(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = Mock()
            backend.execute.return_value = ARCHIVE
            service = AppService(folder + '/config', folder + '/data', backend_factory=lambda _: backend)
            payload = {'course': {'courseId': '7', 'clazzId': '1'}, 'output_dir': folder, 'use_ai': False}
            with patch('chaoxing_linux.review_documents.require_export_tools'), \
                 patch('chaoxing_linux.review_documents.export_review') as export:
                self.assertEqual(service.request('review_export', payload)['status'], 'needs_input')
                backend.execute.assert_not_called()
                with patch.object(service, '_ai') as ai:
                    ai.return_value.summarize_review.return_value = {'1': ['遗漏其他条目']}
                    result = service.request('review_export', payload)
                self.assertEqual(result['status'], 'needs_input')
                self.assertNotIn('files', result)
                export.assert_not_called()
            service.shutdown()

    def test_openai_review_uses_official_responses_json_without_search(self):
        client = AIClient({'api_base': 'https://api.openai.com/v1', 'model': 'gpt-5-mini', 'api_key': 'fake-key'})
        client.check_review_settings()
        reply = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
                 'content': [{'type': 'output_text', 'text': '{"1":["概括"]}'}]}]}
        with patch.object(client, '_request', return_value=reply) as request:
            self.assertEqual(client.summarize_review([{'id': '1', 'title': '样例', 'text': '示例正文'}]), {'1': ['概括']})
        endpoint, payload = request.call_args.args
        self.assertEqual(endpoint, 'https://api.openai.com/v1/responses')
        self.assertEqual(payload['text']['format']['type'], 'json_object')
        self.assertFalse(payload['store'])
        self.assertNotIn('tools', payload)
        self.assertEqual(request.call_args.kwargs['headers']['Authorization'], 'Bearer fake-key')
