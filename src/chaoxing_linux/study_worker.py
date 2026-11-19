"""Private worker; imports third-party code only after choosing its private data directory."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


class InputRequired(ValueError):
    pass


def process_identity(pid: int) -> str | None:
    try:
        return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def start_parent_guard(pid: int, identity: str | None, interval: float = 0.5):
    """Never continue a course after its owning desktop process has exited."""
    def guard():
        while True:
            if not identity or process_identity(pid) != identity or os.getppid() != pid:
                os._exit(74)
            time.sleep(interval)
    threading.Thread(target=guard, daemon=True, name='desktop-owner').start()


def checked_url(value: str) -> str:
    parsed = urlsplit(str(value))
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or not (host == 'chaoxing.com' or host.endswith('.chaoxing.com'))
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('仅支持学习通 HTTPS 作业链接')
    return value


def normalize_course(course: dict) -> dict:
    result = dict(course)
    result['courseId'] = str(course.get('courseId') or course.get('courseid') or '')
    result['clazzId'] = str(course.get('clazzId') or course.get('clazzid') or course.get('classId') or '')
    result['cpi'] = str(course.get('cpi', ''))
    if not result['courseId'] or not result['clazzId']:
        raise ValueError('请选择有效课程')
    result.update(courseid=result['courseId'], clazzid=result['clazzId'],
                  id=str(course.get('id') or result['courseId'] + ':' + result['clazzId']),
                  name=course.get('name') or course.get('title') or result['courseId'])
    return result


def page_problem(text: str, url: str = '') -> str | None:
    if 'passport2.chaoxing.com' in url or re.search(r'<input[^>]+(?:name|id)=[\"\'](?:password|uname)[\"\']', text, re.I):
        return '登录已失效，请重新登录'
    for marker, message in [('验证码', '需要验证码，请在官方页面完成验证'),
                            ('教师未创建完成该测验', '教师尚未创建完成该测验'),
                            ('作业未开放', '作业尚未开放'), ('作业已截止', '作业已截止'),
                            ('无权限访问', '当前账号没有访问权限')]:
        if marker in text:
            return message
    return None


def attach_dom_images(form, questions: list, page_url: str):
    """Associate image nodes with their original question/choice, including decoder output."""
    from chaoxing_linux.question_images import dom_image_source, image_status
    nodes = {}
    for item in form.select('.singleQuesId, .questionLi'):
        qid = str(item.get('data') or item.get('data-questionid') or '')
        if not qid:
            field = item.select_one('[name^=answertype]')
            qid = field['name'].removeprefix('answertype') if field else ''
        if qid:
            nodes[qid] = item
    for question in questions:
        question['images'] = []
        item = nodes.get(question['id'])
        if item is None:
            question['image_status'] = image_status([])
            continue
        title = item.select_one('.Zy_TItle, .mark_name, .question-title')
        collected = set()
        def collect(container, scope, option_key=None):
            labels = []
            if container is None:
                return labels
            for index, img in enumerate(container.find_all('img'), 1):
                if id(img) in collected:
                    continue
                collected.add(id(img))
                index = 1 + sum(image['scope'] == scope and image.get('option_key') == option_key
                                for image in question['images'])
                label = f'题目 {question["id"]} ' + (f'选项 {option_key}' if option_key else '题干') + f'图片 {index}'
                image = {'id': f'{question["id"]}-{scope}-{option_key or "stem"}-{index}',
                         'label': label, 'scope': scope, 'source_url': dom_image_source(img, page_url),
                         'mime': '', 'local_path': '', 'sha256': '', 'status': 'error', 'error': '图片尚未下载'}
                if option_key:
                    image['option_key'] = option_key
                question['images'].append(image)
                labels.append('[' + label + ']')
            return labels
        labels = collect(title, 'question')
        text = re.sub(r'<img\b[^>]*>', '', str(question.get('text') or ''), flags=re.I).strip()
        if labels:
            question['text'] = (text or '题干见图片') + ' ' + ' '.join(labels)
        else:
            question['text'] = text
        options = {option['key']: dict(option) for option in question.get('options', [])}
        ul = item.find('ul')
        if ul:
            for index, option_node in enumerate(ul.find_all('li', recursive=False)):
                original = str(option_node.get('aria-label') or option_node.get_text(' ', strip=True)).strip()
                match = re.match(r'^([A-Z])[.、．\s:：)]*\s*(.*)$', original, re.S)
                key = match[1] if match else chr(65 + index)
                option = options.get(key) or {'key': key, 'text': match[2] if match else original}
                option_labels = collect(option_node, 'option', key)
                option_text = re.sub(r'<img\b[^>]*>', '', str(option.get('text') or ''), flags=re.I).strip()
                if option_labels:
                    option['text'] = (option_text or f'选项 {key} 见图片') + ' ' + ' '.join(option_labels)
                options[key] = option
        question['options'] = sorted(options.values(), key=lambda option: option['key'])
        # Some layouts put diagrams/materials after the title element. Preserve
        # every remaining question image rather than silently losing that data.
        extra = collect(item, 'question')
        if extra:
            question['text'] = (question['text'] or '题干见图片') + ' ' + ' '.join(extra)
        question['image_status'] = image_status(question['images'])


def parse_work(html: str, url: str, decoder=None) -> dict:
    """Normalize the traditional and current form layouts, retaining exact server field names."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    candidates = [(len(node.select('.singleQuesId, .questionLi')), node) for node in soup.find_all('form')]
    candidates = sorted((item for item in candidates if item[0]), key=lambda item: item[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        raise InputRequired('页面包含多个作答表单，无法可靠选择，请在官方页面处理')
    form = candidates[0][1] if candidates else None
    if not form:
        raise InputRequired(page_problem(html, url) or '页面没有可作答表单，可能已提交或需要在手机端处理')
    raw = None
    if decoder:
        try:
            # Upstream decode_questions_info itself chooses the first form. Remove unrelated
            # forms but retain the head/style containing its font-decoding information.
            for other in soup.find_all('form'):
                if other is not form:
                    other.decompose()
            raw = decoder(str(soup))
        except Exception:
            if soup.find('style', id='cxSecretStyle'):
                raise InputRequired('加密字体解码失败，请在官方页面处理；没有提交答案')
    questions = []
    if raw and raw.get('questions'):
        for question in raw['questions']:
            options = []
            for index, line in enumerate(question.get('options', '').splitlines()):
                match = re.match(r'^\s*([A-Z])[.、．\s:：)]*\s*(.*)$', line, re.S)
                options.append({'key': match[1] if match else chr(65 + index), 'text': match[2] if match else line})
            kind = {'judgement': 'judge', 'completion': 'fill', 'shortanswer': 'text'}.get(question['type'], question['type'])
            questions.append({'id': str(question['id']), 'type': kind, 'text': question['title'],
                              'options': options, 'answer_type': str(question['answerField'][f"answertype{question['id']}"])})
    else:
        type_names = {'0': 'single', '1': 'multiple', '2': 'fill', '3': 'judge', '4': 'text'}
        for item in form.select('.singleQuesId, .questionLi'):
            qid = str(item.get('data') or item.get('data-questionid') or '')
            typed = item.select_one('.TiMu[data], input[name^=answertype]')
            code = str((typed.get('data') if typed else None) or (typed.get('value') if typed else None) or '')
            if not qid:
                field = item.select_one('[name^=answertype]')
                qid = field['name'].removeprefix('answertype') if field else ''
            title = item.select_one('.Zy_TItle, .mark_name, .question-title')
            options = []
            for index, option in enumerate(item.select('ul li')):
                text = str(option.get('aria-label') or option.get_text(' ', strip=True))
                match = re.match(r'^([A-Z])[.、．\s:：)]*\s*(.*)$', text, re.S)
                options.append({'key': match[1] if match else chr(65 + index), 'text': match[2] if match else text})
            if qid and code:
                questions.append({'id': qid, 'type': type_names.get(code, 'unknown'),
                                  'text': title.get_text(' ', strip=True) if title else '',
                                  'options': options, 'answer_type': code})
    attach_dom_images(form, questions, url)
    if not questions or len({q['id'] for q in questions}) != len(questions) or any(not q['text'].strip() for q in questions):
        raise InputRequired('未能可靠识别题目，请在官方页面处理；没有提交任何答案')
    for question in questions:
        if question['type'] != 'fill':
            continue
        containers = [node for node in form.select('.singleQuesId, .questionLi')
                      if str(node.get('data') or node.get('data-questionid') or '') == question['id']]
        fields_for_blanks = []
        if containers:
            prefix = 'answer' + question['id']
            fields_for_blanks = list(dict.fromkeys(node['name'] for node in containers[0].select('textarea[name], input[name]')
                                                   if node['name'].startswith(prefix + '_')))
        question['blank_fields'] = fields_for_blanks
        question['blank_count'] = len(fields_for_blanks) or 1
    if soup.find('style', id='cxSecretStyle') and not (raw and raw.get('questions')):
        raise InputRequired('题目使用加密字体且解析失败，请在官方页面处理')
    fields = {item['name']: item.get('value', '') for item in form.select('input[name]')
              if not item['name'].startswith(('answer', 'answertype'))}
    action = str(form.get('action') or '').strip()
    endpoint = checked_url(urljoin(url, action) if action else 'https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew')
    if 'addStudentWork' not in urlsplit(endpoint).path:
        raise InputRequired('未识别的作业提交接口，请在官方页面处理')
    return {'ok': True, 'status': 'success', 'url': url, 'questions': questions,
            'context': {'fields': fields, 'submit_url': endpoint}, 'message': f'读取到 {len(questions)} 道题'}


def verify_work_page(html: str, work: dict, fields: dict, mode: str) -> bool:
    """Confirm a visible server state or exact saved fields, ignoring script/UI templates."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    for node in soup.select('script, style, template'):
        node.decompose()
    if mode == 'submit':
        editable = any(form.select('.singleQuesId, .questionLi') and form.select('[name^=answer], textarea')
                       for form in soup.find_all('form'))
        if editable:
            return False
        text = soup.get_text(' ', strip=True)
        return any(marker in text for marker in ('作业已提交', '已完成本次作业', '待批阅'))
    for question in work['questions']:
        names = question.get('blank_fields') or [f'answer{question["id"]}']
        for name in names:
            nodes = soup.find_all(attrs={'name': name})
            checked = [node for node in nodes if node.get('type') in ('checkbox', 'radio') and node.has_attr('checked')]
            if checked:
                value = ''.join(sorted(str(node.get('value', '')) for node in checked))
            else:
                node = next((node for node in nodes if node.get('type') not in ('checkbox', 'radio')), None)
                value = str(node.get('value', '') or node.get_text()) if node else None
            if value is None or value.strip() != str(fields.get(name, '')).strip():
                return False
    return bool(work['questions'])


def parse_work_list(html: str, url: str) -> dict:
    """Read URLs supplied by the official list. Never build an assignment signature."""
    from bs4 import BeautifulSoup
    checked_url(url)
    soup = BeautifulSoup(html, 'lxml')
    if ('passport2.chaoxing.com' in url or soup.select_one('input[name=password], input#password')):
        raise InputRequired('登录已失效，请重新登录')
    if soup.select_one('input[name*=captcha], input[id*=captcha], input[name*=validatecode]'):
        raise InputRequired('需要验证码，请在官方页面完成验证')
    works, seen = [], set()
    containers = soup.select('.bottomList, .work-list, .workList')
    for container in containers:
        for row in container.select('li[data], li[data-url], li:has(a[href]), .work-list-item'):
            candidates = [row.get('data'), row.get('data-url')]
            candidates.extend(a.get('href') for a in row.select('a[href]'))
            work_url = None
            for candidate in candidates:
                if not candidate or candidate.startswith(('javascript:', '#')):
                    continue
                try:
                    candidate = checked_url(urljoin(url, candidate))
                except ValueError:
                    continue
                path = urlsplit(candidate).path.lower()
                if '/work/' in path and any(segment in path for segment in
                        ('/task', '/dowork', '/dohomeworknew', '/selectworkquestion', '/record-detail')):
                    work_url = candidate
                    break
            if not work_url:
                continue
            query = {key.lower(): value for key, value in parse_qsl(urlsplit(work_url).query)}
            work_id = query.get('workid') or query.get('workrelationid')
            if not work_id or work_id in seen:
                continue
            title_node = row.select_one('.right-content p:first-child, .work-title, .title')
            status_node = row.select_one('.status, .work-status')
            title = title_node.get_text(' ', strip=True) if title_node else ''
            if not title:
                continue
            status_text = status_node.get_text(' ', strip=True) if status_node else ''
            if any(word in status_text for word in ('已截止', '已结束', '过期', '不能作答')):
                status = 'expired'
            elif any(word in status_text for word in ('未开始', '未开放', '未发布', '未发放')):
                status = 'not_open'
            elif any(word in status_text for word in ('已完成', '已提交', '待批阅', '已批阅', '已交', '已批改')):
                status = 'submitted'
            elif any(word in status_text for word in ('未交', '待完成', '未完成', '待提交', '进行中')):
                status = 'pending'
            else:
                status = 'unknown'
            path = urlsplit(work_url).path.lower()
            editable_path = any(path.endswith('/' + name) for name in ('task', 'dowork', 'dohomeworknew'))
            actionable = status == 'pending' and editable_path
            answer_id = query.get('answerid') or query.get('workanswerid') or ''
            works.append({'id': work_id, 'title': title, 'url': work_url, 'status': status,
                          'status_text': status_text, 'actionable': actionable,
                          'requires_start': actionable and path.endswith('/task') and answer_id in ('', '0', '-1'),
                          'answer_id': answer_id})
            seen.add(work_id)
    if not containers and not any(word in soup.get_text() for word in ('暂无作业', '还没有作业', '暂无数据', '没有相关作业')):
        raise InputRequired('未识别学习通作业列表页面，请打开官方课程确认权限或页面状态')
    # Only follow pagination links which are present on the official list itself.
    pages = []
    for anchor in soup.select('.pageDiv a[href], .pagination a[href], a[rel=next][href]'):
        try:
            candidate = checked_url(urljoin(url, anchor['href']))
        except ValueError:
            continue
        if urlsplit(candidate).path == urlsplit(url).path and candidate != url and candidate not in pages:
            pages.append(candidate)
    return {'works': works, 'pagination_urls': pages}


def make_work_form(work: dict, payload: dict, *, data_dir=None) -> dict:
    mode = payload.get('mode', 'save')
    if mode not in ('save', 'submit'):
        raise ValueError('mode 必须是 save 或 submit')
    questions = work['questions']
    original = payload.get('questions')
    if not isinstance(original, list) or [str(q.get('id')) for q in original] != [q['id'] for q in questions]:
        raise ValueError('题目列表已变化，请重新读取作业后作答')
    # Text/options changes can mean that a teacher edited or randomized the assignment.
    for expected, supplied in zip(questions, original):
        for field in ('type', 'text', 'options'):
            if supplied.get(field) != expected.get(field):
                raise ValueError('题目内容或选项已变化，请重新读取作业')
        current_images, previous_images = expected.get('images', []), supplied.get('images', [])
        for question in (expected, supplied):
            status = question.get('image_status') or {}
            if (not isinstance(status, dict) or status.get('state') in ('partial', 'failed', 'error')
                    or not isinstance(question.get('images', []), list)
                    or (isinstance(status.get('count'), int) and status['count'] > len(question.get('images', [])))):
                raise ValueError('题目图片尚未完整读取，请重新读取作业')
        if current_images or previous_images:
            from chaoxing_linux.question_images import normalize_images
            current_images = normalize_images(current_images, data_dir=data_dir)
            previous_images = normalize_images(previous_images, data_dir=data_dir)
            keys = ('id', 'label', 'scope', 'option_key', 'sha256', 'mime', 'status')
            if ([tuple(image.get(key) for key in keys) for image in current_images]
                    != [tuple(image.get(key) for key in keys) for image in previous_images]):
                raise ValueError('题目图片内容或位置已变化，请重新读取和作答')
    answers = payload.get('answers', {})
    if isinstance(answers, list):
        if len(answers) != len(questions) or any(not isinstance(a, dict) or 'id' not in a for a in answers):
            raise ValueError('答案必须与题目逐一对应')
        if any(a.get('status') == 'unknown' for a in answers):
            raise ValueError('存在未知答案，没有保存或提交')
        by_id = {str(a['id']): a for a in answers}
        for question in questions:
            if question.get('images'):
                status = by_id.get(question['id'], {}).get('image_status') or {}
                count = len(question['images'])
                if (not isinstance(status, dict) or status.get('state') != 'ready'
                        or status.get('count') != count or status.get('sent') != count):
                    raise ValueError('题目图片没有完整发送模型，请重新作答')
        answers = {str(a['id']): a.get('answer') for a in answers}
    if not isinstance(answers, dict) or set(map(str, answers)) != {q['id'] for q in questions}:
        raise ValueError('答案必须与题目逐一对应')
    answers = {str(k): v for k, v in answers.items()}
    fields = dict(work['context']['fields'])
    fields['answerwqbid'] = ','.join(q['id'] for q in questions) + ','
    fields['pyFlag'] = '1' if mode == 'save' else ''
    for q in questions:
        answer = answers[q['id']]
        if answer is None or not str(answer).strip():
            raise ValueError(f'题目 {q["id"]} 答案为空；没有提交')
        if q['type'] in ('single', 'multiple'):
            if isinstance(answer, list):
                answer = ''.join(str(value) for value in answer)
            answer = re.sub(r'[\s,，、;；]', '', str(answer)).upper()
            allowed = {o['key'] for o in q['options']}
            if not answer or set(answer) - allowed or (q['type'] == 'single' and len(answer) != 1):
                raise ValueError(f'题目 {q["id"]} 的答案不是有效选项')
            answer = ''.join(sorted(set(answer)))
        elif q['type'] in ('judge', 'judgement'):
            table = {'true': 'true', 'false': 'false', '正确': 'true', '错误': 'false',
                     '对': 'true', '错': 'false', '√': 'true', '×': 'false', '1': 'true', '0': 'false'}
            if str(answer).lower() not in table:
                raise ValueError(f'题目 {q["id"]} 需要明确的判断答案')
            answer = table[str(answer).lower()]
        elif q['type'] in ('fill', 'completion'):
            values = answer if isinstance(answer, list) else [answer]
            if not values or any(value is None or not str(value).strip() for value in values):
                raise ValueError(f'题目 {q["id"]} 包含空白填空答案')
            blank_fields = q.get('blank_fields', [])
            if blank_fields:
                if len(values) != len(blank_fields):
                    raise ValueError(f'题目 {q["id"]} 的填空答案数量不匹配')
                fields.update({name: str(value) for name, value in zip(blank_fields, values)})
                fields[f'answertype{q["id"]}'] = q['answer_type']
                continue
            if len(values) != 1:
                raise InputRequired(f'题目 {q["id"]} 的多个填空字段未能识别，请在官方页面填写')
            answer = values[0]
        elif q['type'] not in ('text', 'shortanswer'):
            raise InputRequired(f'题目 {q["id"]} 类型尚不支持，请在官方页面处理')
        fields[f'answer{q["id"]}'] = str(answer)
        fields[f'answertype{q["id"]}'] = q['answer_type']
    return fields


def sign_parameters(course: dict, payload: dict, uid, fid) -> dict:
    activity_id = str(payload.get('activity_id') or payload.get('activeId') or '')
    if not activity_id.isdigit():
        raise ValueError('请填写有效活动 ID')
    kind = str(payload.get('sign_type', 'normal')).lower()
    params = {'activeId': activity_id, 'uid': uid, 'fid': fid, 'courseId': course['courseId'],
              'classId': course['clazzId'], 'appType': '15', 'name': payload.get('name', ''),
              'clientip': '', 'latitude': -1, 'longitude': -1}
    if kind in ('code', 'gesture'):
        code = str(payload.get('sign_code') or payload.get('signCode') or '').strip()
        if not code.isdigit():
            raise InputRequired('请提供现场签到码或手势数字序列')
        params['signCode'] = code
    elif kind == 'location':
        if not all(str(payload.get(k, '')).strip() for k in ('latitude', 'longitude', 'address')):
            raise InputRequired('位置签到需要经纬度和地址')
        lat, lon = float(payload['latitude']), float(payload['longitude'])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError('签到经纬度超出范围')
        params.update(latitude=lat, longitude=lon, address=payload['address'], ifTiJiao=1)
    elif kind == 'qr':
        enc = str(payload.get('enc') or '').strip()
        if not enc and payload.get('qr_content'):
            qr = parse_qs(urlsplit(payload['qr_content']).query)
            enc = qr.get('enc', [''])[0]
        if not enc:
            raise InputRequired('二维码签到需要当前有效二维码中的 enc')
        params['enc'] = enc
    elif kind != 'normal':
        raise InputRequired('当前签到类型需要在手机官方应用中完成')
    return params


class Worker:
    def __init__(self, backend_root: Path, settings: dict, emit=lambda event: None):
        self.root, self.settings, self.emit = backend_root, settings, emit
        self.data = Path(settings.get('data_dir') or settings.get('backend_data_dir') or Path.home() / '.local/share/chaoxing-linux/study-data').expanduser().resolve()
        if self.data == self.root or self.root in self.data.parents:
            raise ValueError('后端数据目录必须独立于原项目')
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        os.chdir(self.data)
        sys.path.insert(0, str(backend_root))
        from api.config import GlobalConst
        GlobalConst.COOKIES_PATH = str(self.data / 'cookies.txt')
        from api.logger import logger
        logger.remove()
        import api.cxsecret_font as upstream_font
        # The original loader resolves resource/ against CWD. Keep CWD private while
        # loading its immutable font table explicitly from the selected source tree.
        font_table = backend_root / 'resource/font_map_table.json'
        if not font_table.is_file():
            raise InputRequired('学习后端缺少 resource/font_map_table.json，请重新安装后端')
        upstream_font.fonthash_dao = upstream_font.FontHashDAO(str(font_table))
        self.font_entries = len(upstream_font.fonthash_dao.hash_map)
        if not self.font_entries:
            raise InputRequired('学习后端字体映射为空，请重新安装后端')
        from api.base import Account, Chaoxing, SessionManager
        from api.decode import decode_course_card, decode_questions_info
        from loguru import logger
        logger.remove()
        # Upstream trace logging can contain cookies and URLs; all app events are authored here.
        self.account_class, self.client_class = Account, Chaoxing
        self.session_manager = SessionManager
        self.decode_card, self.decode_questions = decode_course_card, decode_questions_info
        self.client = Chaoxing()
        import api.base as upstream_base
        emit = self.emit
        class Progress:
            def __init__(self, total=0, initial=0, desc='', **kwargs):
                self.total, self.n, self.desc, self.last = total, initial, desc, 0
            def refresh(self):
                if time.monotonic() - self.last >= 2:
                    self.last = time.monotonic()
                    emit({'message': f'{self.desc}：{int(self.n)}/{self.total} 秒',
                          'progress': min(1, self.n / max(1, self.total))})
            def close(self):
                pass
        upstream_base.tqdm = Progress
        import requests
        original = requests.sessions.Session.request
        def timed_request(session, method, url, **kwargs):
            kwargs.setdefault('timeout', 20)
            return original(session, method, url, **kwargs)
        requests.sessions.Session.request = timed_request

    @property
    def session(self):
        return self.session_manager.get_session()

    def login(self, payload):
        cookie = payload.get('cookies') or self.settings.get('cookies')
        if cookie:
            if isinstance(cookie, dict):
                cookie = ';'.join(f'{k}={v}' for k, v in cookie.items())
            path = self.data / 'cookies.txt'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(str(cookie))
        username = payload.get('username') or self.settings.get('username')
        password = payload.get('password') or self.settings.get('password')
        self.client = self.client_class(self.account_class(username, password) if username and password else None)
        if not cookie and not (username and password) and not (self.data / 'cookies.txt').exists():
            raise InputRequired('请输入账号密码或导入本人会话 Cookie')
        response = self.client.login(login_with_cookies=bool(cookie) or not (username and password))
        path = self.data / 'cookies.txt'
        if path.exists():
            os.chmod(path, 0o600)
        return {'ok': bool(response.get('status')), 'status': 'success' if response.get('status') else 'needs_input',
                'message': str(response.get('msg', '登录未成功'))[:500]}

    def load_work(self, payload):
        url = checked_url(payload.get('url', ''))
        response = self.get_work_page(url)
        if response.status_code != 200:
            raise InputRequired(f'作业页面返回 HTTP {response.status_code}')
        issue = page_problem(response.text, response.url)
        # Captcha words also occur in ordinary page scripts; require a captcha control.
        if issue and ('验证码' not in issue or re.search(r'(?:id|name)=[\"\'][^\"\']*(?:captcha|validatecode)', response.text, re.I)):
            raise InputRequired(issue)
        work = parse_work(response.text, response.url, self.decode_questions)
        from chaoxing_linux.question_images import image_status, materialize_images
        for question in work['questions']:
            if question['images']:
                question['images'] = materialize_images(question['images'], data_dir=self.data, session=self.session)
            question['image_status'] = image_status(question['images'])
        return work

    def course_entry_url(self, course):
        """Find the actual signed course navigation link in the account's course list."""
        from bs4 import BeautifulSoup
        endpoint = 'https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata'
        folders = ['0']
        def find_in_folder(folder):
            response = self.session.post(endpoint, data={'courseType': 1, 'courseFolderId': folder,
                                                        'query': '', 'superstarClass': 0})
            if response.status_code != 200:
                raise InputRequired(f'课程列表返回 HTTP {response.status_code}')
            soup = BeautifulSoup(response.text, 'lxml')
            if soup.select_one('input[name=password], input#password'):
                raise InputRequired('登录已失效，请重新登录')
            for node in soup.select('div.course'):
                course_field = node.select_one('input.courseId')
                class_field = node.select_one('input.clazzId')
                if (not course_field or not class_field or course_field.get('value') != course['courseId']
                        or class_field.get('value') != course['clazzId']):
                    continue
                for anchor in node.select('a[href]'):
                    try:
                        entry = checked_url(urljoin(response.url, anchor['href']))
                    except ValueError:
                        continue
                    if '/visit/stucoursemiddle' in urlsplit(entry).path:
                        return entry
            return None
        entry = find_in_folder('0')
        if entry:
            return entry
        interaction = self.get_work_page('https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction')
        if interaction.status_code != 200:
            raise InputRequired(f'课程文件夹返回 HTTP {interaction.status_code}')
        soup = BeautifulSoup(interaction.text, 'lxml')
        for node in soup.select('ul.file-list > li[fileid]'):
            folder = str(node['fileid'])
            if folder not in folders:
                folders.append(folder)
        for folder in folders[1:]:
            entry = find_in_folder(folder)
            if entry:
                return entry
        raise InputRequired('当前会话的课程列表中没有找到该课程，请刷新课程列表后重试')

    def list_work(self, payload):
        from bs4 import BeautifulSoup
        course = normalize_course(payload.get('course') or payload)
        entry = self.get_work_page(self.course_entry_url(course))
        if entry.status_code != 200:
            raise InputRequired(f'官方课程入口返回 HTTP {entry.status_code}')
        soup = BeautifulSoup(entry.text, 'lxml')
        anchor = soup.select_one('[dataname="zy"] a[data-url]')
        if not anchor:
            raise InputRequired('该课程没有提供可识别的官方作业入口，请在学习通中确认课程权限')
        list_url = checked_url(urljoin(entry.url, anchor['data-url']))
        if '/work/' not in urlsplit(list_url).path:
            raise InputRequired('官方作业入口格式已变化，请在学习通中打开作业列表')
        def hidden(name):
            node = soup.find('input', id=name) or soup.find('input', attrs={'name': name})
            return str(node.get('value', '')) if node else ''
        course_id, class_id, cpi, work_enc = (hidden(name) for name in ('courseid', 'clazzid', 'cpi', 'workEnc'))
        if course_id != course['courseId'] or class_id != course['clazzId'] or not work_enc:
            raise InputRequired('官方课程页未返回有效作业导航凭证，请重新登录或刷新课程')
        # Reproduce the observed official course-stu.js navigation: these values all
        # come from that page, including workEnc; no assignment-level enc is computed.
        parts = urlsplit(list_url)
        parameters = dict(parse_qsl(parts.query, keep_blank_values=True))
        parameters.update(courseId=course_id, classId=class_id, cpi=cpi, ut='s', enc=work_enc)
        list_url = checked_url(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(parameters), '')))
        queue, visited, works, seen = [list_url], set(), [], set()
        while queue and len(visited) < 20:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            self.emit({'message': f'读取官方作业列表第 {len(visited)} 页'})
            response = self.get_work_page(url)
            if response.status_code != 200:
                raise InputRequired(f'官方作业列表返回 HTTP {response.status_code}')
            page = parse_work_list(response.text, response.url)
            for work in page['works']:
                if work['id'] not in seen:
                    works.append(work)
                    seen.add(work['id'])
            for candidate in page['pagination_urls']:
                query = {key.lower(): value for key, value in parse_qsl(urlsplit(candidate).query)}
                if (query.get('courseid', course_id) != course_id or query.get('classid', class_id) != class_id):
                    continue
                if candidate not in visited and candidate not in queue:
                    queue.append(candidate)
        return {'ok': True, 'status': 'partial' if queue else 'success', 'works': works,
                'source_url': list_url, 'truncated': bool(queue),
                'message': f'已读取 {len(works)} 份官方课程作业' + ('，还有后续页未读取' if queue else '')}

    def get_work_page(self, url):
        for _ in range(6):
            response = self.session.get(checked_url(url), allow_redirects=False)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            url = checked_url(urljoin(url, response.headers.get('Location', '')))
        raise InputRequired('作业页面重定向过多，请在官方页面处理')

    def submit_work(self, payload):
        work = self.load_work(payload)
        fields = make_work_form(work, payload, data_dir=getattr(self, 'data', None))
        response = self.session.post(work['context']['submit_url'], data=fields,
                                     headers={'Referer': work['url'], 'X-Requested-With': 'XMLHttpRequest'},
                                     allow_redirects=False)
        if response.status_code != 200:
            return {'ok': False, 'status': 'error', 'message': f'作业请求返回 HTTP {response.status_code}，请核对官方页面'}
        try:
            raw = response.json()
        except ValueError:
            raise InputRequired(page_problem(response.text, response.url) or '提交接口没有返回可识别结果，请核对官方页面')
        accepted = raw.get('status') in (True, 1, '1', 'true')
        mode = payload.get('mode', 'save')
        result = {'ok': accepted, 'status': 'success' if accepted else 'needs_input', 'mode': mode,
                  'message': str(raw.get('msg') or raw.get('message') or '服务器未返回说明')[:1000],
                  'server_response': raw, 'verified': False}
        if accepted:
            # Acknowledgment is distinguished from read-back; no fabricated grade or completion.
            try:
                page = self.get_work_page(work['url'])
                result['verified'] = page.status_code == 200 and verify_work_page(page.text, work, fields, mode)
                result['verification_message'] = '页面已核对' if result['verified'] else '服务器已接受请求，页面状态尚未可靠核对，请人工查看'
            except Exception:
                result['verification_message'] = '服务器已接受请求，回读失败，请人工查看'
        return result

    def plan(self, payload):
        course = normalize_course(payload.get('course') or payload)
        chapters = self.client.get_course_point(course['courseId'], course['clazzId'], course['cpi'])
        tasks = []
        seen = set()
        points = chapters.get('points', [])
        # get_job_list calls study_emptypage when no jobs exist. Suppress that mutation during planning.
        original = self.client.study_emptypage
        self.client.study_emptypage = lambda *args: None
        try:
            for index, point in enumerate(points):
                self.emit({'message': f'读取章节 {index + 1}/{len(points)}：{point.get("title", "")}',
                           'progress': (index + 1) / max(1, len(points))})
                if point.get('has_finished') or point.get('need_unlock'):
                    tasks.append({'id': str(point['id']), 'type': 'chapter', 'name': point.get('title', ''),
                                  'status': 'completed' if point.get('has_finished') else 'locked', 'point': point})
                    continue
                jobs, info = self.client.get_job_list(course, point)
                if info.get('notOpen'):
                    tasks.append({'id': str(point['id']), 'type': 'chapter', 'name': point.get('title', ''),
                                  'status': 'not_open', 'point': point})
                elif not jobs:
                    tasks.append({'id': str(point['id']), 'type': 'chapter', 'name': point.get('title', ''),
                                  'status': 'pending', 'point': point})
                for index, job in enumerate(jobs):
                    kind = {'workid': 'quiz'}.get(job['type'], job['type'])
                    identity = str(job.get('jobid') or job.get('objectid') or job.get('id') or job.get('aid') or job.get('mid') or index)
                    task_id = f'{point["id"]}:{kind}:{identity}'
                    if task_id in seen:
                        continue
                    seen.add(task_id)
                    task = {'id': task_id,
                            'name': job.get('name') or job.get('title') or point.get('title', ''),
                            'type': kind, 'status': 'pending', 'point': point, 'job': job, 'job_info': info}
                    if kind == 'quiz':
                        task['work_url'] = self.quiz_url(course, job, info)
                    tasks.append(task)
        finally:
            self.client.study_emptypage = original
        return {'ok': True, 'status': 'success', 'course': course, 'tasks': tasks,
                'message': f'读取到 {len(tasks)} 项任务；计划不会执行任务'}

    @staticmethod
    def quiz_url(course, job, info):
        params = {'api': '1', 'workId': job.get('jobid', '').replace('work-', ''), 'jobid': job.get('jobid', ''),
                  'originJobId': job.get('jobid', ''), 'needRedirect': 'true', 'skipHeader': 'true',
                  'knowledgeid': info.get('knowledgeid', ''), 'ktoken': info.get('ktoken', ''),
                  'cpi': info.get('cpi', course.get('cpi', '')), 'ut': 's', 'clazzId': course['clazzId'],
                  'type': '', 'enc': job.get('enc', ''), 'mooc2': '1', 'courseid': course['courseId']}
        return 'https://mooc1.chaoxing.com/mooc-ans/api/work?' + urlencode(params)

    def run_course(self, payload):
        plan = self.plan(payload)
        course, tasks = plan['course'], plan['tasks']
        selected = payload.get('task_ids')
        results = []
        for index, task in enumerate(tasks):
            item = {'id': task['id'], 'name': task['name'], 'type': task['type']}
            if task['status'] != 'pending' or (selected is not None and task['id'] not in selected):
                item.update(status=task['status'] if task['status'] != 'pending' else 'not_selected')
                results.append(item)
                continue
            self.emit({'message': f'执行 {index + 1}/{len(tasks)}：{task["name"]}', 'progress': index / max(1, len(tasks))})
            try:
                if task['type'] == 'quiz':
                    item.update(self.run_quiz(task, payload))
                elif task['type'] == 'video':
                    value = self.client.study_video(course, task['job'], task['job_info'], _speed=float(payload.get('speed', 1.0)))
                    if value.is_failure():
                        value = self.client.study_video(course, task['job'], task['job_info'], _speed=float(payload.get('speed', 1.0)), _type='Audio')
                    item.update(status='reported' if value.is_success() else 'error', source_result=value.name)
                elif task['type'] == 'document':
                    value = self.client.study_document(course, task['job'])
                    item.update(status='reported' if value.is_success() else 'error', source_result=value.name)
                elif task['type'] == 'read':
                    value = self.client.study_read(course, task['job'], task['job_info'])
                    item.update(status='reported' if value.is_success() else 'error', source_result=value.name)
                elif task['type'] == 'chapter':
                    value = self.client.study_emptypage(course, task['point'])
                    item.update(status='reported' if value.is_success() else 'error', source_result=value.name)
                else:
                    item.update(status='unsupported', message='该任务需在官方应用中完成')
            except Exception as exc:
                item.update(status='error', message=f'任务请求失败（{type(exc).__name__}）')
            results.append(item)
            self.emit({'message': f'{task["name"]}：{item["status"]}', 'progress': (index + 1) / max(1, len(tasks))})
        # Source methods may treat HTTP 200 as success. Verify each chapter using a fresh server read.
        verified_chapters = set()
        try:
            fresh = self.client.get_course_point(course['courseId'], course['clazzId'], course['cpi'])
            verified_chapters = {str(point['id']) for point in fresh.get('points', []) if point.get('has_finished')}
        except Exception:
            pass
        for item, task in zip(results, tasks):
            if item['status'] == 'reported':
                item['verified'] = str(task['point']['id']) in verified_chapters
                if item['verified']:
                    item['status'] = 'completed'
        complete = all(item['status'] in ('completed', 'not_selected') for item in results)
        return {'ok': complete, 'status': 'success' if complete else 'partial', 'results': results,
                'message': '已结束课程队列；请查看逐项结果，reported 仅表示上游请求被接受'}

    def run_quiz(self, task, payload):
        url = task['work_url']
        if not payload.get('solve_quizzes', False):
            return {'status': 'needs_input', 'work_url': url, 'message': '章节测验待解答；未自动保存或提交'}
        work = self.load_work({'url': url})
        def progress(done, total, result):
            self.emit({'message': f'章节测验搜题 {done}/{total}', 'progress': done / max(1, total)})
        if self.settings.get('ai_provider') == 'chatgpt':
            from chaoxing_linux.chatgpt_ai import configured_ai
            ai = configured_ai(self.settings)
        else:
            from chaoxing_linux.assistant_ai import AIClient
            ai = AIClient(self.settings)
        answers = ai.solve_questions(work['questions'], search=True, progress=progress)
        unknown = [a for a in answers if a.get('status') != 'ok' or a.get('answer') is None or not str(a.get('answer')).strip()]
        if len(answers) != len(work['questions']) or unknown:
            return {'status': 'needs_input', 'work_url': url, 'questions': work['questions'], 'answers': answers,
                    'message': '部分题目未找到可靠答案，未保存或提交本测验'}
        unverified = [a for a in answers if not isinstance(a.get('search_status'), dict)
                      or (a['search_status'].get('state') not in ('disabled','not_used')
                          and (a['search_status'].get('state') != 'ok' or not a.get('sources')))]
        if payload.get('submit_quizzes', False) and unverified:
            return {'status': 'needs_input', 'work_url': url, 'questions': work['questions'], 'answers': answers,
                    'message': '部分答案没有联网来源核验，未自动正式提交本测验'}
        mode = 'submit' if payload.get('submit_quizzes', False) else 'save'
        result = self.submit_work({'url': url, 'questions': work['questions'], 'answers': answers, 'mode': mode})
        # Saving an answer is never completion of a scored quiz.
        status = 'completed' if mode == 'submit' and result.get('ok') and result.get('verified') else (
            'saved' if mode == 'save' and result.get('ok') else 'reported' if result.get('ok') else 'error')
        return {'status': status, 'work_url': url, 'mode': mode, 'answers': answers, 'submission': result}

    def execute(self, action, payload):
        if action == 'status':
            return {'ok': True, 'status': 'success', 'message': '学习后端已就绪',
                    'python': sys.version.split()[0], 'backend_path': str(self.root),
                    'font_table_entries': self.font_entries,
                    'has_session': (self.data / 'cookies.txt').exists(),
                    'capabilities': ['courses', 'course_plan', 'course_run', 'work_list', 'work_load', 'work_submit', 'activities', 'sign', 'review_collect']}
        if action == 'login':
            return self.login(payload)
        if not self.client._validate_cookie_session():
            raise InputRequired('请先登录学习后端；手机调试授权不等同于网站账号登录')
        if action == 'courses':
            return {'ok': True, 'status': 'success', 'courses': [normalize_course(c) for c in self.client.get_course_list()]}
        if action == 'review_collect':
            from chaoxing_linux.review_fetch import collect_course
            return collect_course(self, payload)
        if action == 'course_plan':
            return self.plan(payload)
        if action == 'course_run':
            return self.run_course(payload)
        if action == 'work_list':
            return self.list_work(payload)
        if action == 'work_load':
            return self.load_work(payload)
        if action == 'work_submit':
            return self.submit_work(payload)
        course = normalize_course(payload.get('course') or payload)
        if action == 'activities':
            return {'ok': True, 'status': 'success', 'activities': self.client.get_activity_list(course)}
        if action == 'sign':
            params = sign_parameters(course, payload, self.client.get_uid(), self.client.get_fid())
            pre = self.client.pre_sign(course, params['activeId'])
            issue = page_problem(pre)
            if issue and ('验证码' not in issue or re.search(r'(?:id|name)=[\"\'][^\"\']*(?:captcha|validatecode)', pre, re.I)):
                raise InputRequired(issue)
            if payload.get('sign_type', 'normal') == 'normal':
                response = self.client.sign_in_normal(course, params['activeId'], name=params['name'], obj_id='')
            else:
                response = self.session.get('https://mobilelearn.chaoxing.com/pptSign/stuSignajax', params=params).text
            success = response.strip() in ('success', '您已签到过了', '已签到')
            return {'ok': success, 'status': 'success' if success else 'needs_input',
                    'message': response[:1000], 'server_response': response[:1000]}
        raise ValueError('未知后端操作')


def main():
    wire = sys.stdout
    def send(kind, value):
        wire.write(json.dumps({'type': kind, 'event' if kind == 'event' else 'result': value}, ensure_ascii=False) + '\n')
        wire.flush()
    try:
        request = json.loads(sys.stdin.readline())
        parent = request.get('_parent') or {'pid': os.getppid(), 'identity': process_identity(os.getppid())}
        start_parent_guard(int(parent['pid']), parent.get('identity'))
        os.umask(0o077)
        with open(os.devnull, 'w') as silent, contextlib.redirect_stdout(silent), contextlib.redirect_stderr(silent):
            worker = Worker(Path(sys.argv[1]).resolve(), request.get('settings', {}), lambda event: send('event', event))
            result = worker.execute(request['action'], request.get('payload', {}))
        send('result', result)
    except InputRequired as exc:
        send('result', {'ok': False, 'status': 'needs_input', 'message': str(exc)})
    except ValueError as exc:
        send('result', {'ok': False, 'status': 'error', 'message': str(exc)})
    except Exception as exc:
        send('result', {'ok': False, 'status': 'error', 'message': f'后端请求失败（{type(exc).__name__}），请检查网络、依赖和官方页面状态'})


if __name__ == '__main__':
    main()
