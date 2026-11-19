"""Render one course review as an editable DOCX and a matching PDF."""
from __future__ import annotations

from concurrent.futures import CancelledError
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


WORD = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
OFFICE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CORE_PROPERTIES = 'http://schemas.openxmlformats.org/package/2006/metadata/core-properties'
FONT = 'Noto Sans CJK SC'
PDF_TIMEOUT = 120.0

_INVALID_XML = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]')
_PRIVATE_QUERY_KEYS = {
    'auth', 'authorization', 'cookie', 'enc', 'moocenc', 'standardenc', 'password',
    'pwd', 'secret', 'session', 'sessionid', 'sign', 'signature', 'token', 'access_token',
}


def require_export_tools():
    """Return the Writer executable or raise an actionable setup error."""
    executable = shutil.which('libreoffice') or shutil.which('soffice')
    if not executable:
        raise ValueError('导出 PDF 需要 LibreOffice Writer。请安装 libreoffice-writer 与 fonts-noto-cjk 后重试。')
    return executable


def _check_cancel(cancel):
    if cancel is not None and (cancel.is_set() if hasattr(cancel, 'is_set') else cancel()):
        raise CancelledError()


def _text(value):
    return _INVALID_XML.sub('', value).strip() if isinstance(value, str) else ''


def _entries(value):
    return value if isinstance(value, (list, tuple)) else ()


def _lines(value):
    for item in (value,) if isinstance(value, str) else _entries(value):
        yield from (line.strip() for line in _text(item).splitlines() if line.strip())


def _single_line(value):
    return ' '.join(_text(value).split())


def _safe_source(url):
    url = _text(url)
    if not url or len(url) > 2048 or any(char.isspace() or ord(char) < 32 for char in url):
        return ''
    try:
        parsed = urlsplit(url)
        sensitive = any(key.lower() in _PRIVATE_QUERY_KEYS or key.lower().endswith(('token', 'signature'))
                        for key, _ in parse_qsl(parsed.query, max_num_fields=32))
        if (parsed.scheme == 'https' and parsed.hostname and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None and not sensitive):
            return url
    except ValueError:
        pass
    return ''


def _w(parent, tag, **attrs):
    return ET.SubElement(parent, f'w:{tag}', {f'w:{key}': str(value) for key, value in attrs.items()})


def _paragraph(parent, style=None, text=None):
    paragraph = _w(parent, 'p')
    if style:
        _w(_w(paragraph, 'pPr'), 'pStyle', val=style)
    if text:
        _w(_w(paragraph, 'r'), 't').text = text
    return paragraph


def _relationships():
    return ET.Element('Relationships', xmlns=PACKAGE_REL)


def _relationship(root, identifier, kind, target, *, external=False):
    attrs = {'Id': identifier, 'Type': f'{OFFICE_REL}/{kind}', 'Target': target}
    if external:
        attrs['TargetMode'] = 'External'
    ET.SubElement(root, 'Relationship', attrs)


def _source_link(body, relationships, url):
    identifier = f'rId{len(relationships) + 1}'
    _relationship(relationships, identifier, 'hyperlink', url, external=True)
    paragraph = _paragraph(body, 'ReviewSource')
    link = ET.SubElement(paragraph, 'w:hyperlink', {'r:id': identifier})
    run = _w(link, 'r')
    properties = _w(run, 'rPr')
    _w(properties, 'color', val='174B70')
    _w(properties, 'u', val='single')
    _w(run, 't').text = '打开学习来源'


def _with_prefix(body, style, label, text):
    paragraph = _paragraph(body, style)
    run = _w(paragraph, 'r')
    _w(_w(run, 'rPr'), 'b')
    _w(run, 't').text = label
    node = _w(_w(paragraph, 'r'), 't')
    node.set('xml:space', 'preserve')
    node.text = '  ' + text


def _review_document(review, title, cancel):
    document = ET.Element('w:document', {'xmlns:w': WORD, 'xmlns:r': OFFICE_REL})
    body = _w(document, 'body')
    relations = _relationships()
    _relationship(relations, 'rId1', 'styles', 'styles.xml')
    _relationship(relations, 'rId2', 'footer', 'footer1.xml')
    _paragraph(body, 'Title', f'{title} 课程复习指南')
    generated_at = _single_line(review.get('generated_at'))[:80]
    mode = {'local': '本地摘编', 'ai': 'AI 整理', 'mixed': 'AI 与本地摘编'}.get(_text(review.get('summary_mode')))
    metadata = [value for value in (f'整理日期  {generated_at}' if generated_at else '', mode) if value]
    if metadata:
        _paragraph(body, 'ReviewMeta', '  ／  '.join(metadata))
    counts = review.get('counts') if isinstance(review.get('counts'), dict) else {}
    labels = (('chapters', '个章节'), ('assignments', '份作业'), ('materials', '份资料'))
    coverage = [
        f'{counts[key]} {label}' for key, label in labels
        if type(counts.get(key)) is int and counts[key] >= 0
    ]
    if coverage:
        _paragraph(body, 'ReviewMeta', '  ／  '.join(coverage))
    _paragraph(body, 'ReviewIntro', '先按章节回顾要点，再用作业和资料复习易混概念。遇到需要核对的内容，可打开相应学习来源。')

    for section in _entries(review.get('sections')):
        if not isinstance(section, dict):
            continue
        _check_cancel(cancel)
        _paragraph(body, 'Heading1', _single_line(section.get('title'))[:160] or '复习内容')
        items = [item for item in _entries(section.get('items')) if isinstance(item, dict)]
        if not items:
            _paragraph(body, 'ReviewNote', '本部分暂未收录内容。')
        for item in items:
            _check_cancel(cancel)
            if item_title := _single_line(item.get('title')):
                _paragraph(body, 'Heading2', item_title[:300])
            for line in _lines(item.get('summary')):
                _check_cancel(cancel)
                _paragraph(body, None, line)
            practice = iter(_lines(item.get('practice')))
            if first := next(practice, None):
                _with_prefix(body, 'ReviewPractice', '自测', first)
                for line in practice:
                    _check_cancel(cancel)
                    _paragraph(body, 'ReviewPractice', line)
            for line in _lines(item.get('note')):
                _paragraph(body, 'ReviewNote', line)
            if source := _safe_source(item.get('source')):
                _source_link(body, relations, source)

    issues = list(_lines(review.get('issues')))
    if issues:
        _paragraph(body, 'Heading1', '待查资料')
        for issue in issues:
            _check_cancel(cancel)
            _paragraph(body, 'ReviewNote', issue)

    section = _w(body, 'sectPr')
    _w(section, 'footerReference', type='default').set('r:id', 'rId2')
    _w(section, 'pgSz', w=11906, h=16838)
    _w(section, 'pgMar', top=1180, right=1200, bottom=1180, left=1200, header=520, footer=560, gutter=0)
    return document, relations


def _fonts(properties, size, color, *, bold=False):
    _w(properties, 'rFonts', ascii=FONT, hAnsi=FONT, eastAsia=FONT, cs=FONT)
    if bold:
        _w(properties, 'b')
    _w(properties, 'color', val=color)
    _w(properties, 'sz', val=size)
    _w(properties, 'szCs', val=size)
    _w(properties, 'lang', val='zh-CN', eastAsia='zh-CN')


def _styles():
    root = ET.Element('w:styles', {'xmlns:w': WORD})
    defaults = _w(root, 'docDefaults')
    _fonts(_w(_w(defaults, 'rPrDefault'), 'rPr'), 22, '161616')
    normal = _w(root, 'style', type='paragraph', default=1, styleId='Normal')
    _w(normal, 'name', val='Normal')
    normal_properties = _w(normal, 'pPr')
    _w(normal_properties, 'widowControl')
    _w(normal_properties, 'spacing', before=0, after=85, line=295, lineRule='auto')

    variants = (
        ('Title', 'Title', 46, '000000', 0, 195, True, None, None),
        ('Heading1', 'heading 1', 30, '000000', 225, 120, True, 0, None),
        ('Heading2', 'heading 2', 24, '000000', 130, 75, True, 1, None),
        ('ReviewMeta', 'Review metadata', 19, '454545', 0, 20, False, None, None),
        ('ReviewIntro', 'Review introduction', 22, '161616', 85, 105, False, None, None),
        ('ReviewNote', 'Review note', 20, '484848', 0, 100, False, None, None),
        ('ReviewPractice', 'Review practice', 22, '161616', 20, 65, False, None, 220),
        ('ReviewSource', 'Review link', 19, '174B70', 0, 95, False, None, None),
        ('ReviewFooter', 'Review page number', 18, '454545', 0, 0, False, None, None),
    )
    for key, name, size, color, before, after, heading, level, indent in variants:
        style = _w(root, 'style', type='paragraph', styleId=key)
        _w(style, 'name', val=name)
        _w(style, 'basedOn', val='Normal')
        _w(style, 'next', val='Normal')
        if heading:
            _w(style, 'qFormat')
        properties = _w(style, 'pPr')
        if heading:
            _w(properties, 'keepNext')
            _w(properties, 'keepLines')
        _w(properties, 'spacing', before=before, after=after, line=290, lineRule='auto')
        if indent is not None:
            _w(properties, 'ind', left=indent)
        if key == 'ReviewFooter':
            _w(properties, 'jc', val='right')
        if level is not None:
            _w(properties, 'outlineLvl', val=level)
        _fonts(_w(style, 'rPr'), size, color, bold=heading)
    return root


def _footer():
    footer = ET.Element('w:ftr', {'xmlns:w': WORD})
    paragraph = _paragraph(footer, 'ReviewFooter')
    field = _w(paragraph, 'fldSimple', instr='PAGE')
    _w(_w(field, 'r'), 't').text = '1'
    return footer


def _content_types():
    root = ET.Element('Types', xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
    ET.SubElement(root, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
    for part, kind in (('/word/document.xml', 'document.main'), ('/word/styles.xml', 'styles'), ('/word/footer1.xml', 'footer')):
        ET.SubElement(root, 'Override', PartName=part, ContentType=f'application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}+xml')
    ET.SubElement(root, 'Override', PartName='/docProps/core.xml',
                  ContentType='application/vnd.openxmlformats-package.core-properties+xml')
    return root


def _write_docx(path, review, title, cancel):
    document, relations = _review_document(review, title, cancel)
    root_relations = _relationships()
    _relationship(root_relations, 'rId1', 'officeDocument', 'word/document.xml')
    ET.SubElement(root_relations, 'Relationship', Id='rId2', Type=f'{PACKAGE_REL}/metadata/core-properties',
                  Target='docProps/core.xml')
    metadata = ET.Element('cp:coreProperties', {'xmlns:cp': CORE_PROPERTIES,
                                               'xmlns:dc': 'http://purl.org/dc/elements/1.1/'})
    ET.SubElement(metadata, 'dc:title').text = f'{title} 课程复习指南'
    ET.SubElement(metadata, 'dc:language').text = 'zh-CN'
    parts = {
        '[Content_Types].xml': _content_types(),
        '_rels/.rels': root_relations,
        'docProps/core.xml': metadata,
        'word/document.xml': document,
        'word/_rels/document.xml.rels': relations,
        'word/styles.xml': _styles(),
        'word/footer1.xml': _footer(),
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as handle, ZipFile(handle, 'w', ZIP_DEFLATED) as bundle:
        for filename, node in parts.items():
            _check_cancel(cancel)
            bundle.writestr(filename, ET.tostring(node, encoding='utf-8', xml_declaration=True))


def _stop_writer(process):
    for how in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, how)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            continue


def _convert_pdf(executable, docx, pdf, profile, cancel):
    command = [
        executable, f'-env:UserInstallation={profile.as_uri()}', '--headless',
        '--norestore', '--convert-to', 'pdf:writer_pdf_Export', '--outdir', str(pdf.parent), str(docx),
    ]
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as exc:
        raise ValueError('无法启动 LibreOffice Writer，请检查安装。') from exc
    try:
        deadline = time.monotonic() + PDF_TIMEOUT
        while True:
            _check_cancel(cancel)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('PDF 转换超时，请缩小复习范围后重试。')
            try:
                code = process.wait(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if code or not pdf.is_file():
            raise ValueError('LibreOffice 未生成 PDF，请确认 LibreOffice Writer 已安装。')
        with pdf.open('rb') as stream:
            if stream.read(5) != b'%PDF-' or stream.seek(0, os.SEEK_END) < 16:
                raise ValueError('LibreOffice 生成的 PDF 文件无效。')
            stream.seek(-min(stream.tell(), 1024), os.SEEK_END)
            if not stream.read().rstrip().endswith(b'%%EOF'):
                raise ValueError('LibreOffice 生成的 PDF 文件不完整。')
        pdf.chmod(0o600)
    finally:
        if process.poll() is None:
            _stop_writer(process)


def _file_stem(title):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', ' ', title)
    name = ' '.join(name.split()).strip('. ')
    # Limit UTF-8 bytes as well as characters for long names with Chinese text.
    name = name.encode('utf-8')[:108].decode('utf-8', errors='ignore').strip('. ')
    return f'{name or "课程"} 复习指南'


def export_review(review, output_dir, *, cancel=None):
    """Publish both documents together and return their absolute paths."""
    if not isinstance(review, dict):
        raise ValueError('复习文档数据格式无效。')
    _check_cancel(cancel)
    executable = require_export_tools()
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    title = _single_line(review.get('title'))[:200] or '课程'
    stem = _file_stem(title)
    name = f'{stem} {datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:12]}'
    destination = root / name
    with TemporaryDirectory(prefix='.review-', dir=root) as staging:
        scratch = Path(staging)
        bundle = scratch / 'files'
        bundle.mkdir(mode=0o700)
        docx, pdf = bundle / f'{stem}.docx', bundle / f'{stem}.pdf'
        _write_docx(docx, review, title, cancel)
        _check_cancel(cancel)
        _convert_pdf(executable, docx, pdf, scratch / 'profile', cancel)
        _check_cancel(cancel)
        bundle.rename(destination)
    return {'docx': str(destination / docx.name), 'pdf': str(destination / pdf.name)}
