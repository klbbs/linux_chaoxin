"""Exercise the course collector with a synthetic student session."""
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from chaoxing_linux.review_fetch import _document_text, collect_course


COURSE = {'courseId': '101', 'clazzId': '202', 'cpi': '303', 'name': '离散数学'}
ENTRY = 'https://mooc2-ans.chaoxing.com/visit/stucoursemiddle?courseid=101&clazzid=202&enc=course-secret'
MATERIAL_PATH = '/mooc2-ans/coursedata/stu-datalist'
DETAIL_PATH = '/mooc-ans/work/record-detail'
TASK_PATH = '/mooc-ans/mooc2/work/task'
DOWNLOAD_PATH = '/mooc2-ans/coursedata/downloadData'
VIEW_PATH = '/mooc-ans/mooc2/work/view'
CDN_URL = 'https://d0.cldisk.com/download/signed-opaque'
COURSE_VIEW = '/mooc2-ans/mycourse/stu'
ENTRY_PAGE = f'''
    <input id="courseid" value="101"><input id="clazzid" value="202"><input id="cpi" value="303">
    <input id="enc" value="page-secret">
    <li dataname="zl"><a data-url="{MATERIAL_PATH}">资料</a></li>
'''
CHAPTERS = '''
    <div class="chapter_unit"><ul>
    <li><div id="cur123"><a class="clicktitle">第一章 命题与量词</a></div></li>
    <li><div id="cur124"><a class="clicktitle">第二章 关系</a><span class="bntHoverTips">需要解锁</span></div></li>
    </ul></div>
'''
MATERIALS = f'''
    <div class="dataBody">
        <ul dataname="布尔代数讲义.docx" type="docx" id="123">
            <li class="dataBody_name_stu"><span class="rename_title">布尔代数讲义.docx</span></li>
            <li><a href="{DOWNLOAD_PATH}?enc=file-secret">下载</a></li>
        </ul>
        <ul dataname="受限.pdf" type="pdf" id="124" isdown="0"></ul>
    </div>
'''
CARD = '''
    <div id="content"><p>德摩根定律把否定分配到合取与析取。</p>
    <p hidden>不能导出的内容</p><p><img alt="命题真值表"></p>
    <script>var mArg = {"defaults":{"cardCount":1}, "attachments":[{"type":"video","property":{"name":"课堂视频"}}]};</script>
    </div>
'''
CURSOR = '''
    <input id="courseid" value="101"><input id="clazzid" value="202"><input id="cpi" value="303">
    <input id="enc" value="folder-secret"><input id="t" value="123456"><input id="ut" value="s">
'''
DETAIL = '''
    <div class="TiMu singleQuesId" data="700">
      <div class="Zy_TItle"><span class="newZy_TItle">单选题</span>命题 P 与 Q 都为真时，合取为？</div>
      <ul><li>A. 真</li><li>B. 假</li></ul>
      <span>正确答案：</span><div class="fl answerCon">A</div>
    </div>
'''


def office_document():
    data = BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="urn:test"><w:body><w:p>'
                         '<w:r><w:t>布尔函数可表示为析取范式。</w:t></w:r>'
                         '</w:p></w:body></w:document>')
    return data.getvalue()


class Response:
    def __init__(self, url, body='', status=200, headers=None):
        self.url, self.status_code, self.headers = url, status, headers or {}
        self.body = body.encode() if isinstance(body, str) else body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, _):
        yield self.body


class CourseReviewTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.get.side_effect = self.reply
        self.worker = SimpleNamespace(session=self.session, emit=Mock(), course_entry_url=Mock(return_value=ENTRY),
            list_work=Mock(return_value={'status': 'success', 'works': [
                {'id': '701', 'title': '已提交作业', 'status': 'submitted', 'status_text': '已批阅',
                 'url': f'https://mooc1.chaoxing.com{DETAIL_PATH}?workId=701&enc=assignment-secret'},
                {'id': '702', 'title': '待交作业', 'status': 'pending',
                 'url': f'https://mooc1.chaoxing.com{TASK_PATH}?workId=702&enc=start-secret'},
            ]}))
        self.detail_redirect = ''
        self.folder_listing = False
        self.cdn_download = False
        self.full_course_page = False
        self.embedded_question = False

    def reply(self, url, **kwargs):
        self.assertFalse(kwargs['allow_redirects'])
        query = parse_qs(urlsplit(url).query)
        path = urlsplit(url).path
        if path.endswith('/mycourse/studentcourse'):
            return Response(url, CHAPTERS)
        if path.endswith('/visit/stucoursemiddle'):
            if self.full_course_page:
                return Response(url, status=302, headers={'Location': f'{COURSE_VIEW}?courseid=101&clazzid=202&enc=redirect-secret'})
            return Response(url, ENTRY_PAGE)
        if path == COURSE_VIEW:
            return Response(url, ENTRY_PAGE)
        if path == MATERIAL_PATH:
            if self.folder_listing:
                if query.get('dataId') == ['901']:
                    self.assertEqual(query.get('dataName'), ['教材'])
                    self.assertEqual(query.get('enc'), ['folder-secret'])
                    self.assertNotIn('stuenc', query)
                    return Response(url, CURSOR + MATERIALS)
                return Response(url, CURSOR + '<ul dataname="%E6%95%99%E6%9D%90" type="afolder" id="901"></ul>')
            self.assertEqual(query['stuenc'], ['page-secret'])
            return Response(url, MATERIALS)
        if path == DOWNLOAD_PATH:
            if self.cdn_download:
                return Response(url, status=302, headers={'Location': CDN_URL})
            return Response(url, office_document(), headers={'Content-Type': 'application/octet-stream'})
        if path.endswith('/knowledge/cards'):
            self.assertEqual(query['knowledgeid'], ['123'])
            self.assertEqual(query['num'], ['0'])
            return Response(url, CARD)
        if path == DETAIL_PATH:
            if self.detail_redirect:
                return Response(url, status=302, headers={'Location': self.detail_redirect})
            if self.embedded_question:
                return Response(url, '<div class="questionLi singleQuesId"><div class="mark_name">'
                                     '<div class="qtContent"><div class="editor-iframe">'
                                     '<iframe class="attach-iframe" filename="逻辑题练习单.docx"'
                                     ' src="/ananas/modules/placeholder.html"></iframe></div></div></div></div>')
            return Response(url, DETAIL)
        if path == TASK_PATH:
            self.assertEqual(query.get('answerId'), ['750'])
            return Response(url, status=302,
                headers={'Location': f'{VIEW_PATH}?courseid=101&classid=202&workid=701&answerid=750&enc=view-secret'})
        if path == VIEW_PATH:
            return Response(url, '<div class="questionLi singleQuesId"><div class="mark_name">'
                                 '<span>单选题</span><div class="qtContent">逻辑题</div></div>'
                                 '<div class="rightAnswerContent"><img alt="真值表"></div></div>')
        self.fail(f'Unexpected GET to {path}')

    def test_collects_chapters_questions_filenames_and_downloadable_text(self):
        result = collect_course(self.worker, {'course': COURSE})
        self.assertEqual(result['course']['title'], '离散数学')
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['chapters']), 2)
        self.assertIn('德摩根定律', result['chapters'][0]['text'])
        self.assertIn('[命题真值表]', result['chapters'][0]['text'])
        self.assertNotIn('不能导出的内容', result['chapters'][0]['text'])
        self.assertEqual(result['chapters'][1]['status'], 'locked')
        self.assertEqual(result['assignments'][0]['questions'][0]['correct_answer'], 'A')
        self.assertIn('B. 假', result['assignments'][0]['text'])
        self.assertEqual(result['assignments'][1]['text'], '')
        self.assertIn('布尔函数可表示为析取范式', result['materials'][0]['text'])
        self.assertIn('受限.pdf', [item['title'] for item in result['materials']])
        self.assertIn('课堂视频', [item['title'] for item in result['materials']])
        self.assertNotIn('secret', repr(result))
        self.session.post.assert_not_called()

    def test_rejects_external_or_write_path_redirects_before_contact(self):
        for url in ('https://fake.invalid/record-detail', f'https://mooc1.chaoxing.com{TASK_PATH}?workId=701',
                    f'https://mooc1.chaoxing.com{DETAIL_PATH}?workId=999'):
            with self.subTest(url=url):
                self.detail_redirect = url
                self.session.get.reset_mock()
                result = collect_course(self.worker, {'course': COURSE})
                self.assertEqual(result['assignments'][0]['questions'], [])
                self.assertTrue(any('允许的读取范围' in issue for issue in result['issues']))
                self.assertNotIn(url, [call.args[0] for call in self.session.get.call_args_list])

    def test_text_file_parser_rejects_html_errors(self):
        for ext in ('.txt', '.md', '.csv'):
            with self.subTest(ext=ext), self.assertRaisesRegex(ValueError, '文本文件'):
                _document_text(b'<html>sign in</html>', ext)

    def test_submitted_positive_answer_uses_only_the_review_redirect(self):
        work = self.worker.list_work.return_value['works'][0]
        base = f'https://mooc1.chaoxing.com{TASK_PATH}?courseId=101&classId=202&workId=701&answerId='
        work['url'] = base + '750&enc=task-secret'
        result = collect_course(self.worker, {'course': COURSE})
        work_result = result['assignments'][0]
        self.assertTrue(work_result['url'].endswith(f'{VIEW_PATH}?courseid=101&classid=202&workid=701'))
        self.assertEqual(work_result['questions'][0]['text'], '逻辑题')
        self.assertEqual(work_result['questions'][0]['correct_answer'], '[真值表]')
        self.assertNotIn('secret', repr(result))
        work['url'] = base + '0'
        self.session.get.reset_mock()
        result = collect_course(self.worker, {'course': COURSE})
        self.assertFalse(result['assignments'][0]['text'])
        self.assertFalse(any(urlsplit(call.args[0]).path == TASK_PATH for call in self.session.get.call_args_list))

    def test_official_subfolder_uses_cursor_without_exporting_signatures(self):
        self.folder_listing = True
        result = collect_course(self.worker, {'course': COURSE})
        self.assertEqual(result['materials'][0]['folder'], '教材')
        self.assertIn('析取范式', result['materials'][0]['text'])
        self.assertNotIn('secret', repr(result))

    def test_download_cdn_uses_new_session_and_origin_only_referer(self):
        self.cdn_download = True
        with patch('requests.Session') as factory:
            isolated = factory.return_value.__enter__.return_value
            isolated.get.return_value = Response(CDN_URL, office_document(),
                                                  headers={'Content-Type': 'application/octet-stream'})
            result = collect_course(self.worker, {'course': COURSE})
        self.assertFalse(isolated.trust_env)
        self.assertEqual(isolated.get.call_args.kwargs['headers'], {'Referer': 'https://mooc2-ans.chaoxing.com/'})
        self.assertEqual(isolated.get.call_args.args, (CDN_URL,))
        self.assertFalse(any(call.args[0] == CDN_URL for call in self.session.get.call_args_list))
        self.assertIn('析取范式', result['materials'][0]['text'])
        self.assertNotIn('signed-opaque', repr(result))

    def test_preserves_embedded_only_question_and_follows_official_course_entry(self):
        self.full_course_page = self.embedded_question = True
        result = collect_course(self.worker, {'course': COURSE})
        work = result['assignments'][0]
        self.assertIn('原题见嵌入附件', work['text'])
        self.assertIn('逻辑题练习单.docx', work['text'])
        self.assertEqual(work['questions'][0]['attachments'][0]['url'], '')
        self.assertIn('布尔函数可表示为析取范式', result['materials'][0]['text'])
        self.assertTrue(any('原页未提供可用下载链接' in issue for issue in result['issues']))
        self.assertNotIn('secret', repr(result))
        self.assertFalse(any('/placeholder.html' in call.args[0] for call in self.session.get.call_args_list))

    def test_chapter_uses_exact_linked_material_when_card_has_only_notice(self):
        card = CARD.replace('德摩根定律把否定分配到合取与析取。', '此内容由 AI 生成，仅供参考。')
        card = card.replace('<p><img alt="命题真值表"></p>', '<p><img src="empty.svg">暂无内容</p>')
        card = card.replace('"video"', '"document"').replace('课堂视频', '布尔代数讲义.docx')
        card = card.replace('id="content"', 'id="pageDiv" class="ans-cc"') + '<div class="func-pop"><p>标注</p><p>写笔记</p></div>'
        with patch(__name__ + '.CARD', card):
            result = collect_course(self.worker, {'course': COURSE})
        chapter = result['chapters'][0]
        self.assertIn('析取范式', chapter['text'])
        self.assertNotIn('AI 生成', repr(chapter))
        self.assertNotIn('写笔记', chapter['text'])
        self.assertNotIn('暂无内容', chapter['text'])
        self.assertEqual(sum(item['title'] == '布尔代数讲义.docx' for item in result['materials']), 1)


if __name__ == '__main__':
    unittest.main()
