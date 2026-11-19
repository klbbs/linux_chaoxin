"""Synthetic fixtures preserve the structure observed on the official course/list pages."""
from collections import Counter
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import Mock

from chaoxing_linux.study_worker import InputRequired, Worker, parse_work_list


COURSE = {'courseId': '11', 'clazzId': '22', 'cpi': '33'}
ENTRY_URL = 'https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid=11&clazzid=22&cpi=33&enc=course-supplied'
LIST_URL = 'https://mooc1.chaoxing.com/mooc2/work/list?courseId=11&classId=22&cpi=33&ut=s&enc=navigation-supplied'
TASK_URL = 'https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=11&classId=22&cpi=33&workId=44&answerId=0&enc=task-supplied'
ENTRY = '''<input type="hidden" id="courseid" value="11">
<input type="hidden" id="clazzid" value="22"><input type="hidden" id="cpi" value="33">
<input type="hidden" id="workEnc" value="navigation-supplied">
<li dataname="zy"><a data-url="https://mooc1.chaoxing.com/mooc2/work/list">作业</a></li>'''


def row(url=TASK_URL, status='未交', title='合成测试作业'):
    return f'''<li onclick="goTask(this)" data="{url.replace('&', '&amp;')}">
<div class="tag work-icon icon-zy-g"></div><div class="right-content">
<p class="overHidden2 fl">{title}</p><div class="clear"></div>
<p class="status fl">{status}</p></div><div class="clearfix"></div></li>'''


def page(rows='', extra=''):
    return '<div class="bottomList"><ul>' + rows + '</ul></div>' + extra


def response(html, url=LIST_URL):
    return SimpleNamespace(text=html, url=url, status_code=200)


class WorkListParserTests(unittest.TestCase):
    def test_official_data_link_is_preserved_without_generating_signature(self):
        parsed = parse_work_list(page(row()), LIST_URL)
        self.assertEqual(len(parsed['works']), 1)
        work = parsed['works'][0]
        self.assertEqual(work['id'], '44')
        self.assertEqual(work['url'], TASK_URL)
        self.assertEqual(work['status'], 'pending')
        self.assertTrue(work['actionable'])
        self.assertTrue(work['requires_start'])

    def test_submitted_and_expired_items_are_not_editable(self):
        completed = row(TASK_URL.replace('workId=44', 'workId=45').replace('answerId=0', 'answerId=55'), '已完成')
        expired = row(TASK_URL, '未交 已截止')
        works = parse_work_list(page(completed + expired), LIST_URL)['works']
        self.assertEqual([work['status'] for work in works], ['submitted', 'expired'])
        self.assertTrue(all(not work['actionable'] and not work['requires_start'] for work in works))

    def test_detail_urls_and_unknown_status_never_imply_can_answer(self):
        detail = TASK_URL.replace('/work/task?', '/work/record-detail?')
        works = parse_work_list(page(row(detail, '未交')), LIST_URL)['works']
        self.assertFalse(works[0]['actionable'])
        works = parse_work_list(page(row(status='未知状态')), LIST_URL)['works']
        self.assertEqual(works[0]['status'], 'unknown')
        self.assertFalse(works[0]['actionable'])

    def test_untrusted_urls_are_excluded_and_duplicate_rows_collapse(self):
        unsafe = row(TASK_URL.replace('https://mooc1.chaoxing.com', 'https://example.com'))
        unsafe += row(TASK_URL.replace('https:', 'http:'))
        unsafe += row('javascript:alert(1)')
        result = parse_work_list(page(unsafe + row() + row()), LIST_URL)
        self.assertEqual(len(result['works']), 1)
        self.assertEqual(result['works'][0]['url'], TASK_URL)

    def test_login_unknown_page_and_empty_list_are_distinct(self):
        with self.assertRaisesRegex(InputRequired, '登录'):
            parse_work_list('<form><input name="password"></form>', LIST_URL)
        with self.assertRaisesRegex(InputRequired, '未识别'):
            parse_work_list('<p>页面维护中</p>', LIST_URL)
        self.assertEqual(parse_work_list(page(), LIST_URL)['works'], [])


class WorkListReadOnlyTests(unittest.TestCase):
    def worker(self):
        worker = Worker.__new__(Worker)
        worker.emit = Mock()
        return worker

    def test_list_navigation_uses_real_page_values_and_never_opens_tasks(self):
        worker = self.worker()
        worker.course_entry_url = Mock(return_value=ENTRY_URL)
        worker.get_work_page = Mock(side_effect=[response(ENTRY, ENTRY_URL), response(page(row()))])
        result = worker.list_work({'course': COURSE})
        self.assertTrue(result['ok'])
        self.assertEqual(result['works'][0]['url'], TASK_URL)
        called = [call.args[0] for call in worker.get_work_page.call_args_list]
        self.assertEqual(len(called), 2)
        self.assertEqual(urlsplit(called[1]).path, '/mooc2/work/list')
        self.assertEqual(parse_qs(urlsplit(called[1]).query)['enc'], ['navigation-supplied'])
        self.assertNotIn(TASK_URL, called)

    def test_missing_navigation_token_does_not_guess_or_fetch_work_list(self):
        worker = self.worker()
        worker.course_entry_url = Mock(return_value=ENTRY_URL)
        worker.get_work_page = Mock(return_value=response(ENTRY.replace('id="workEnc"', 'id="unused"'), ENTRY_URL))
        with self.assertRaisesRegex(InputRequired, '凭证'):
            worker.list_work({'course': COURSE})
        self.assertEqual(worker.get_work_page.call_count, 1)

    def test_courses_in_folders_use_actual_course_link(self):
        worker = self.worker()
        session = Mock()
        root_html = '<div class="course"><input class="courseId" value="99"><input class="clazzId" value="22"></div>'
        folder_html = ('<div class="course"><input class="courseId" value="11">'
                       '<input class="clazzId" value="22"><a href="' + ENTRY_URL + '">课程</a></div>')
        session.post.side_effect = [response(root_html), response(folder_html)]
        worker.session_manager = SimpleNamespace(get_session=lambda: session)
        worker.get_work_page = Mock(return_value=response('<ul class="file-list"><li fileid="7"></li></ul>'))
        self.assertEqual(worker.course_entry_url(COURSE), ENTRY_URL)
        self.assertEqual([call.kwargs['data']['courseFolderId'] for call in session.post.call_args_list], ['0', '7'])
        self.assertTrue(all(call.args[0].endswith('/visit/courselistdata') for call in session.post.call_args_list))

    def test_only_supplied_same_course_pagination_links_are_followed(self):
        worker = self.worker()
        worker.course_entry_url = Mock(return_value=ENTRY_URL)
        next_url = LIST_URL + '&page=2'
        other_course = next_url.replace('courseId=11', 'courseId=99')
        links = f'<div class="pageDiv"><a href="{next_url}">下一页</a><a href="{other_course}">其他</a></div>'
        second_row = row(TASK_URL.replace('workId=44', 'workId=45'), '已完成')
        worker.get_work_page = Mock(side_effect=[response(ENTRY, ENTRY_URL), response(page(row(), links)),
                                                response(page(second_row), next_url)])
        result = worker.list_work({'course': COURSE})
        self.assertEqual(len(result['works']), 2)
        self.assertEqual(worker.get_work_page.call_count, 3)
        self.assertEqual(worker.get_work_page.call_args.args[0], next_url)
