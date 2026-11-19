"""Course selection, export progress and local links for review guides."""
from pathlib import Path

from gi.repository import Gio, GLib, Gtk

from .workspace import STATUS_NAMES, label


class ReviewPanel:
    def __init__(self, window, page):
        self.window = window
        self.job_id = None
        self.busy = False
        self.files = {}

        settings = window._panel(page, '整理课程资料')
        self.course_select = window._course_selector(settings)
        self.course_select.connect('changed', self._refresh_controls)
        settings.pack_start(label('保存位置', 'subtitle'), False, False, 0)
        self.folder = Gtk.FileChooserButton(title='选择复习资料保存位置', action=Gtk.FileChooserAction.SELECT_FOLDER)
        self.folder.set_width_chars(22)
        self.folder.set_hexpand(True)
        settings.pack_start(self.folder, False, False, 0)
        document_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
        default_dir = Path(document_dir) if document_dir else Path.home()
        self.folder.set_filename(str(default_dir if default_dir.is_dir() else Path.home()))
        self.folder.connect('file-set', self._refresh_controls)

        settings.pack_start(label('生成时会将所选课程的章节、作业和资料发送给已配置的 AI。', 'subtitle'), False, False, 0)

        row = window._row(settings)
        self.export_button = window._button(row, '用 AI 生成 Word 和 PDF', self.start, primary=True)
        window._button(row, 'AI 设置', lambda _: self.open_ai_settings())
        self.cancel_button = window._button(row, '停止', self.cancel)
        results = window._panel(page, '导出结果')
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_text('尚未开始')
        results.pack_start(self.progress, False, False, 0)
        self.notice = label('先刷新或同步课程，再选择要复习的课程。', 'subtitle')
        self.notice.set_max_width_chars(72)
        results.pack_start(self.notice, False, False, 0)
        self.coverage = label('', 'subtitle')
        self.coverage.set_max_width_chars(72)
        results.pack_start(self.coverage, False, False, 0)
        row = window._row(results)
        self.file_buttons = {
            kind: window._button(row, caption, lambda _, file_kind=kind: self.open_file(file_kind))
            for kind, caption in (('docx', '打开 Word'), ('pdf', '打开 PDF'))
        }
        self.issues = Gtk.Expander(label='读取详情')
        self.issues_text = label('', 'subtitle')
        self.issues_text.set_max_width_chars(72)
        self.issues.add(self.issues_text)
        results.pack_start(self.issues, False, False, 0)
        self.issues.set_no_show_all(True)
        self._refresh_controls()

    def open_ai_settings(self):
        self.window.settings_sections.set_visible_child_name('ai')
        self.window.stack.set_visible_child_name('settings')

    def _refresh_controls(self, *_):
        self.export_button.set_sensitive(not self.busy and self.course_select.get_active() >= 0 and bool(self.folder.get_filename()))
        self.course_select.set_sensitive(not self.busy)
        self.folder.set_sensitive(not self.busy)
        self.cancel_button.set_sensitive(self.busy and bool(self.job_id))
        for kind, button in self.file_buttons.items():
            button.set_sensitive(kind in self.files)
            button.set_tooltip_text(str(self.files[kind]) if kind in self.files else None)

    def start(self, *_):
        if self.busy:
            return
        course = self.window._selected_course(self.course_select)
        folder = self.folder.get_filename()
        if not course or not folder:
            self.notice.set_text('请先选择课程和保存位置。')
            return
        self.busy = True
        self.files.clear()
        self.coverage.set_text('')
        self.issues.hide()
        self.issues.set_expanded(False)
        self.progress.set_fraction(0)
        self.progress.set_text('正在创建任务')
        self.notice.set_text('正在准备读取课程…')
        self._refresh_controls()
        self.window._start('review_export', {
            'course': course, 'output_dir': folder,
        }, done=self.finished)

    def started(self, job_id):
        self.job_id = job_id
        self.progress.set_text('等待执行')
        self._refresh_controls()

    def update_progress(self, state, fraction, message):
        self.progress.set_fraction(fraction)
        self.progress.set_text(f'{STATUS_NAMES.get(state, state)} · {fraction:.0%}')
        if message:
            self.notice.set_text(message)
        if state == 'cancelling':
            self.cancel_button.set_sensitive(False)

    def cancel(self, *_):
        job_id = self.job_id
        if not job_id:
            return
        self.cancel_button.set_sensitive(False)

        def failed(message):
            if self.job_id == job_id:
                self.notice.set_text('未能停止任务：' + message)
                self._refresh_controls()

        self.window._async(lambda: self.window.service.cancel(job_id), lambda _: self.window._poll(), failed)

    def finished(self, result):
        self.busy = False
        self.job_id = None
        if not isinstance(result, dict):
            result = {}
        self.files.clear()
        files = result.get('files') or {}
        ok = result.get('ok') is True
        if ok:
            for kind in self.file_buttons:
                value = files.get(kind) if isinstance(files, dict) else None
                if isinstance(value, str):
                    path = Path(value)
                    if path.is_absolute() and path.suffix.lower() == '.' + kind:
                        self.files[kind] = path

        has_outputs = ok and len(self.files) == len(self.file_buttons)
        state = result.get('status') or ('success' if has_outputs else 'error')
        if ok and not has_outputs:
            state = 'error'
        self.progress.set_text(STATUS_NAMES.get(state, str(state)))
        if has_outputs:
            self.progress.set_fraction(1)
        message = result.get('message') or result.get('error')
        if ok and not has_outputs:
            message = '返回的文档路径不完整，请检查保存位置。'
        self.notice.set_text(str(message or ('Word 和 PDF 已保存。' if has_outputs else '导出未完成。')))

        counts = result.get('counts')
        if isinstance(counts, dict):
            coverage = []
            for key, name in (('chapters', '章节'), ('assignments', '作业'), ('materials', '资料')):
                count = counts.get(key)
                if type(count) is int and count >= 0:
                    coverage.append(f'{name} {count} 项')
            mode = {'ai': 'AI 整理', 'local': '本机整理', 'mixed': 'AI 与本机整理'}.get(result.get('summary_mode'))
            if mode:
                coverage.append(mode)
            self.coverage.set_text('　·　'.join(coverage))

        issues = result.get('issues')
        lines = [item for item in issues if isinstance(item, str) and item.strip()] if isinstance(issues, list) else []
        if lines:
            self.issues.set_label(f'读取详情（{len(lines)} 项）')
            self.issues_text.set_text('\n'.join('• ' + item for item in lines))
            self.issues_text.show()
            self.issues.set_expanded(True)
            self.issues.show()
        self._refresh_controls()

    def open_file(self, kind):
        path = self.files.get(kind)
        if not path:
            return

        def launch():
            if not path.is_file():
                raise FileNotFoundError('文档已移动或删除，请核对保存位置。')
            if not Gio.AppInfo.launch_default_for_uri(path.as_uri(), None):
                raise RuntimeError('系统找不到能打开该格式的应用。')

        def failed(message):
            self.window._message('无法打开文档：' + message)

        self.window._async(launch, lambda _: None, failed)
