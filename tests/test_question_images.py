"""Assignment diagrams: offline DOM, authenticated-download and write guards."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from chaoxing_linux import question_images as images
from chaoxing_linux.study_worker import Worker, make_work_form, parse_work


URL = 'https://mooc1-api.chaoxing.com/mooc-ans/mooc2/work/dowork?workId=88'


def png(red=32):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0' + bytes([red, 64, 128]) * 2)) + chunk(b'IEND', b''))


def inline(data=None):
    return 'data:image/png;base64,' + base64.b64encode(png() if data is None else data).decode()


def descriptor(source=None, **changes):
    return dict({'id': 'q1-question-stem-1', 'label': '题干示意图', 'scope': 'question',
                 'source_url': inline() if source is None else source}, **changes)


def image_html():
    return '''<img src="https://example.org/outside.png"><form action="/mooc-ans/work/addStudentWorkNew">
    <input name="workId" value="88"><div class="singleQuesId" data="q1"><div class="TiMu" data="0">
    <div class="Zy_TItle"><img data-src="/diagram.png" src="placeholder.png"></div>
    <ul><li aria-label="A"><img src="//p.ananas.chaoxing.com/a.png"></li>
    <li>B. 文字选项</li></ul><input name="answertypeq1" value="0"></div></div></form>'''


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = Mock()

    def cached(self, **changes):
        return images.materialize_images([descriptor(**changes)], data_dir=self.root, session=self.session)[0]

    def test_inline_roundtrip_private_modes_and_no_base64_in_metadata(self):
        image = self.cached()
        self.assertEqual(images.normalize_images([image], data_dir=self.root), [image])
        self.assertEqual(Path(image['local_path']).read_bytes(), png())
        self.assertEqual(Path(image['local_path']).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'assignment-images').stat().st_mode & 0o777, 0o700)
        self.assertNotIn('base64,', json.dumps(image))
        self.session.get.assert_not_called()

    def test_dom_keeps_pure_image_stem_option_association_and_lazy_url(self):
        question = parse_work(image_html(), URL)['questions'][0]
        self.assertIn('题干见图片', question['text'])
        self.assertEqual(len(question['images']), 2)
        self.assertEqual(question['images'][0]['source_url'], 'https://mooc1-api.chaoxing.com/diagram.png')
        self.assertEqual(question['images'][1]['option_key'], 'A')
        self.assertIn('选项 A', question['options'][0]['text'])
        self.assertEqual(question['options'][1]['text'], '文字选项')

    def test_images_outside_title_are_kept_without_duplicates(self):
        html = image_html().replace('<div class="Zy_TItle">', '<img src="/material.png"><div class="Zy_TItle">')
        question = parse_work(html, URL)['questions'][0]
        self.assertEqual(len(question['images']), 3)
        self.assertEqual(len({image['id'] for image in question['images']}), 3)

    def test_decoded_text_still_uses_original_dom_images(self):
        decoded = {'questions': [{'id': 'q1', 'type': 'single', 'title': '解码题干',
            'options': 'A.\nB. 文字选项',
            'answerField': {'answertypeq1': '0'}}]}
        # The upstream decoder uses its own option/type representation.
        def decoder(_):
            return deepcopy(decoded)
        question = parse_work(image_html(), URL, decoder)['questions'][0]
        self.assertIn('解码题干', question['text'])
        self.assertEqual(len(question['images']), 2)

    def test_srcset_picture_and_lazy_attributes(self):
        from bs4 import BeautifulSoup
        html = '<picture><source srcset="/small.png 1x, /large.png 2x"><img src="/fallback.png"></picture>'
        node = BeautifulSoup(html, 'lxml').find('img')
        self.assertEqual(images.dom_image_source(node, URL), 'https://mooc1-api.chaoxing.com/large.png')
        node['data-original'] = '/original.png'
        self.assertTrue(images.dom_image_source(node, URL).endswith('/original.png'))

    def response(self, status=200, data=None, headers=None):
        return Mock(status_code=status, headers=headers or {}, iter_content=Mock(return_value=[png() if data is None else data]))

    def test_download_uses_supplied_session_and_manually_checks_redirect(self):
        first = self.response(302, headers={'Location': 'https://p.ananas.chaoxing.com/final.png'})
        final = self.response()
        self.session.get.side_effect = [first, final]
        image = self.cached(source='https://mooc1.chaoxing.com/image')
        self.assertEqual(image['status'], 'ready')
        self.assertEqual(self.session.get.call_count, 2)
        self.assertFalse(self.session.get.call_args.kwargs['allow_redirects'])
        first.close.assert_called_once()
        final.close.assert_called_once()

    def test_unsafe_source_and_external_redirect_are_explicit_failures(self):
        for source in ('http://mooc1.chaoxing.com/a', 'https://chaoxing.com.evil/a', 'file:///etc/passwd',
                       'https://user@chaoxing.com/a', 'https://127.0.0.1/a'):
            with self.subTest(source=source):
                self.assertEqual(self.cached(source=source)['status'], 'error')
        self.session.get.assert_not_called()
        response = self.response(302, headers={'Location': 'https://example.org/stolen'})
        self.session.get.return_value = response
        self.assertEqual(self.cached(source='https://mooc1.chaoxing.com/a')['status'], 'error')
        self.session.get.assert_called_once()
        response.close.assert_called_once()

    def test_http_failure_html_and_oversize_never_become_ready(self):
        for response in (self.response(403), self.response(data=b'<html>login</html>'),
                         self.response(headers={'Content-Length': str(9 * 1024 * 1024)})):
            self.session.get.return_value = response
            self.assertEqual(self.cached(source='https://mooc1.chaoxing.com/a')['status'], 'error')
            response.close.assert_called_once()

    def test_stream_limit_and_request_failure_have_no_secret_details(self):
        self.session.get.return_value = self.response(data=b'x' * 65)
        with patch.object(images, 'MAX_IMAGE_BYTES', 64):
            self.assertEqual(self.cached(source='https://mooc1.chaoxing.com/a')['status'], 'error')
        self.session.get.side_effect = RuntimeError('secret-session-cookie')
        image = self.cached(source='https://mooc1.chaoxing.com/a')
        self.assertNotIn('secret-session-cookie', json.dumps(image))

    def test_count_and_total_limits_preserve_failed_entries(self):
        with patch.object(images, 'MAX_QUESTION_IMAGES', 1):
            result = images.materialize_images([descriptor(), descriptor(id='extra')], data_dir=self.root, session=self.session)
        self.assertEqual(images.image_status(result), {'state': 'partial', 'count': 2, 'ready': 1})
        with patch.object(images, 'MAX_QUESTION_BYTES', len(png())):
            result = images.materialize_images([descriptor(), descriptor(id='extra')], data_dir=self.root, session=self.session)
        self.assertEqual([image['status'] for image in result], ['ready', 'error'])

    def test_corrupt_inline_and_mismatched_type_are_explicit_failures(self):
        for source in ('data:image/png;base64,INVALID!', inline(b'not an image'), inline().replace('image/png', 'image/jpeg')):
            self.assertEqual(self.cached(source=source)['status'], 'error')

    def test_changed_deleted_external_symlink_and_insecure_modes_rejected(self):
        image = self.cached()
        path = Path(image['local_path'])
        path.write_bytes(png(200))
        with self.assertRaises(ValueError):
            images.normalize_images([image], data_dir=self.root)
        path.unlink()
        with self.assertRaises(ValueError):
            images.normalize_images([image], data_dir=self.root)
        external = self.root / 'outside.png'
        external.write_bytes(png())
        external.chmod(0o600)
        path.symlink_to(external)
        with self.assertRaises(ValueError):
            images.normalize_images([image], data_dir=self.root)
        path.unlink()
        path.write_bytes(png())
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            images.normalize_images([image], data_dir=self.root)
        with self.assertRaises(ValueError):
            images.normalize_images([dict(image, local_path=str(external))], data_dir=self.root)

    def test_load_work_materializes_images_with_authenticated_session(self):
        worker = Worker.__new__(Worker)
        worker.data = self.root
        worker.decode_questions = None
        worker.session_manager = SimpleNamespace(get_session=lambda: self.session)
        worker.get_work_page = Mock(return_value=SimpleNamespace(status_code=200, text=image_html(), url=URL))
        self.session.get.return_value = self.response()
        work = worker.load_work({'url': URL})
        self.assertEqual(work['questions'][0]['image_status'], {'state': 'ready', 'count': 2, 'ready': 2})
        self.assertEqual(self.session.get.call_count, 2)

    def test_submit_rechecks_image_hash_and_never_posts_changed_work(self):
        image = self.cached()
        work = parse_work(image_html(), URL)
        question = work['questions'][0]
        question.update(images=[image], image_status=images.image_status([image]))
        payload = {'questions': deepcopy(work['questions']), 'answers': [{'id': 'q1', 'answer': 'A', 'status': 'ok',
            'image_status': {'state': 'ready', 'count': 1, 'sent': 1}}]}
        self.assertEqual(make_work_form(work, payload, data_dir=self.root)['answerq1'], 'A')
        changed = self.cached(source=inline(png(200)))
        work['questions'][0]['images'] = [changed]
        worker = Worker.__new__(Worker)
        worker.data = self.root
        worker.load_work = Mock(return_value=work)
        worker.session_manager = SimpleNamespace(get_session=lambda: self.session)
        with self.assertRaisesRegex(ValueError, '图片内容或位置已变化'):
            worker.submit_work(payload)
        self.session.post.assert_not_called()

    def test_missing_attachments_or_unsent_ai_results_cannot_write(self):
        image = self.cached()
        work = parse_work(image_html(), URL)
        work['questions'][0].update(images=[image], image_status=images.image_status([image]))
        payload = {'questions': deepcopy(work['questions']), 'answers': [{'id': 'q1', 'answer': 'A', 'status': 'ok'}]}
        with self.assertRaisesRegex(ValueError, '完整发送模型'):
            make_work_form(work, payload, data_dir=self.root)
        work['questions'][0].update(images=[], image_status={'state': 'failed', 'count': 1, 'ready': 0})
        payload['questions'] = deepcopy(work['questions'])
        payload['answers'] = {'q1': 'A'}
        with self.assertRaisesRegex(ValueError, '完整读取'):
            make_work_form(work, payload, data_dir=self.root)


if __name__ == '__main__':
    unittest.main()
