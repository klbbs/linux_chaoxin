"""Collect course content through the signed-in student's read-only web pages."""
from __future__ import annotations

from collections import deque
from contextlib import ExitStack
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from chaoxing_linux.study_worker import InputRequired, checked_url, normalize_course

CHAPTER_PATH = '/mooc2-ans/mycourse/studentcourse'
CARD_PATH = '/mooc-ans/knowledge/cards'
ASSIGNMENT_DETAILS = frozenset({
    '/mooc-ans/work/record-detail', '/mooc-ans/mooc2/work/record-detail',
    '/mooc2/work/record-detail', '/work/record-detail',
    '/mooc-ans/work/selectworkquestion', '/mooc-ans/mooc2/work/selectworkquestion',
    '/mooc2/work/selectworkquestion', '/work/selectworkquestion',
    '/mooc-ans/mooc2/work/view', '/mooc2/work/view',
})
ASSIGNMENT_ENTRY = '/mooc-ans/mooc2/work/task'
SOURCE_QUERY = frozenset({'courseid', 'clazzid', 'classid', 'knowledgeid', 'chapterid', 'workid', 'dataid'})
DOCUMENT_EXTENSIONS = frozenset({'.txt', '.md', '.csv', '.pdf', '.docx', '.pptx', '.odt', '.doc', '.ppt'})
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_CHAPTERS = 180
MAX_ASSIGNMENTS = 120
MAX_MATERIALS = 100
MAX_TEXT = 16_000
MAX_COURSE_TEXT = 500_000
MAX_REQUESTS = 300
CARD_NOTICE = re.compile(r'此内容由\s*AI\s*生成[，,]?\s*仅供参考[。.]?', re.I)
CARD_CONTENT = '#pageDiv.ans-cc, #content, #contentArea, .chapter-content, .mainContent, .content, main'
EMPTY_CARD_LINE = re.compile(r'^(?:\[原页图片\]\s*)*(?:暂无内容[。.]?)?$')


def _soup(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, 'lxml')


def _safe_url(base, candidate):
    try:
        return checked_url(urljoin(base, str(candidate))) if candidate else ''
    except ValueError:
        return ''


def _path(url):
    return urlsplit(url).path.rstrip('/').lower()


def _params(url):
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def _query(url, updates):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({**_params(url), **updates}), ''))


def source_url(url):
    """Export only the ordinary educational identifiers, never URL signatures."""
    if not url:
        return ''
    parts = urlsplit(url)
    keep = {key: val for key, val in _params(url).items()
            if key.lower() in SOURCE_QUERY and re.fullmatch(r'[0-9]{1,24}', val)}
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(keep), ''))


def _clean(text):
    lines = []
    for line in str(text or '').splitlines():
        line = ' '.join(line.split())
        if line:
            lines.append(line)
    return '\n'.join(lines)


def _excerpt(text):
    clean = _clean(text)
    return clean if len(clean) <= MAX_TEXT else clean[:MAX_TEXT] + '\n（正文节选）'


def _material_title_key(title):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', title)).casefold()


def _visible(node):
    for child in reversed(node.select('script, style, template, noscript, [hidden], [aria-hidden="true"], input')):
        child.decompose()
    for image in node.find_all('img'):
        image.replace_with('[' + (str(image.get('alt') or '').strip() or '原页图片') + ']')
    for split in node.find_all('br'):
        split.replace_with('\n')
    for block in node.select('p, div, li, h1, h2, h3, h4, tr, pre'):
        block.append('\n')
    for cell in node.select('td, th'):
        cell.append('  ')
    return _clean(node.get_text())


class _Reader:
    def __init__(self, session):
        self.session = session
        self.requests = 0
        self.deadline = time.monotonic() + 240

    def read(self, url, allowed, *, limit=MAX_HTML_BYTES, referer=None, redirect_allowed=None, allow_file_cdn=False):
        initial = url
        on_cdn = False
        for hop in range(5):
            check = redirect_allowed if hop and redirect_allowed is not None else allowed
            is_cdn = allow_file_cdn and bool(hop) and _file_cdn(url)
            if not is_cdn and (on_cdn or not _safe_url(url, url) or not check(url)):
                raise InputRequired('页面跳转超出复习资料允许的读取范围')
            on_cdn = on_cdn or is_cdn
            remaining = self.deadline - time.monotonic()
            if self.requests >= MAX_REQUESTS or remaining <= 0:
                raise InputRequired('本次复习资料读取已达到请求或时间上限')
            self.requests += 1
            kwargs = {'stream': True, 'allow_redirects': False, 'timeout': min(10, remaining)}
            if referer and not is_cdn:
                kwargs['headers'] = {'Referer': referer}
            with ExitStack() as stack:
                session = self.session
                if is_cdn:
                    # The signed download requires the same origin-only referer
                    # a browser sends on this cross-origin hop. Never forward the
                    # full course URL, session cookies, or ambient credentials.
                    from requests import Session
                    session = stack.enter_context(Session())
                    session.trust_env = False
                    kwargs['headers'] = {'Referer': f'https://{urlsplit(initial).hostname}/'}
                response = stack.enter_context(session.get(url, **kwargs))
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location')
                    if not location:
                        raise InputRequired('学习通页面的跳转地址缺失')
                    url = urljoin(url, location)
                    if _safe_url(url, url) and (urlsplit(url).hostname or '').startswith('passport'):
                        raise InputRequired('登录已失效，请重新同步学习通')
                    continue
                if response.status_code != 200:
                    raise InputRequired(f'学习通返回 HTTP {response.status_code}')
                data = bytearray()
                for chunk in response.iter_content(32 * 1024):
                    if time.monotonic() >= self.deadline or len(data) + len(chunk) > limit:
                        raise InputRequired('页面或文件超出读取上限')
                    data.extend(chunk)
                return bytes(data), response.url or url, response.headers
        raise InputRequired(f'学习通页面跳转过多（{_path(initial)}）')

    def page(self, url, allowed, *, referer=None, redirect_allowed=None):
        data, actual_url, _ = self.read(url, allowed, referer=referer, redirect_allowed=redirect_allowed)
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('gb18030', errors='replace')
        soup = _soup(text)
        if soup.select_one('input[name=password], input#password, input[name=uname]'):
            raise InputRequired('登录已失效，请重新同步学习通')
        if soup.select_one('input[name*=captcha], input[id*=captcha], input[name*=validatecode]'):
            raise InputRequired('学习通需要验证码，请先在官方页面验证')
        return soup, actual_url


def _chapter_url(course):
    return _query('https://mooc2-ans.chaoxing.com' + CHAPTER_PATH, {
        'courseid': course['courseId'], 'clazzid': course['clazzId'], 'cpi': course['cpi'], 'ut': 's'})


def parse_chapter_outline(soup):
    points, seen = [], set()
    for node in soup.select('.chapter_unit [id^=cur]'):
        match = re.fullmatch(r'cur(\d{1,24})', str(node.get('id', '')))
        anchor = node.select_one('a.clicktitle')
        if not match or not anchor:
            continue
        chapter_id = match[1]
        title = _clean(anchor.get_text(' ', strip=True))
        if chapter_id in seen or not title:
            continue
        hint = node.select_one('.bntHoverTips')
        lock_text = hint.get_text(' ', strip=True) if hint else ''
        points.append((chapter_id, title, any(word in lock_text for word in ('解锁', '未开放'))))
        seen.add(chapter_id)
    return points


def _card_payload(soup):
    for script in soup.find_all('script'):
        text = script.string or ''
        match = re.search(r'\bmArg\s*=\s*(?=\{)', text)
        if match:
            try:
                value, _ = json.JSONDecoder().raw_decode(text[match.end():])
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return {}


def _extension(name, url):
    title_suffix = PurePosixPath(name).suffix.lower()
    url_suffix = PurePosixPath(unquote(urlsplit(url).path)).suffix.lower()
    return title_suffix if title_suffix in DOCUMENT_EXTENSIONS else url_suffix


def _material_kind(title, url):
    extension = _extension(title, url)
    return extension.removeprefix('.').upper() if extension in DOCUMENT_EXTENSIONS else '资料'


def _card_sources(soup, page_url, payload):
    for item in payload.get('attachments') or []:
        if not isinstance(item, dict):
            continue
        properties = item.get('property') if isinstance(item.get('property'), dict) else {}
        title = _clean(properties.get('name') or properties.get('title') or properties.get('filename')
                       or item.get('name') or item.get('title'))
        kind = _clean(item.get('type') or properties.get('type') or '资料')
        url = _safe_url(page_url, properties.get('downloadUrl') or item.get('downloadUrl')
                        or properties.get('url') or properties.get('resourceUrl') or item.get('url'))
        if title:
            yield title, url, _material_kind(title, url) if kind == 'document' else kind
    container = soup.select_one('#pageDiv.ans-cc') or soup.select_one(CARD_CONTENT)
    if container:
        for anchor in container.select('a[download], a[href], a[data-url]'):
            title = _clean(anchor.get('download') or anchor.get_text(' ', strip=True) or anchor.get('title'))
            url = _safe_url(page_url, anchor.get('href') or anchor.get('data-url'))
            if title and url and (_extension(title, url) in DOCUMENT_EXTENSIONS or _material_link(url)):
                yield title, url, _material_kind(title, url)


def _material_link(url):
    path = _path(url)
    return re.fullmatch(r'/(?:mooc-ans/|mooc2-ans/)?(?:coursedata|resourcedata)(?:/[\w.-]+){0,5}', path) is not None


def _download_path(url):
    path = _path(url)
    return (bool(re.fullmatch(r'/(?:mooc-ans/|mooc2-ans/)?(?:coursedata|resourcedata)/(?:download|downloaddata|downloadresource|downloadfile)', path))
            or (path.startswith(('/ananas/', '/download/', '/view/', '/files/'))
                and PurePosixPath(path).suffix in DOCUMENT_EXTENSIONS)
            or (urlsplit(url).hostname or '').endswith('.ananas.chaoxing.com')
            and path.startswith(('/download/', '/view/', '/files/')))


def _file_cdn(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.hostname == 'd0.cldisk.com'
                and parts.username is None and parts.password is None and parts.port in (None, 443)
                and parts.path.startswith('/download/'))
    except ValueError:
        return False


def _office_text(data, extension):
    def is_body(name):
        if extension == '.docx':
            return name == 'word/document.xml'
        if extension == '.odt':
            return name == 'content.xml'
        return re.fullmatch(r'ppt/slides/slide\d{1,5}\.xml', name) is not None

    with ZipFile(BytesIO(data)) as archive:
        entries = [file for file in archive.infolist() if is_body(file.filename)]
        entries.sort(key=lambda file: int(re.search(r'(\d+)\.xml$', file.filename)[1])
                     if extension == '.pptx' else 0)
        if sum(file.file_size for file in entries[:100]) > MAX_FILE_BYTES:
            raise ValueError('文档正文超出读取上限')
        paragraphs = []
        for file in entries[:100]:
            xml = archive.read(file)
            if re.search(br'<!\s*(DOCTYPE|ENTITY)', xml.replace(b'\x00', b''), re.I):
                raise ValueError('不支持该文档正文格式')
            root = ElementTree.fromstring(xml)
            for node in root.iter():
                if node.tag.rsplit('}', 1)[-1] in ('p', 'h'):
                    value = (''.join(node.itertext()) if extension == '.odt' else
                             ''.join(item.text or '' for item in node.iter() if item.tag.rsplit('}', 1)[-1] == 't'))
                    if value.strip():
                        paragraphs.append(value)
        return '\n'.join(paragraphs)


def _document_text(data, extension):
    if extension == '.pdf':
        tool = shutil.which('pdftotext')
        if not tool:
            raise ValueError('缺少 PDF 文本读取工具 pdftotext')
        if not data.startswith(b'%PDF-'):
            raise ValueError('服务器未返回 PDF 文件')
        result = subprocess.run([tool, '-f', '1', '-l', '40', '-layout', '-enc', 'UTF-8', '-', '-'],
                                input=data, capture_output=True, timeout=12, check=True)
        return result.stdout.decode('utf-8', errors='replace')
    if data.startswith(b'PK\x03\x04') and extension in ('.doc', '.ppt'):
        extension = extension + 'x'
    if extension in ('.docx', '.pptx', '.odt'):
        return _office_text(data, extension)
    if extension in ('.doc', '.ppt'):
        if not data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
            raise ValueError('服务器未返回预期的 Office 文件')
        command = 'antiword' if extension == '.doc' else 'catppt'
        tool = shutil.which(command)
        if not tool:
            return _legacy_office_text(data, extension)
        with tempfile.TemporaryDirectory(prefix='chaoxing-review-') as folder:
            path = Path(folder) / ('document' + extension)
            path.write_bytes(data)
            result = subprocess.run([tool, *(['-m', 'UTF-8.txt'] if extension == '.doc' else []), str(path)],
                                    cwd=folder, capture_output=True, timeout=12, check=True)
            return result.stdout.decode('utf-8', errors='replace')
    if extension in ('.txt', '.md', '.csv'):
        if re.match(br'\s*(?:<!doctype\s+html|<html\b|<script\b)', data, re.I) or b'\x00' in data:
            raise ValueError('服务器未返回文本文件')
        try:
            return data.decode('utf-8-sig')
        except UnicodeDecodeError:
            return data.decode('gb18030')
    return ''


def _legacy_office_text(data, extension):
    """LibreOffice fallback with no network access and no user's files mounted."""
    bubblewrap, office = shutil.which('bwrap'), shutil.which('libreoffice')
    if not bubblewrap or not office:
        raise ValueError('缺少旧版 Office 的隔离文本转换工具')
    with tempfile.TemporaryDirectory(prefix='chaoxing-review-') as folder:
        root = Path(folder)
        profile = root / 'profile/user'
        profile.mkdir(parents=True)
        (profile / 'registrymodifications.xcu').write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '</item></oor:items>', encoding='utf-8')
        input_name = 'sample' + extension
        (root / input_name).write_bytes(data)
        output_format = 'txt:Text (encoded):UTF8' if extension == '.doc' else 'pptx:Impress MS PowerPoint 2007 XML'
        args = [bubblewrap, '--unshare-all', '--die-with-parent', '--new-session',
                '--ro-bind', '/usr', '/usr', '--ro-bind', '/etc', '/etc', '--proc', '/proc', '--dev', '/dev',
                '--tmpfs', '/tmp', '--symlink', 'usr/bin', '/bin',
                '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
                '--bind', str(root), '/work', '--chdir', '/work', '--clearenv',
                '--setenv', 'PATH', '/usr/bin', '--setenv', 'HOME', '/tmp', '--setenv', 'LANG', 'C.UTF-8',
                '--', office, '-env:UserInstallation=file:///work/profile', '--headless',
                '--convert-to', output_format, '--outdir', '/work', '/work/' + input_name]
        subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True)
        suffix = '.txt' if extension == '.doc' else '.pptx'
        output = root / ('sample' + suffix)
        file_no = os.open(output, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(file_no, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
                raise ValueError('未生成可用的旧版 Office 文件正文')
            content = handle.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError('旧版 Office 正文超出读取上限')
        return _document_text(content, suffix)


def _question_attachments(node, base):
    seen = set()
    for tag in node.select('.editor-iframe iframe, iframe.attach-iframe, a[download]'):
        name = _clean(tag.get('filename') or tag.get('fileName') or tag.get('name')
                      or tag.get('title') or tag.get('download') or tag.get_text(' ', strip=True))
        title = name[:240] or '原页嵌入附件'
        url = _safe_url(base, tag.get('downloadurl') or tag.get('href') or tag.get('src'))
        if not url or not _download_path(url):
            url = ''
        if (title, url) in seen:
            continue
        seen.add((title, url))
        kind = _material_kind(title, url)
        file_type = str(tag.get('filetype') or '').lower()
        if kind == '资料' and '.' + file_type in DOCUMENT_EXTENSIONS:
            kind = file_type.upper()
        yield title, url, kind


def _parse_questions(soup, base=''):
    questions = []
    used_text = 0
    for node in soup.select('.singleQuesId, .questionLi, .question-item'):
        if node.find_parent(class_=['singleQuesId', 'questionLi', 'question-item']):
            continue
        title = (node.select_one('.mark_name .qtContent')
                 or node.select_one('.Zy_TItle, .newZy_TItle, .mark_name, .question-title'))
        if title is None:
            continue
        if 'newZy_TItle' in (title.get('class') or []) and title.parent:
            title = title.parent
        attachments = [{'title': filename, 'url': source_url(url), 'kind': kind, '_fetch_url': url}
                       for filename, url, kind in list(_question_attachments(node, base))[:20]]
        options = [{'key': match[1] if match else chr(65 + index),
                    'text': (match[2] if match else value)[:1600]}
                   for index, option in enumerate(node.select('.mark_letter li, ul > li, .question-option')[:32])
                   if (value := _visible(option))
                   for match in [re.match(r'^([A-Z])[.、．：:\s]+(.*)$', value, re.S)]]
        title_text = _visible(title)[:4000]
        if not title_text and attachments:
            title_text = '原题见嵌入附件'
        if title_text:
            question = {'text': title_text, 'options': options}
            if attachments:
                question['attachments'] = attachments
            for selector, field in (('.correct-answer, .correctAnswer, .standard-answer, .rightAnswerContent', 'correct_answer'),
                                    ('.question-analysis, .answer-analysis, .analysisCon', 'analysis')):
                shown = node.select_one(selector)
                if shown:
                    question[field] = _visible(shown)[:4000]
            for marker in node.find_all(string=re.compile(r'^\s*正确答案[:：]\s*$')):
                if 'correct_answer' not in question and marker.parent:
                    answer = marker.parent.find_next_sibling()
                    if answer and ('answerCon' in (answer.get('class') or []) or answer.name == 'span'):
                        question['correct_answer'] = _visible(answer)[:4000]
            questions.append(question)
            used_text += (len(title_text) + sum(len(option['text']) for option in options)
                          + len(question.get('correct_answer', '')) + len(question.get('analysis', '')))
        if len(questions) >= 100 or used_text >= MAX_TEXT:
            break
    return questions


def _printable_questions(questions):
    sections = []
    for index, question in enumerate(questions, 1):
        lines = [f'{index}. {question["text"]}']
        lines.extend(f'{option["key"]}. {option["text"]}' for option in question['options'])
        if question.get('correct_answer'):
            lines.append(f'正确答案：{question["correct_answer"]}')
        if question.get('analysis'):
            lines.append(f'解析：{question["analysis"]}')
        lines.extend(f'原题附件：{item["title"]}' for item in question.get('attachments', []))
        sections.append('\n'.join(lines))
    return _excerpt('\n'.join(sections))


def _course_scope(url, course):
    expected = {'courseid': course['courseId'], 'classid': course['clazzId'], 'clazzid': course['clazzId']}
    return _identifiers_match(url, expected)


def _identifiers_match(url, expected):
    return all(key.lower() not in expected or expected[key.lower()] == val
               for key, val in parse_qsl(urlsplit(url).query, keep_blank_values=True))


def _named_input(soup, name):
    field = soup.select_one(f'input[id="{name}"], input[name="{name}"]')
    return str(field.get('value', '')) if field else ''


def _materials_navigation(soup, base, course):
    anchor = soup.select_one('[dataname=zl] a[data-url], [dataname=zl] a[href]')
    if not anchor:
        return ''
    url = _safe_url(base, anchor.get('data-url') or anchor.get('href'))
    enc = _named_input(soup, 'enc')
    if (not url or _path(url) not in ('/mooc2-ans/coursedata/stu-datalist', '/coursedata/stu-datalist')
            or not _course_scope(url, course)):
        raise InputRequired('课程资料入口格式无法识别')
    if not enc:
        raise InputRequired('课程页没有提供资料导航凭证')
    course_id = _named_input(soup, 'courseid')
    class_id = _named_input(soup, 'clazzid')
    if course_id != course['courseId'] or class_id != course['clazzId']:
        raise InputRequired('课程资料导航与当前选择不一致')
    params = {'courseid': course_id, 'clazzid': class_id,
              'cpi': _named_input(soup, 'cpi') or course['cpi'], 'ut': 's',
              't': _named_input(soup, 't') or str(int(time.time() * 1000)), 'stuenc': enc}
    microtopic = _named_input(soup, 'microTopicId')
    if re.fullmatch(r'[1-9][0-9]{0,19}', microtopic):
        params['microTopicId'] = microtopic
    return _query(url, params)


def _read_material_rows(soup, page_url):
    materials = []
    for row in soup.select('ul[dataname][type][id], .resource-list .resource-item, .material-list .material-item'):
        title_node = row.select_one('.dataBody_name_stu .rename_title, .rename_title, .resource-title, .material-title')
        title = _clean(title_node.get_text(' ', strip=True) if title_node else row.get('dataname'))
        if not title:
            continue
        if row.get('type') == 'afolder':
            continue
        download_node = row.select_one('a[href*="/coursedata/downloadData" i], a[download][href]')
        downloadable = str(row.get('isdown') or '').lower() not in ('0', 'false')
        url = _safe_url(page_url, download_node.get('href')) if download_node and downloadable else ''
        description = row.select_one('.description, .resource-desc, .material-desc')
        text = _excerpt(_visible(description)) if description else ''
        kind = _material_kind(title, url)
        file_type = str(row.get('type') or '').lower()
        if kind == '资料' and ('.' + file_type) in DOCUMENT_EXTENSIONS:
            kind = file_type.upper()
        materials.append({'title': title, 'text': text, 'url': url, 'kind': kind, 'id': str(row.get('id') or '')})
    return materials


def _material_cursor(soup, course):
    fields = {key: _named_input(soup, key) for key in (
        'courseid', 'clazzid', 'cpi', 'ut', 't', 'microTopicId', 'enc', 'dataName', 'dataId', 'type', 'flag', 'parent', 'order')}
    if fields['courseid'] != course['courseId'] or fields['clazzid'] != course['clazzId'] or not fields['enc']:
        return {}
    return fields


def _material_children(soup, start, course, ancestors):
    """Follow the official folder and pagination calls declared by this list."""
    cursor = _material_cursor(soup, course)
    if not cursor:
        return [], any(row.get('type') == 'afolder' for row in soup.select('ul[dataname][type][id]'))
    next_items = []
    parts = urlsplit(start)
    endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    parent = ','.join(ancestors)
    marked_parents = [node.get('value') or node.get_text(strip=True) for node in soup.select('.subtitleId')]
    if marked_parents and all(re.fullmatch(r'\d{1,24}', str(value)) for value in marked_parents):
        parent = ','.join(marked_parents)
    for row in soup.select('ul[dataname][type=afolder][id]'):
        folder_id = str(row['id'])
        if not re.fullmatch(r'\d{1,24}', folder_id) or folder_id in ancestors or len(ancestors) >= 6:
            continue
        name = unquote(str(row['dataname']))[:240]
        params = {**cursor, 'dataName': name, 'dataId': folder_id, 'type': '1', 'parent': parent,
                  'flag': '0', 'isAjax': 'isAjax'}
        next_items.append((_query(endpoint, params), ancestors + (folder_id,), name))
    page_no, total = _named_input(soup, 'currentPageNo'), _named_input(soup, 'totalPages') or _named_input(soup, 'totalPage')
    if not page_no:
        page_no = _named_input(soup, 'pages') or '1'
    if re.fullmatch(r'\d{1,6}', page_no) and re.fullmatch(r'\d{1,6}', total) and int(page_no) < int(total):
        params = {**cursor, 'pages': str(int(page_no) + 1), 'isAjax': 'isAjax'}
        next_items.append((_query(endpoint, params), ancestors, ''))
    return next_items, False


class CourseCollector:
    def __init__(self, worker, course):
        self.worker = worker
        self.course = course
        self.reader = _Reader(worker.session)
        self.chapters = []
        self.assignments = []
        self.materials = []
        self.material_keys = set()
        self.issues = []
        self.chapter_source = _chapter_url(course)
        self.used_text = 0

    def _event(self, message, progress):
        self.worker.emit({'message': message, 'progress': progress})

    def _issue(self, subject, error):
        # Connection errors may contain signed URLs. Only local exception messages
        # authored by this module or the existing Worker are safe to pass onward.
        reason = str(error) if isinstance(error, InputRequired) else '读取或解析失败'
        detail = f'{subject}：{reason}'
        if detail not in self.issues and len(self.issues) < 100:
            self.issues.append(detail)

    def _text(self, value):
        text = _excerpt(value)
        remaining = max(0, MAX_COURSE_TEXT - self.used_text)
        if len(text) > remaining:
            self._issue('课程正文', InputRequired('达到本次导出的正文长度上限'))
        result = text[:remaining]
        self.used_text += len(result)
        return result

    def _safe_course(self, url):
        return _course_scope(url, self.course)

    def _add_material(self, title, text, url, kind, referer, *, identity='', folder=''):
        if len(self.materials) >= MAX_MATERIALS:
            self._issue('课程资料', InputRequired('达到本次资料数量上限'))
            return
        if not _safe_url(referer, url):
            url = ''
        if not (identity or folder or url or text):
            # An index-only copy of a listed file adds no source or content.
            name = _material_title_key(title)
            if any(_material_title_key(item['title']) == name for item in self.materials):
                return
        key = ('id', identity) if identity else (title, urlsplit(url).netloc, _path(url), folder)
        if key in self.material_keys:
            return
        self.material_keys.add(key)
        item = {'title': title[:240], 'text': text, 'kind': kind, 'url': source_url(url or referer)}
        if folder:
            item['folder'] = folder
        extension = _extension(title, url)
        if extension not in DOCUMENT_EXTENSIONS and '.' + kind.lower() in DOCUMENT_EXTENSIONS:
            extension = '.' + kind.lower()
        if extension in DOCUMENT_EXTENSIONS and url and _download_path(url) and self._safe_course(url):
            try:
                data, _, headers = self.reader.read(
                    url, lambda candidate: _download_path(candidate) and self._safe_course(candidate),
                    limit=MAX_FILE_BYTES, referer=referer, allow_file_cdn=True)
                if 'text/html' in headers.get('Content-Type', '').lower():
                    raise InputRequired('下载链接返回了网页，未取得文件正文')
                body = _document_text(data, extension)
                if not body.strip():
                    raise InputRequired('文件没有可提取的文本内容')
                item['text'] = '\n'.join(filter(None, (text, body)))
            except (InputRequired, OSError, ValueError, BadZipFile, ElementTree.ParseError, subprocess.SubprocessError):
                self._issue(f'资料「{title[:100]}」', InputRequired('文件正文未能读取，已保留资料名称'))
            except Exception as exc:
                self._issue(f'资料「{title[:100]}」', exc)
        elif not text:
            if extension in DOCUMENT_EXTENSIONS:
                self._issue(f'资料「{title[:100]}」', InputRequired('没有可读取的下载链接，已保留资料名称'))
            elif kind not in ('folder', '目录'):
                self._issue(f'资料「{title[:100]}」', InputRequired('仅收录资料名称与可用入口'))
        item['text'] = self._text(item['text'])
        self.materials.append(item)

    def chapter_outline(self):
        soup, _ = self.reader.page(self.chapter_source,
                                   lambda url: _path(url) == CHAPTER_PATH and self._safe_course(url))
        points = parse_chapter_outline(soup)
        if not points and not any(marker in soup.get_text(' ', strip=True) for marker in ('暂无章节', '没有章节')):
            self._issue('课程章节', InputRequired('没有识别到章节目录'))
        if len(points) > MAX_CHAPTERS:
            self._issue('课程章节', InputRequired('达到本次章节数量上限'))
        for chapter_id, title, locked in points[:MAX_CHAPTERS]:
            self.chapters.append({'id': chapter_id, 'title': title[:240], 'text': '',
                                  'status': 'locked' if locked else '', 'url': '',
                                  'attachments': []})

    def chapter_content(self, chapter):
        if chapter['status'] == 'locked':
            self._issue(f'章节「{chapter["title"]}」', InputRequired('章节尚未解锁'))
            return
        sections, seen, attachments = [], set(), []
        card_base = _query('https://mooc1.chaoxing.com' + CARD_PATH, {
            'clazzid': self.course['clazzId'], 'courseid': self.course['courseId'],
            'knowledgeid': chapter['id'], 'ut': 's', 'cpi': self.course['cpi'], 'mooc2': '1'})
        chapter['url'] = source_url(card_base)
        for card_no in range(7):
            url = _query(card_base, {'num': str(card_no)})
            soup, actual = self.reader.page(url, lambda candidate: _path(candidate) == CARD_PATH
                                            and self._safe_course(candidate)
                                            and _identifiers_match(candidate, {'knowledgeid': chapter['id']}))
            payload = _card_payload(soup)
            container = soup.select_one('#pageDiv.ans-cc') or soup.select_one(CARD_CONTENT)
            entries = list(_card_sources(soup, actual, payload))
            body = _visible(container) if container is not None else ''
            if container is None and soup.body:
                # Old cards sometimes put editorial paragraphs directly in body.
                blocks = soup.body.find_all(('p', 'h1', 'h2', 'h3', 'h4', 'pre', 'table'), recursive=False)
                body = '\n'.join(filter(None, (_visible(block) for block in blocks)))
            body = '\n'.join(line for line in _clean(CARD_NOTICE.sub('', body)).splitlines()
                             if not EMPTY_CARD_LINE.fullmatch(line))
            if re.fullmatch(r'\w', body, re.ASCII):
                body = ''
            if '章节未开放' in body or ('章节未开放' in soup.get_text(' ', strip=True) and len(sections) == 0):
                self._issue(f'章节「{chapter["title"]}」', InputRequired('章节尚未开放'))
                break
            fingerprint = (body, tuple(entries))
            if fingerprint in seen or not (body or entries):
                break
            seen.add(fingerprint)
            if body and body not in sections:
                sections.append(body)
            attachments.extend(entry for entry in entries if entry[0] not in {existing[0] for existing in attachments})
            declared_count = (payload.get('defaults') or {}).get('cardCount') if isinstance(payload.get('defaults'), dict) else None
            if str(declared_count).isdigit() and 1 <= int(declared_count) <= card_no + 1:
                break
        chapter['text'] = self._text('\n'.join(sections))
        chapter['attachments'] = [{'title': title[:240], 'url': source_url(url), 'kind': kind}
                                  for title, url, kind in attachments]
        for title, url, kind in attachments:
            self._add_material(title, '', url, kind, referer=chapter['url'])
        if not chapter['text'] and attachments:
            linked = []
            for title, _, _ in attachments:
                name = _material_title_key(title)
                texts = {item['text'] for item in self.materials if item['text']
                         and _material_title_key(item['title']) == name}
                if len(texts) == 1:
                    linked.append(next(iter(texts)))
            if linked:
                chapter['text'] = self._text('\n'.join(dict.fromkeys(linked)))
                if chapter['text']:
                    chapter['note'] = '以下要点摘自本章节所附的可读资料。'
        if not chapter['text']:
            self._issue(f'章节「{chapter["title"]}」', InputRequired('没有可提取的章节文字'))

    def course_assignments(self):
        listing = self.worker.list_work({'course': self.course})
        if listing.get('status') == 'partial':
            self._issue('课程作业', InputRequired('作业列表还有后续页未读取'))
        works = listing.get('works') or []
        if len(works) > MAX_ASSIGNMENTS:
            self._issue('课程作业', InputRequired('达到本次作业数量上限'))
        for item in works[:MAX_ASSIGNMENTS]:
            title = _clean(item.get('title') or '未命名作业')[:240]
            url = _safe_url(self.chapter_source, item.get('url'))
            params = {key.lower(): val for key, val in _params(url).items()}
            identity = str(item.get('id') or '')
            answer_id = params.get('answerid') or params.get('workanswerid') or ''
            known_answer = re.fullmatch(r'[1-9][0-9]{0,23}', answer_id) is not None
            expected = {'workid': identity, 'workrelationid': identity}
            if known_answer:
                expected.update({'answerid': answer_id, 'workanswerid': answer_id})
            read_url = url if url and _path(url) in ASSIGNMENT_DETAILS and self._safe_course(url) else ''
            if (url and _path(url) == ASSIGNMENT_ENTRY and self._safe_course(url) and item.get('status') == 'submitted'
                    and known_answer and (params.get('workid') or params.get('workrelationid')) == identity
                    and params.get('courseid') == self.course['courseId']
                    and (params.get('classid') or params.get('clazzid')) == self.course['clazzId']
                    and _identifiers_match(url, expected)):
                read_url = url
            entry = {'title': title, 'url': source_url(read_url), 'status': item.get('status_text') or item.get('status') or '',
                     'text': '', 'questions': []}
            self.assignments.append(entry)
            if not read_url or item.get('status') != 'submitted':
                self._issue(f'作业「{title}」', InputRequired('没有可用的已提交作业只读详情，已保留标题与状态'))
                continue
            try:
                def detail_allowed(candidate):
                    return (_path(candidate) in ASSIGNMENT_DETAILS and self._safe_course(candidate)
                            and _identifiers_match(candidate, expected))

                initial_task = _path(read_url) == ASSIGNMENT_ENTRY
                soup, actual = self.reader.page(read_url, lambda candidate: (
                    initial_task and candidate == read_url or detail_allowed(candidate)),
                                                redirect_allowed=detail_allowed)
                if not detail_allowed(actual):
                    raise InputRequired('没有取得已提交作业只读页面')
                entry['url'] = source_url(actual)
                if soup.select_one('style#cxSecretStyle'):
                    raise InputRequired('作业正文需要额外的字体解析，已保留标题与状态')
                questions = _parse_questions(soup, actual)
                if not questions:
                    raise InputRequired('没有识别到已提交作业的题目内容')
                missing = 0
                for question in questions:
                    for attachment in question.get('attachments', []):
                        url = attachment.pop('_fetch_url')
                        if url and self._safe_course(url):
                            self._add_material(attachment['title'], '', url, attachment['kind'], referer=actual,
                                               folder=f'作业附件 / {title}')
                        else:
                            missing += 1
                if missing:
                    self._issue(f'作业「{title}」', InputRequired(f'{missing} 项嵌入附件保留名称，原页未提供可用下载链接'))
                entry['text'] = self._text(_printable_questions(questions))
                entry['questions'] = questions if entry['text'] else []
            except Exception as exc:
                self._issue(f'作业「{title}」', exc)

    def course_materials(self):
        entry = self.worker.course_entry_url(self.course)
        entry_allowed = lambda url: _path(url).endswith('/visit/stucoursemiddle') and self._safe_course(url)
        next_allowed = lambda url: (_path(url) == '/mooc2-ans/mycourse/stu' or
                                    _path(url).endswith('/visit/stucoursemiddle')) and self._safe_course(url)
        soup, base = self.reader.page(entry, entry_allowed, redirect_allowed=next_allowed)
        start = _materials_navigation(soup, base, self.course)
        if not start:
            self._issue('课程资料', InputRequired('当前课程没有显示资料入口'))
            return
        allowed = lambda url: _path(url) == _path(start) and self._safe_course(url)
        queue, visited = deque([(start, (), '')]), set()
        while queue and len(visited) < 30:
            url, ancestors, folder_path = queue.popleft()
            params = _params(url)
            key = (params.get('dataId') or '', params.get('pages') or '1')
            if key in visited:
                continue
            visited.add(key)
            try:
                soup, actual = self.reader.page(url, allowed, referer=base)
            except Exception as exc:
                self._issue(f'资料目录「{folder_path}」' if folder_path else '课程资料', exc)
                continue
            rows = _read_material_rows(soup, actual)
            if not rows and len(visited) == 1 and not soup.select_one('#dataBody, .dataBody, .resource-list, .material-list'):
                if not any(marker in soup.get_text(' ', strip=True) for marker in ('暂无资料', '没有资料', '暂无文件')):
                    self._issue('课程资料', InputRequired('未识别到资料列表结构'))
            for item in rows:
                self._add_material(item['title'], item['text'], item['url'], item['kind'], referer=actual,
                                   identity=item['id'], folder=folder_path)
            children, missing_cursor = _material_children(soup, start, self.course, ancestors)
            if missing_cursor:
                self._issue('课程资料', InputRequired('缺少子目录导航信息'))
            for child_url, ids, name in children:
                child_path = ' / '.join(filter(None, (folder_path, name)))
                queue.append((child_url, ids, child_path))
            for anchor in soup.select('a[rel=next][href], .pagination a[href], .pageDiv a[href]'):
                next_url = _safe_url(actual, anchor['href'])
                if next_url and allowed(next_url):
                    queue.append((next_url, ancestors, folder_path))
        if queue:
            self._issue('课程资料', InputRequired('资料列表还有后续页未读取'))

    def collect(self):
        self._event('读取课程目录', 0.03)
        try:
            self.chapter_outline()
        except Exception as exc:
            self._issue('课程章节', exc)
        self._event('读取课程作业', 0.08)
        try:
            self.course_assignments()
        except Exception as exc:
            self._issue('课程作业', exc)
        self._event('读取课程资料', 0.16)
        try:
            self.course_materials()
        except Exception as exc:
            self._issue('课程资料', exc)
        for index, chapter in enumerate(self.chapters):
            if self.reader.requests >= MAX_REQUESTS or time.monotonic() >= self.reader.deadline:
                self._issue('课程章节', InputRequired('达到本次读取上限，剩余章节已保留目录'))
                break
            self._event(f'读取章节内容 {index + 1}/{len(self.chapters)}', 0.22 + index / len(self.chapters) * 0.75)
            try:
                self.chapter_content(chapter)
            except Exception as exc:
                self._issue(f'章节「{chapter["title"]}」', exc)
        self._event('课程复习资料读取完成', 1)
        status = 'partial' if self.issues else 'success'
        if not (self.chapters or self.assignments or self.materials) and self.issues:
            status = 'needs_input'
        title = _clean(self.course.get('title') or self.course.get('name'))[:240]
        return {'ok': status != 'needs_input', 'status': status, 'course': {'title': title},
                'chapters': self.chapters, 'assignments': self.assignments,
                'materials': self.materials, 'issues': self.issues,
                'message': (f'已读取 {len(self.chapters)} 个章节、{len(self.assignments)} 份作业、'
                            f'{len(self.materials)} 项资料' if status != 'needs_input' else '未读取到可用的课程复习资料')}


def collect_course(worker, payload):
    course = normalize_course(payload.get('course') or payload)
    return CourseCollector(worker, course).collect()
