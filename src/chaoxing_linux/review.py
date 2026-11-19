"""Build a revision guide from a read-only course snapshot."""
from concurrent.futures import CancelledError
from datetime import datetime
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .assistant_ai import AIError


SECTIONS = (('chapters', '章节复习'), ('assignments', '作业复盘'), ('materials', '课程资料'))
LINK_FIELDS = {'courseid', 'classid', 'clazzid', 'knowledgeid', 'chapterid', 'workid', 'dataid'}
MAX_SOURCE_CHARS = 3200
AI_BATCH_SIZE = 6


class ReviewNeedsInput(ValueError):
    pass


def _check_cancel(cancel):
    if cancel.is_set():
        raise CancelledError()


def _text(value):
    if not isinstance(value, str):
        return ''
    cleaned = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', ' ', value)
    cleaned = re.sub(r'https?://[^\s<>"“”]+', '[原文链接]', cleaned)
    return cleaned.strip()


def source_link(url):
    """Only keep stable course identifiers on HTTPS source references."""
    try:
        parts = urlsplit(str(url or ''))
        host = parts.hostname or ''
        if (parts.scheme != 'https' or not (host == 'chaoxing.com' or host.endswith('.chaoxing.com'))
                or parts.username or parts.password or parts.port not in (None, 443)):
            return ''
    except ValueError:
        return ''
    if not parts.path or len(parts.path) > 256 or '%' in parts.path or not re.fullmatch(r'/[A-Za-z0-9_./-]*', parts.path):
        return ''
    keep = [(key, value) for key, value in parse_qsl(parts.query)
            if key.lower() in LINK_FIELDS and re.fullmatch(r'\d{1,20}', value)]
    return urlunsplit(('https', host, parts.path, urlencode(keep), ''))


def _question_text(question, index):
    stem = _text(question.get('text') or question.get('title'))
    if not stem:
        return ''
    options = [f'{option.get("key", "")}. {_text(option.get("text"))}'
               for option in (question.get('options') or []) if isinstance(option, dict) and option.get('text')]
    attachments = [_text(item.get('title')) for item in (question.get('attachments') or [])
                   if isinstance(item, dict) and item.get('title')]
    return '　'.join([f'{index}. {stem}', *options, *attachments])


def _build_item(record, category):
    title = _text(record.get('title') or record.get('name'))[:160] or '未命名条目'
    content = _text(record.get('text'))
    source = source_link(record.get('url'))
    note = _text(record.get('note') or record.get('content_status'))[:220]
    if category == 'assignments':
        note = ' '.join(filter(None, (_text(record.get('status_text') or record.get('status'))[:120], note)))
    if category == 'materials' and (folder := _text(record.get('folder'))[:180]):
        note = ' '.join(filter(None, (f'资料目录 {folder}', note)))
    questions = [line for index, question in enumerate(record.get('questions') or [], 1)
                 if isinstance(question, dict) and (line := _question_text(question, index))]
    attachments = [name for item in (record.get('attachments') or []) if isinstance(item, dict)
                   and (name := _text(item.get('title') or item.get('name')))]
    practice = []
    if category == 'assignments' and questions:
        practice = [question[:380]+('…' if len(question) > 380 else '') for question in questions[:12]]
        if len(questions) > 12:
            note = ' '.join(filter(None, (note, f'收录前 12 道，共读取 {len(questions)} 道。')))
    if attachments:
        note = ' '.join(filter(None, (note, '随章资料：' + '、'.join(attachments[:8])[:220])))
    if not content and not questions:
        note = ' '.join(filter(None, (note, '本条仅收录索引，未取得可整理的正文。')))
    draft = dict(title=title, summary=[], practice=practice, source=source, note=note)
    details = content or '\n'.join(questions)
    if len(details) > MAX_SOURCE_CHARS:
        draft['note'] = ' '.join(filter(None, (note, 'AI 根据正文节选整理。')))
    details = details[:MAX_SOURCE_CHARS]
    return draft, details


def _records(records, prefix=''):
    for record in records:
        if not isinstance(record, dict):
            continue
        title = _text(record.get('title') or record.get('name'))
        item_title = ' / '.join(filter(None, [prefix, title]))
        yield dict(record, title=item_title)
        children = record.get('children')
        if isinstance(children, list):
            yield from _records(children, item_title)


def build_review(archive, *, summarize, progress=lambda _event: None, cancel):
    course = archive.get('course') or {}
    issues = [_text(issue)[:300] for issue in (archive.get('issues') or []) if _text(issue)]
    review = {'title': _text(course.get('title') or course.get('name')) or '课程',
              'generated_at': datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z'),
              'summary_mode': 'ai', 'sections': [], 'issues': issues, 'counts': {}}
    pending = []
    for key, heading in SECTIONS:
        items = []
        for record in _records(archive.get(key) or []):
            _check_cancel(cancel)
            item, details = _build_item(record, key)
            items.append(item)
            if details:
                pending.append((item, details))
        review['sections'].append({'title': heading, 'items': items})
        review['counts'][key] = len(items)

    if not pending:
        raise ReviewNeedsInput('当前课程只有目录或标题，没有可交给 AI 整理的正文。')

    enriched = 0
    for offset in range(0, len(pending), AI_BATCH_SIZE):
        _check_cancel(cancel)
        batch = pending[offset:offset + AI_BATCH_SIZE]
        progress({'message': f'正在用 AI 整理第 {offset + 1}–{offset + len(batch)} 条正文',
                  'progress': .65 + .22 * offset / len(pending)})
        items = [{'id': str(index), 'title': item['title'], 'text': details}
                 for index, (item, details) in enumerate(batch, 1)]
        summaries = summarize(items, cancel)
        if not isinstance(summaries, dict) or set(summaries) != {item['id'] for item in items}:
            raise AIError('模型回复缺少对应的复习条目。')
        for index, (item, _) in enumerate(batch, 1):
            points = summaries[str(index)]
            if not isinstance(points, list) or any(not isinstance(point, str) for point in points):
                raise AIError('模型返回的复习要点格式无效。')
            item['summary'] = [_text(point)[:250] for point in points[:5] if _text(point)]
            if item['summary']:
                enriched += 1
            else:
                item['note'] = ' '.join(filter(None, (item['note'], '原文不足以提炼复习要点。')))
    if not enriched:
        raise ReviewNeedsInput('AI 未能从课程正文提炼出可用的复习要点。')
    if enriched < len(pending):
        issues.append(f'{len(pending) - enriched} 条原文不足以提炼复习要点。')
    return review


def run_review_export(service, payload, event, cancel):
    from .review_documents import export_review, require_export_tools

    course = payload.get('course')
    if not isinstance(course, dict) or not (course.get('courseId') or course.get('courseid')):
        raise ValueError('请先选择要导出的学习通课程。')
    destination = payload.get('output_dir')
    if not isinstance(destination, str) or not Path(destination).is_absolute() or not Path(destination).is_dir():
        raise ValueError('请选择已经存在的本机导出目录。')
    ai = service._ai()
    try:
        ai.check_review_settings()
    except (AIError, ValueError) as exc:
        return {'ok': False, 'status': 'needs_input', 'message': str(exc)}
    require_export_tools()
    event({'message': '正在读取学习通课程信息', 'progress': .03})

    def collecting(update):
        update = dict(update)
        if isinstance(update.get('progress'), (float, int)):
            update['progress'] = .05 + .55 * min(1, max(0, update['progress']))
        event(update)

    with service._backend_lock:
        _check_cancel(cancel)
        snapshot = service._backend().execute('review_collect', {'course': course}, on_event=collecting, cancel=cancel)
        if (snapshot.get('status') == 'needs_input' and '登录' in str(snapshot.get('message', ''))
                and (sync := service._sync_session(event, cancel)).get('ok') is True):
            snapshot = service._backend().execute('review_collect', {'course': course}, on_event=collecting, cancel=cancel)
    _check_cancel(cancel)
    if snapshot.get('ok') is not True:
        return {'ok': False, 'status': snapshot.get('status', 'error'),
                'message': snapshot.get('message') or '学习通课程读取失败。'}
    event({'message': '正在整理章节、作业和课程资料', 'progress': .63})
    try:
        review = build_review(service._clean(snapshot), summarize=ai.summarize_review, progress=event, cancel=cancel)
    except ReviewNeedsInput as exc:
        return {'ok': False, 'status': 'needs_input', 'message': str(exc)}
    except (AIError, ValueError) as exc:
        _check_cancel(cancel)
        return {'ok': False, 'status': 'needs_input', 'message': f'AI 整理未完成，未生成文件。{exc}'}
    _check_cancel(cancel)
    event({'message': '正在生成 Word 和 PDF', 'progress': .9})
    files = export_review(review, destination, cancel=cancel)
    partial = snapshot.get('status') == 'partial' or bool(review['issues'])
    return {'ok': True, 'status': 'partial' if partial else 'success', 'files': files,
            'counts': review['counts'], 'issues': review['issues'], 'summary_mode': review['summary_mode'],
            'message': 'Word 和 PDF 已生成，部分内容请查看读取说明。' if partial else 'Word 和 PDF 已生成。'}
