from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from chaoxing_linux.study_worker import (InputRequired, Worker, checked_url,
                                        make_work_form, normalize_course, parse_work,
                                        process_identity, sign_parameters, verify_work_page)


HTML = '''<form action="/mooc-ans/work/addStudentWorkNew">
<input name="workId" value="88"><input name="enc" value="opaque">
<div class="singleQuesId" data="100"><div class="TiMu" data="0">
<div class="Zy_TItle">请选择质数</div><ul><li>A. 2</li><li>B. 4</li></ul>
<input name="answer100" value=""><input name="answertype100" value="0"></div></div>
<div class="singleQuesId" data="101"><div class="TiMu" data="3">
<div class="Zy_TItle">1 是质数</div><input name="answer101" value="">
<input name="answertype101" value="3"></div></div></form>'''
URL = 'https://mooc1-api.chaoxing.com/mooc-ans/mooc2/work/dowork?workId=88'
COURSE = {'courseId': '1', 'clazzId': '2', 'cpi': '3', 'title': '课程'}


class WorkTests(unittest.TestCase):
    def work(self):
        return parse_work(HTML, URL)

    def payload(self):
        return {'questions': self.work()['questions'], 'answers': {'100': 'A', '101': '错误'}}

    def test_parse_preserves_field_context_and_normalizes_choices(self):
        work = self.work()
        self.assertEqual(work['questions'][0]['options'], [{'key': 'A', 'text': '2'}, {'key': 'B', 'text': '4'}])
        self.assertEqual(work['context']['fields'], {'workId': '88', 'enc': 'opaque'})
        self.assertEqual(work['context']['submit_url'], 'https://mooc1-api.chaoxing.com/mooc-ans/work/addStudentWorkNew')

    def test_missing_form_action_uses_upstream_canonical_endpoint(self):
        work = parse_work(HTML.replace(' action="/mooc-ans/work/addStudentWorkNew"', ''), URL)
        self.assertEqual(work['context']['submit_url'], 'https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew')

    def test_question_form_is_selected_before_upstream_first_form_decoder(self):
        html = '<form id="search"><input name="query"></form>' + HTML
        def decoder(selected):
            from bs4 import BeautifulSoup
            first = BeautifulSoup(selected, 'lxml').find('form')
            self.assertIsNone(first.find(attrs={'name': 'query'}))
            self.assertIsNotNone(first.select_one('.singleQuesId'))
            return {}
        self.assertEqual(len(parse_work(html, URL, decoder)['questions']), 2)

    def test_save_and_submission_have_different_server_flags(self):
        payload = self.payload()
        saved = make_work_form(self.work(), payload)
        self.assertEqual(saved['pyFlag'], '1')
        self.assertEqual(saved['answer101'], 'false')
        payload['mode'] = 'submit'
        self.assertEqual(make_work_form(self.work(), payload)['pyFlag'], '')

    def test_empty_misaligned_and_invalid_answers_are_rejected(self):
        for change in ({'100': '', '101': 'true'}, {'100': 'Z', '101': 'true'}, {'100': 'A'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                make_work_form(self.work(), dict(self.payload(), answers=change))
        payload = self.payload()
        payload['questions'][0]['options'].reverse()
        with self.assertRaisesRegex(ValueError, '选项'):
            make_work_form(self.work(), payload)

    def test_ai_arrays_and_boolean_answers_use_native_form_values(self):
        work = self.work()
        work['questions'][0]['type'] = 'multiple'
        work['questions'][0]['answer_type'] = '1'
        payload = {'questions': deepcopy(work['questions']),
                   'answers': [{'id': '100', 'answer': ['B', 'A'], 'status': 'ok'},
                               {'id': '101', 'answer': False, 'status': 'ok'}]}
        fields = make_work_form(work, payload)
        self.assertEqual(fields['answer100'], 'AB')
        self.assertEqual(fields['answer101'], 'false')

    def test_fill_arrays_require_matching_named_blank_fields(self):
        work = {'questions': [{'id': '5', 'type': 'fill', 'answer_type': '2', 'text': '填空',
                              'options': [], 'blank_fields': ['answer5_1', 'answer5_2']}],
                'context': {'fields': {}}}
        payload = {'questions': deepcopy(work['questions']), 'answers': {'5': ['甲', '乙']}}
        fields = make_work_form(work, payload)
        self.assertEqual(fields['answer5_1'], '甲')
        self.assertEqual(fields['answer5_2'], '乙')
        work['questions'][0]['blank_fields'] = []
        with self.assertRaises(InputRequired):
            make_work_form(work, payload)

    def test_no_form_or_unsupported_post_target_cannot_submit(self):
        with self.assertRaises(InputRequired):
            parse_work('<p>作业已截止</p>', URL)
        with self.assertRaises(InputRequired):
            parse_work(HTML.replace('/mooc-ans/work/addStudentWorkNew', '/unrecognized'), URL)
        for url in ('http://mooc1.chaoxing.com/a', 'https://chaoxing.com.evil/a',
                    'https://evil/a', 'https://user@chaoxing.com/a'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                checked_url(url)

    def test_empty_question_stem_cannot_be_submitted(self):
        with self.assertRaises(InputRequired):
            parse_work(HTML.replace('请选择质数', ''), URL)

    def test_submit_acknowledgment_is_not_fabricated_verification(self):
        worker = Worker.__new__(Worker)
        worker.load_work = Mock(return_value=self.work())
        session = Mock()
        session.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'status': True, 'msg': '成功'})
        worker.session_manager = SimpleNamespace(get_session=lambda: session)
        worker.get_work_page = Mock(return_value=SimpleNamespace(text='<p>作业</p>'))
        result = worker.submit_work(dict(self.payload(), mode='submit'))
        self.assertTrue(result['ok'])
        self.assertFalse(result['verified'])
        self.assertEqual(result['mode'], 'submit')

    def test_readback_ignores_script_status_and_editable_form(self):
        fields = make_work_form(self.work(), self.payload())
        self.assertFalse(verify_work_page('<script>var msg="作业已提交";</script>', self.work(), fields, 'submit'))
        self.assertFalse(verify_work_page('<p>作业已提交</p>' + HTML, self.work(), fields, 'submit'))
        self.assertTrue(verify_work_page('<p>作业已提交，待批阅</p>', self.work(), fields, 'submit'))
        self.assertFalse(verify_work_page('<p>提交成功</p>', self.work(), fields, 'submit'))
        saved = '<input name="answer100" value="A"><input name="answer101" value="false">'
        self.assertTrue(verify_work_page(saved, self.work(), fields, 'save'))
        self.assertFalse(verify_work_page(saved.replace('value="A"', 'value="B"'), self.work(), fields, 'save'))

    def test_cross_domain_redirect_is_rejected_before_following(self):
        worker = Worker.__new__(Worker)
        session = Mock()
        session.get.return_value = SimpleNamespace(status_code=302, headers={'Location': 'https://example.com/steal'})
        worker.session_manager = SimpleNamespace(get_session=lambda: session)
        with self.assertRaises(ValueError):
            worker.get_work_page(URL)
        self.assertEqual(session.get.call_count, 1)


class SignTests(unittest.TestCase):
    def test_sign_types_require_their_actual_inputs(self):
        for kind in ('location', 'code', 'gesture', 'qr', 'face'):
            with self.subTest(kind=kind), self.assertRaises(InputRequired):
                sign_parameters(COURSE, {'activity_id': '42', 'sign_type': kind}, 'u', 'f')
        params = sign_parameters(COURSE, {'activity_id': '42', 'sign_type': 'code', 'sign_code': '0258'}, 'u', 'f')
        self.assertEqual(params['signCode'], '0258')
        params = sign_parameters(COURSE, {'activity_id': '42', 'sign_type': 'qr',
                                         'qr_content': 'https://mobilelearn.chaoxing.com/a?enc=current'}, 'u', 'f')
        self.assertEqual(params['enc'], 'current')

    def test_location_validates_ranges_and_does_not_infer_position(self):
        payload = {'activity_id': '42', 'sign_type': 'location', 'address': '现场', 'latitude': '91', 'longitude': '120'}
        with self.assertRaises(ValueError):
            sign_parameters(COURSE, payload, 'u', 'f')
        payload['latitude'] = '30'
        self.assertEqual(sign_parameters(COURSE, payload, 'u', 'f')['latitude'], 30)


class CourseTests(unittest.TestCase):
    def worker(self):
        worker = Worker.__new__(Worker)
        worker.emit = Mock()
        worker.client = Mock()
        worker.settings = {}
        return worker

    def test_plan_prevents_upstream_empty_chapter_side_effect(self):
        worker = self.worker()
        worker.client.get_course_point.return_value = {'points': [{'id': '10', 'title': '第一章'}]}
        original = worker.client.study_emptypage
        def upstream(course, point):
            worker.client.study_emptypage(course, point)
            return [], {}
        worker.client.get_job_list.side_effect = upstream
        plan = worker.plan({'course': COURSE})
        original.assert_not_called()
        self.assertIs(worker.client.study_emptypage, original)
        self.assertEqual(plan['tasks'][0]['type'], 'chapter')

    def test_duplicate_job_cards_do_not_schedule_a_quiz_twice(self):
        worker = self.worker()
        worker.client.get_course_point.return_value = {'points': [{'id': '10', 'title': '章节'}]}
        job = {'type': 'workid', 'jobid': 'work-88', 'enc': 'opaque'}
        info = {'knowledgeid': '10', 'ktoken': 'token', 'cpi': '3'}
        worker.client.get_job_list.return_value = [job, dict(job)], info
        tasks = worker.plan({'course': COURSE})['tasks']
        self.assertEqual(len(tasks), 1)
        from urllib.parse import parse_qs, urlsplit
        parameters = parse_qs(urlsplit(tasks[0]['work_url']).query, keep_blank_values=True)
        self.assertEqual(parameters['workId'], ['88'])
        self.assertEqual(parameters['knowledgeid'], ['10'])
        self.assertEqual(parameters['type'], [''])
        self.assertEqual(parameters['clazzId'], ['2'])

    def test_course_job_http_success_is_unverified_until_server_read(self):
        worker = self.worker()
        worker.plan = Mock(return_value={'course': COURSE, 'tasks': [
            {'id': 'a', 'name': '文档', 'type': 'document', 'status': 'pending', 'job': {}, 'point': {'id': '10'}}]})
        worker.client.study_document.return_value = SimpleNamespace(is_success=lambda: True, name='SUCCESS')
        worker.client.get_course_point.return_value = {'points': [{'id': '10', 'has_finished': False}]}
        result = worker.run_course({'course': COURSE})
        self.assertEqual(result['results'][0]['status'], 'reported')
        self.assertFalse(result['ok'])
        worker.client.get_course_point.return_value = {'points': [{'id': '10', 'has_finished': True}]}
        self.assertTrue(worker.run_course({'course': COURSE})['ok'])

    def test_quizzes_are_pending_without_explicit_solver(self):
        worker = self.worker()
        result = worker.run_quiz({'work_url': URL}, {})
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['work_url'], URL)

    def test_unknown_ai_answer_prevents_quiz_submission(self):
        worker = self.worker()
        worker.load_work = Mock(return_value=parse_work(HTML, URL))
        worker.submit_work = Mock()
        ai = Mock()
        ai.return_value.solve_questions.return_value = [{'id': '100', 'answer': None, 'status': 'unknown'}]
        with patch.dict(sys.modules, {'chaoxing_linux.assistant_ai': SimpleNamespace(AIClient=ai)}):
            result = worker.run_quiz({'work_url': URL}, {'solve_quizzes': True, 'submit_quizzes': True})
        worker.submit_work.assert_not_called()
        self.assertEqual(result['status'], 'needs_input')

    def test_quiz_save_is_not_a_completed_scored_task(self):
        worker = self.worker()
        worker.load_work = Mock(return_value=parse_work(HTML, URL))
        worker.submit_work = Mock(return_value={'ok': True, 'verified': True})
        ai = Mock()
        ai.return_value.solve_questions.return_value = [{'id': '100', 'answer': 'A', 'status': 'ok',
                                                        'search_status': {'state': 'ok'}, 'sources': [{'url': 'https://example.org'}]},
                                                       {'id': '101', 'answer': 'false', 'status': 'ok',
                                                        'search_status': {'state': 'ok'}, 'sources': [{'url': 'https://example.org'}]}]
        with patch.dict(sys.modules, {'chaoxing_linux.assistant_ai': SimpleNamespace(AIClient=ai)}):
            result = worker.run_quiz({'work_url': URL}, {'solve_quizzes': True})
            self.assertEqual(result['status'], 'saved')
            self.assertEqual(worker.submit_work.call_args.args[0]['mode'], 'save')
            result = worker.run_quiz({'work_url': URL}, {'solve_quizzes': True, 'submit_quizzes': True})
            self.assertEqual(result['status'], 'completed')

    def test_quiz_model_fallback_is_not_an_internet_verified_submission(self):
        worker = self.worker()
        worker.load_work = Mock(return_value=parse_work(HTML, URL))
        worker.submit_work = Mock()
        ai = Mock()
        ai.return_value.solve_questions.return_value = [
            {'id': '100', 'answer': 'A', 'status': 'ok', 'search_status': {'state': 'failed'}, 'sources': []},
            {'id': '101', 'answer': False, 'status': 'ok', 'search_status': {'state': 'failed'}, 'sources': []}]
        with patch.dict(sys.modules, {'chaoxing_linux.assistant_ai': SimpleNamespace(AIClient=ai)}):
            result = worker.run_quiz({'work_url': URL}, {'solve_quizzes': True, 'submit_quizzes': True})
        self.assertEqual(result['status'], 'needs_input')
        worker.submit_work.assert_not_called()

    def test_quiz_optional_search_can_finish_without_a_search_call(self):
        worker = self.worker()
        worker.load_work = Mock(return_value=parse_work(HTML, URL))
        worker.submit_work = Mock(return_value={'ok':True,'verified':True})
        ai = Mock()
        ai.return_value.solve_questions.return_value = [
            {'id':'100','answer':'A','status':'ok','search_status':{'state':'not_used'},'sources':[]},
            {'id':'101','answer':False,'status':'ok','search_status':{'state':'not_used'},'sources':[]}]
        with patch.dict(sys.modules, {'chaoxing_linux.assistant_ai': SimpleNamespace(AIClient=ai)}):
            result = worker.run_quiz({'work_url':URL},{'solve_quizzes':True,'submit_quizzes':True})
        self.assertEqual(result['status'],'completed')
        self.assertEqual(worker.submit_work.call_args[0][0]['mode'],'submit')


class OwnershipTests(unittest.TestCase):
    def test_process_start_identity_detects_missing_owner(self):
        self.assertTrue(process_identity(os.getpid()))
        self.assertIsNone(process_identity(999999999))

    def test_worker_guard_exits_without_owning_desktop(self):
        script = ('from chaoxing_linux.study_worker import start_parent_guard; import time; '
                  "start_parent_guard(999999999, 'missing', 0.01); time.sleep(5)")
        process = subprocess.run([sys.executable, '-c', script], timeout=2)
        self.assertEqual(process.returncode, 74)
