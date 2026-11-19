"""Verify that course exports are readable and published as a complete pair."""
from concurrent.futures import CancelledError
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

from chaoxing_linux import review_documents


REVIEW = {
    'title': '数据库原理',
    'generated_at': '2026-11-19 15:24',
    'summary_mode': 'local',
    'sections': [{
        'title': '章节复习', 'items': [{
            'title': '关系模型',
            'summary': ['候选键的超键中不含多余属性。', '实体完整性要求主键不为空。'],
            'practice': ['尝试写出一个候选键。'],
            'source': 'https://mooc1.chaoxing.com/course/brief?courseId=9999',
        }, {
            'title': '事务',
            'summary': ['原子性要求事务整体提交或回滚。'],
            'source': 'https://mooc1.chaoxing.com/course/brief?enc=sample-private-token',
        }],
    }],
    'issues': ['附录资料需在课程页面查看。'],
    'counts': {'chapters': 2, 'assignments': 0, 'materials': 1},
}


class ReviewDocumentTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('libreoffice') or shutil.which('soffice'),
                         'LibreOffice Writer required to validate matching Word and PDF output')
    def test_export_preserves_content_sources_and_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = review_documents.export_review(REVIEW, root)
            docx, pdf = Path(paths['docx']), Path(paths['pdf'])
            self.assertEqual(docx.parent, pdf.parent)
            self.assertEqual(list(root.iterdir()), [docx.parent])
            self.assertEqual(len(list(docx.parent.iterdir())), 2)
            self.assertEqual(docx.parent.stat().st_mode & 0o777, 0o700)
            for product in docx, pdf:
                self.assertEqual(product.stat().st_mode & 0o777, 0o600)
            with ZipFile(docx) as bundle:
                document = ElementTree.fromstring(bundle.read('word/document.xml'))
                content = ' '.join(document.itertext())
                self.assertIn('数据库原理 课程复习指南', content)
                self.assertIn('实体完整性', content)
                self.assertIn('自测', content)
                self.assertIn('附录资料需在课程页面查看', content)
                relationships = bundle.read('word/_rels/document.xml.rels').decode()
                self.assertIn('courseId=9999', relationships)
                self.assertNotIn('sample-private-token', relationships)
            with pdf.open('rb') as handle:
                self.assertEqual(handle.read(5), b'%PDF-')
            if shutil.which('pdftotext') and shutil.which('fc-match'):
                fonts = subprocess.check_output(['fc-match', '-f', '%{lang}', ':lang=zh-cn'], text=True)
                if 'zh-cn' in fonts:
                    rendered = subprocess.check_output(['pdftotext', str(pdf), '-'], text=True)
                    self.assertIn('实体完整性', rendered)
                    self.assertIn('附录资料需在课程页面查看', rendered)

    def test_failed_conversion_and_cancellation_leave_no_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(review_documents, 'require_export_tools', return_value='/unused'):
                with patch.object(review_documents, '_convert_pdf', side_effect=ValueError('模拟转换错误')):
                    with self.assertRaisesRegex(ValueError, '模拟转换错误'):
                        review_documents.export_review(REVIEW, root)
                self.assertEqual(list(root.iterdir()), [])
                cancel = threading.Event()

                def converted(_program, _docx, pdf, _profile, _cancel):
                    pdf.touch()
                    cancel.set()

                with patch.object(review_documents, '_convert_pdf', side_effect=converted):
                    with self.assertRaises(CancelledError):
                        review_documents.export_review(REVIEW, root, cancel=cancel)
                self.assertEqual(list(root.iterdir()), [])

    def test_long_path_like_titles_stay_inside_destination_and_never_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review = dict(REVIEW, title='../资料\\备份\x01' + '课程名称' * 80)

            def converted(_program, _docx, pdf, _profile, _cancel):
                pdf.write_bytes(b'%PDF-1.7\n%%EOF')

            with patch.object(review_documents, 'require_export_tools', return_value='/unused'):
                with patch.object(review_documents, '_convert_pdf', side_effect=converted):
                    first = Path(review_documents.export_review(review, root)['docx'])
                    second = Path(review_documents.export_review(review, root)['docx'])
            self.assertNotEqual(first, second)
            for docx in first, second:
                self.assertEqual(docx.parent.parent, root)
                self.assertTrue(docx.is_file())
                self.assertLess(len(docx.name.encode()), 150)
                self.assertNotIn('\\', docx.name)
                self.assertNotIn('\x01', docx.name)
