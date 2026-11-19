"""Desktop workspace for course, assignment, attendance and phone services.

Widgets never perform network or ADB work on the GTK main thread. AppService is
injected for previews/tests and imported only when a real window is constructed.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

CSS = b"""
window { background: #f3f6fb; color: #18304b; }
.sidebar { background: #15283f; padding: 24px 14px; }
.brand { color: #ffffff; font-size: 25px; font-weight: bold; }
.side-note { color: #a6b8ce; }
.sidebar button { background: transparent; color: #d8e4f4; border: 0; padding: 13px; box-shadow: none; }
.sidebar button:hover { background: #284664; }
.content { padding: 24px; }
.title { font-size: 25px; font-weight: bold; }
.subtitle { color: #61758b; }
.result-success { color: #1d7047; }
.result-error { color: #b23232; }
.source-link, .source-link label { color: #1c5cbb; }
.card { background: #ffffff; border: 1px solid #dfE7f1; border-radius: 10px; padding: 18px; }
.section-title { font-size: 17px; font-weight: bold; }
button { background-image: none; background-color: #ffffff; color: #18304b; padding: 8px 13px; border-radius: 6px; box-shadow: none; }
.primary { background: #2868cf; color: white; border-color: #2868cf; }
button:disabled, .primary:disabled { background-color: #eef1f5; color: #667282; border-color: #d4dce6; }
.status { background: #e6eefb; padding: 10px 16px; }
textview, textview text { background: #ffffff; color: #18304b; }
entry { background-image: none; background-color: #ffffff; color: #18304b; padding: 7px; }
treeview.view { background-color: #ffffff; color: #18304b; }
progressbar text { color: #18304b; }
spinbutton, spinbutton entry { color: #18304b; background-color: #ffffff; }
/* Popup menus are separate GTK surfaces and need their own theme rules. */
combobox, combobox button, combobox button:hover, combobox button:checked,
combobox button:active, combobox button:disabled, combobox cellview,
combobox cellview:selected, combobox cellview:disabled {
    background-image: none; background-color: #ffffff; color: #111111;
}
combobox button:disabled, combobox cellview:disabled { color: #596575; }
combobox arrow { color: #111111; }
menu, menuitem, menuitem label, .menu, .menuitem, popover, popover.background,
menuitem:hover, menuitem:selected, menuitem:checked,
menuitem:hover label, menuitem:selected label, menuitem:checked label {
    background-image: none; background-color: #ffffff; color: #111111;
    text-shadow: none;
}
menuitem:hover, menuitem:selected, menuitem:checked { box-shadow: inset 3px 0 #2868cf; }
menuitem:disabled, menuitem:disabled label { background-color: #ffffff; color: #596575; }
popover modelbutton, popover modelbutton label, popover modelbutton:hover,
popover modelbutton:selected, popover modelbutton:checked {
    background-image: none; background-color: #ffffff; color: #111111;
}
"""
def constrain_combo(combo):
    for cell in combo.get_cells():
        if isinstance(cell, Gtk.CellRendererText):
            cell.set_property('ellipsize', Pango.EllipsizeMode.END)
            cell.set_property('max-width-chars', 42)
    return combo


TERMINAL = {'done', 'completed', 'success', 'succeeded', 'error', 'failed', 'cancelled', 'canceled', 'stopped', 'needs_input', 'partial', 'timeout', 'interrupted', 'saved', 'reported'}



STATUS_NAMES = {
    'queued': '等待执行', 'running': '执行中', 'cancelling': '正在停止',
    'pending': '待执行', 'completed': '已完成', 'done': '已完成',
    'success': '成功', 'succeeded': '成功', 'error': '失败', 'failed': '失败',
    'locked': '未解锁', 'not_open': '未开放', 'unsupported': '需手机处理',
    'reported': '请求已接受，未核对完成', 'verified': '已核对完成',
    'needs_input': '需补充信息', 'partial': '部分完成', 'saved': '已保存草稿',
    'not_selected': '未选择', 'cancelled': '已停止', 'canceled': '已停止',
    'interrupted': '已中断', 'timeout': '已超时', 'stopped': '已停止',
}
TYPE_NAMES = {'chapter': '章节', 'video': '视频', 'audio': '音频',
              'document': '文档', 'read': '阅读', 'quiz': '章节测验', 'workid': '章节测验'}
ACTION_NAMES = {'course_plan': '读取课程计划', 'course_run': '后台执行课程',
                'courses': '读取课程', 'work_list': '读取作业列表', 'session_sync': '同步学习通登录状态',
                'work_load': '读取作业', 'work_solve': 'AI 解答',
                'work_complete': '一键完成作业', 'work_submit': '保存 / 提交作业',
                'review_export': '生成复习资料',
                'sign': '签到', 'login': '账号登录', 'ai_test': '测试 AI 连接',
                'chatgpt_sign_in': 'ChatGPT 账号授权',
                'install_backend': '安装课程组件', 'phone_open': '打开手机窗口'}


def human_message(message):
    return re.sub(r'\b(' + '|'.join(STATUS_NAMES) + r')\b',
                  lambda match: STATUS_NAMES[match.group(0)], str(message))


def progress_value(value):
    try:
        number = float(value)
        return max(0, min(1, number / 100 if number > 1 else number))
    except (ValueError, TypeError):
        return 0


def pretty(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)


def label(text='', style=None, wrap=True):
    item = Gtk.Label(label=text, xalign=0)
    item.set_line_wrap(wrap)
    item.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    item.set_selectable(style not in ('brand', 'side-note', 'title', 'section-title'))
    if style:
        item.get_style_context().add_class(style)
    return item


def textbox(height=160, editable=False):
    view = Gtk.TextView(editable=editable, wrap_mode=Gtk.WrapMode.WORD_CHAR)
    view.set_left_margin(10)
    view.set_right_margin(10)
    view.set_top_margin(8)
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_min_content_height(height)
    scroll.add(view)
    return view, scroll


def put(view, value):
    view.get_buffer().set_text(pretty(value))


def contents(view):
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)


class Window(Gtk.Window):
    def __init__(self, service=None, smoke_path=None):
        from . import __version__
        super().__init__(title=f'学习通 Linux · 工作台 {__version__}')
        if service is None:
            from .service import AppService
            service = AppService()
        self.service = service
        self.smoke_path = smoke_path
        self.closed = False
        self.closing = False
        self._shutdown_complete = False
        self._shutdown_lock = threading.Lock()
        self.courses = []
        self.devices = []
        self.activity_items = []
        self.work_data = {}
        self.job_callbacks = {}
        self.seen_jobs = {}
        self.polling = False
        self.phone_loading = False
        self.phone_size = None
        self.phone_frame_serial = None
        self.course_job_id = None
        self.course_points = {}
        self.set_default_size(1240, 880)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect('destroy', self._destroy)
        self.connect('delete-event', self._close_request)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        body = Gtk.Box()
        outer.pack_start(body, True, True, 0)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        side.get_style_context().add_class('sidebar')
        side.set_size_request(190, -1)
        body.pack_start(side, False, False, 0)
        side.pack_start(label('学习通', 'brand'), False, False, 8)
        side.pack_start(label('LINUX 工作台', 'side-note'), False, False, 0)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        body.pack_start(self.stack, True, True, 0)
        self.pages = {}
        for name, title in [('home', '概览'), ('courses', '课程后台'), ('work', '作业 AI'), ('review', '复习资料'), ('sign', '签到'), ('phone', '手机控制'), ('settings', '设置'), ('jobs', '任务记录')]:
            button = Gtk.Button(label=title)
            button.connect('clicked', lambda _, n=name: self.stack.set_visible_child_name(n))
            side.pack_start(button, False, False, 0)
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            page.get_style_context().add_class('content')
            scroll.add(page)
            self.stack.add_named(scroll, name)
            self.pages[name] = page
            page.pack_start(label(title, 'title'), False, False, 0)
        side.pack_end(label('课程、作业与手机\n在一个窗口中管理', 'side-note'), False, False, 8)
        self.status = label('就绪。先在设置同步学习通登录状态并配置 AI，或打开手机控制。', 'status')
        self.status.set_lines(2)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_max_width_chars(110)
        outer.pack_end(self.status, False, False, 0)
        self._home()
        self._courses()
        self._work()
        from .review_ui import ReviewPanel
        self.review_panel = ReviewPanel(self, self.pages['review'])
        self._sign()
        self._phone()
        self._settings()
        self._jobs()
        self.stack.set_visible_child_name('home')
        self.show_all()
        self._sign_mode_changed()
        self.stack.connect('notify::visible-child-name', self._sign_page_changed)
        GLib.timeout_add(1500, self._poll)
        GLib.timeout_add(2500, self._phone_tick)
        if smoke_path:
            GLib.timeout_add(900, self._screenshot)

    def _shutdown_service(self):
        # Main-loop teardown and delete-event can both reach here. Shut down once.
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            shutdown = getattr(self.service, 'shutdown', None)
            try:
                if shutdown:
                    shutdown(timeout=5)
            finally:
                self._shutdown_complete = True

    def _close_request(self, *_):
        if not self.closing:
            self.closing = True
            self._message('正在结束后台任务并关闭，请稍候…')
            self.set_sensitive(False)
            def finish():
                try:
                    self._shutdown_service()
                finally:
                    GLib.idle_add(self.destroy)
            threading.Thread(target=finish, daemon=True, name='workspace-shutdown').start()
        return True

    def _destroy(self, *_):
        self.closed = True
        try:
            self._shutdown_service()
        finally:
            if Gtk.main_level():
                Gtk.main_quit()

    def _message(self, text):
        if not self.closed:
            self.status.set_text(str(text))
            self.status.set_tooltip_text(str(text))

    def _async(self, fn, done=None, error=None):
        def deliver(callback, value):
            if not self.closed:
                callback(value)
            return False
        def run():
            try:
                result = fn()
            except Exception as exc:
                GLib.idle_add(deliver, error or (lambda err: self._message('操作未完成：' + str(err))), str(exc))
            else:
                GLib.idle_add(deliver, done or (lambda _: self._message('操作完成。')), result)
        threading.Thread(target=run, daemon=True).start()

    def _request(self, action, payload=None, done=None, button=None):
        if button:
            button.set_sensitive(False)
        self._message('正在处理，请稍候…')
        def finish(value):
            if button:
                button.set_sensitive(True)
            if isinstance(value, dict) and value.get('ok') is False:
                self._message(value.get('message') or '操作未完成，请查看服务配置及登录状态。')
                return
            self._message('操作完成。')
            if done:
                done(value)
        def failure(message):
            if button:
                button.set_sensitive(True)
            self._message('操作未完成：' + message)
        self._async(lambda: self.service.request(action, payload or {}), finish, failure)

    def _start(self, action, payload=None, done=None, output=None):
        def started(job):
            ident = str(job.get('id', job.get('job_id', ''))) if isinstance(job, dict) else ''
            if action == 'ai_test' and not ident:
                self._ai_test_result(job if isinstance(job, dict) else {'ok': False, 'error': '任务未能创建。'})
                return
            if action in ('review_export', 'chatgpt_sign_in') and not ident:
                message = job.get('message') or job.get('error') if isinstance(job, dict) else ''
                callback = self.review_panel.finished if action == 'review_export' else self._chatgpt_sign_in_finished
                callback({'ok': False, 'message': message or '任务未能创建。'})
                return
            if ident:
                self.job_callbacks[ident] = (done, output)
                if action == 'review_export':
                    self.review_panel.started(ident)
                if action == 'chatgpt_sign_in':
                    self.chatgpt_job_id = ident
                    self.chatgpt_cancel.set_sensitive(True)
                if action in ('course_plan', 'course_run'):
                    self.course_job_id = ident
                    self.course_progress.set_fraction(0)
                    self.course_progress.set_text('等待执行')
                    self.course_notice.set_text('任务已加入后台，正在等待执行。')
            if output:
                put(output, job)
            self._message('任务已加入后台，进度可在任务记录中查看。')
            self._poll()
        def failure(message):
            self._message('任务无法开始：' + str(message))
            if action == 'sign':
                self._sign_result({'ok': False, 'status': 'error', 'message': str(message)})
            elif action == 'ai_test':
                self._ai_test_result({'ok': False, 'status': 'error', 'message': str(message)})
            elif action == 'review_export':
                self.review_panel.finished({'ok': False, 'message': str(message)})
            elif action == 'chatgpt_sign_in':
                self._chatgpt_sign_in_finished({'ok': False, 'message': str(message)})
        self._async(lambda: self.service.start(action, payload or {}), started, failure)

    def _button(self, box, text, callback, primary=False):
        button = Gtk.Button(label=text)
        if primary:
            button.get_style_context().add_class('primary')
        button.connect('clicked', callback)
        box.pack_start(button, False, False, 0)
        return button

    def _row(self, page):
        row = Gtk.Box(spacing=8)
        page.pack_start(row, False, False, 0)
        return row

    def _entry(self, page, title, placeholder='', secret=False):
        page.pack_start(label(title, 'subtitle'), False, False, 0)
        entry = Gtk.Entry()
        entry.set_placeholder_text(placeholder)
        entry.set_visibility(not secret)
        page.pack_start(entry, False, False, 0)
        return entry

    def _home(self):
        page = self.pages['home']
        page.pack_start(label('在桌面管理课程、作业、复习资料、签到和手机。', 'section-title'), False, False, 0)
        page.pack_start(label('账号服务复用可读取的本机学习通已登录网页会话：先在设置同步登录状态。手机控制操作已通过 USB 调试授权的设备，连接手机不会自动复制应用私有登录状态。', 'subtitle'), False, False, 0)
        for title, text, target in [
            ('课程后台', '读取课程、查看待完成内容、生成执行计划，并在后台跟踪任务进度。', 'courses'),
            ('作业 AI', '选择课程并读取作业列表，选定作业后读题，查看并编辑答案，再保存或提交。', 'work'),
            ('复习资料', '整理课程章节、作业与课程资料，导出 Word 和 PDF。', 'review'),
            ('签到', '读取课程活动并按活动要求提供签到码、位置或二维码信息；结果以服务返回为准。', 'sign'),
            ('手机控制', '在桌面查看手机画面、点击、输入文字，或打开独立手机控制窗口。', 'phone'),
        ]:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.get_style_context().add_class('card')
            page.pack_start(card, False, False, 0)
            card.pack_start(label(title, 'section-title'), False, False, 0)
            card.pack_start(label(text, 'subtitle'), False, False, 0)
            self._button(card, '打开' + title, lambda _, n=target: self.stack.set_visible_child_name(n))
        row = self._row(page)
        self._button(row, '前往设置', lambda _: self.stack.set_visible_child_name('settings'), True)
        self._button(row, '旧兼容环境启动器', lambda _: subprocess.Popen([sys.executable, '-m', 'chaoxing_linux', '--legacy-launcher']))

    def _course_selector(self, page):
        selector = constrain_combo(Gtk.ComboBoxText())
        selector.set_hexpand(True)
        row = self._row(page)
        row.pack_start(selector, True, True, 0)
        self._button(row, '刷新课程', lambda button: self._request('courses', done=self._courses_loaded, button=button))
        return selector

    def _courses(self):
        page = self.pages['courses']
        page.pack_start(label('先读取课程，再查看执行计划。任务在后台运行，关闭进度页不会终止任务。', 'subtitle'), False, False, 0)
        self.course_select = self._course_selector(page)
        row = self._row(page)
        self._button(row, '查看课程计划', lambda _: self._course_action('course_plan'), True)
        row.pack_start(label('播放速度'), False, False, 0)
        self.course_speed = Gtk.SpinButton.new_with_range(1.0, 2.0, 0.1)
        self.course_speed.set_value(1.0)
        row.pack_start(self.course_speed, False, False, 0)
        self._button(row, '后台执行课程', lambda _: self._course_action('course_run'))
        row = self._row(page)
        self.solve_quizzes = Gtk.CheckButton(label='AI 处理章节测验')
        self.submit_quizzes = Gtk.CheckButton(label='正式提交章节测验答案')
        row.pack_start(self.solve_quizzes, False, False, 0)
        row.pack_start(self.submit_quizzes, False, False, 0)
        self.course_notice = label('尚未读取课程计划。', 'subtitle')
        page.pack_start(self.course_notice, False, False, 0)
        self.course_progress = Gtk.ProgressBar(show_text=True)
        self.course_progress.set_text('尚未开始')
        page.pack_start(self.course_progress, False, False, 0)
        self.course_store = Gtk.ListStore(str, str, str, str)
        self.course_tree = Gtk.TreeView(model=self.course_store)
        self.course_tree.set_tooltip_column(3)
        for index, title in enumerate(('章节 / 任务名称', '类型', '状态', '说明')):
            renderer = Gtk.CellRendererText()
            renderer.set_property('ellipsize', Pango.EllipsizeMode.END)
            renderer.set_property('max-width-chars', (45, 10, 25, 45)[index])
            renderer.set_property('ypad', 6)
            renderer.set_property('height', 36)
            renderer.set_property('yalign', 0.5)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_min_width((220, 80, 205, 160)[index])
            column.set_expand(index in (0, 3))
            self.course_tree.append_column(column)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(250)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.course_tree)
        page.pack_start(scroll, True, True, 0)
        page.pack_start(label('执行动态', 'section-title'), False, False, 0)
        self.course_events, scroll = textbox(95)
        page.pack_start(scroll, False, False, 0)
        put(self.course_events, '任务开始后将在这里显示中文进度。')
        details = Gtk.Expander(label='详情 · 原始返回数据')
        self.course_output, scroll = textbox(200)
        details.add(scroll)
        page.pack_start(details, False, False, 0)

    def _courses_loaded(self, result):
        self.courses = result.get('courses', []) if isinstance(result, dict) else result
        for selector in (self.course_select, self.sign_course, self.work_course, self.review_panel.course_select):
            selector.remove_all()
            for course in self.courses:
                selector.append_text(str(course.get('name', course.get('id', '未命名课程'))))
            if self.courses:
                selector.set_active(0)
        self._message('已读取 %d 门课程。' % len(self.courses))

    def _selected_course(self, selector):
        index = selector.get_active()
        if index < 0 or index >= len(self.courses):
            self._message('请先刷新课程或同步学习通登录状态，再选择课程。')
            return None
        return self.courses[index]

    def _course_action(self, action):
        course = self._selected_course(self.course_select)
        if course:
            self._start(action, {'course': course, 'speed': self.course_speed.get_value(), 'solve_quizzes': self.solve_quizzes.get_active(), 'submit_quizzes': self.submit_quizzes.get_active()}, done=self._course_result, output=self.course_output)

    def _course_result(self, data):
        if not isinstance(data, dict):
            return
        tasks = data.get('tasks', data.get('results', []))
        self.course_store.clear()
        for task in tasks:
            ident = str(task.get('id', ''))
            point = task.get('point') or {}
            chapter = str(point.get('title', '')) if isinstance(point, dict) else str(point)
            if chapter:
                self.course_points[ident] = chapter
            chapter = chapter or self.course_points.get(ident, '')
            name = str(task.get('name') or ident or '未命名任务')
            title = chapter + ' / ' + name if chapter and chapter != name else name
            state = str(task.get('status', 'pending'))
            status = STATUS_NAMES.get(state, '待确认')
            if task.get('verified') is True:
                status = '已核对完成'
            note = str(task.get('message') or '')
            if state == 'reported' and not task.get('verified'):
                note = note or '请求已接受，未核对完成；请查看官方课程状态。'
            elif state == 'locked':
                note = note or '需先完成前置章节。'
            elif state == 'not_open':
                note = note or '请等待课程开放。'
            elif state == 'unsupported':
                note = note or '请在手机官方应用中处理此任务。'
            self.course_store.append([title, TYPE_NAMES.get(str(task.get('type')), '其他任务'), status, human_message(note)])
        message = data.get('message') or ('共 %d 项任务。' % len(tasks))
        self.course_notice.set_text(human_message(message))
        self.course_progress.set_fraction(1)
        self.course_progress.set_text('已读取计划' if 'tasks' in data else STATUS_NAMES.get(str(data.get('status')), '执行已结束'))
        put(self.course_output, data)

    def _work(self):
        page = self.pages['work']
        self.work_items = []
        self.work_generation = 0
        self.work_course_generation = 0
        self.work_list_generation = 0
        self.work_image_cache_root = None
        self.work_image_errors = []
        page.pack_start(label('选择课程 → 读取作业列表 → 选择作业 → 读取题目。答案可编辑，保存与正式提交分别操作。', 'subtitle'), False, False, 0)
        self.work_course = self._course_selector(page)
        self.work_course.connect('changed', self._work_course_changed)
        row = self._row(page)
        self.work_list_button = self._button(row, '读取作业列表', self._load_work_list)
        self.work_select = constrain_combo(Gtk.ComboBoxText())
        self.work_select.set_hexpand(True)
        self.work_select.connect('changed', self._work_selection_changed)
        row.pack_start(self.work_select, True, True, 0)
        self.work_list_note = label('先同步登录状态或刷新课程，再读取所选课程的作业。', 'subtitle')
        page.pack_start(self.work_list_note, False, False, 0)
        self.work_link_expander = Gtk.Expander(label='高级 · 手动填写作业链接')
        advanced = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.work_url = self._entry(advanced, '作业链接', '选作业后自动填写，也可手动粘贴学习通作业链接')
        self.work_url.connect('changed', self._work_url_changed)
        self.work_link_expander.add(advanced)
        page.pack_start(self.work_link_expander, False, False, 0)
        row = self._row(page)
        self.work_search = Gtk.CheckButton(label='允许模型联网')
        self.work_search.set_active(True)
        row.pack_start(self.work_search, False, False, 0)
        self.work_submit_check = Gtk.CheckButton(label='一键完成后正式提交（未选则保存草稿）')
        row.pack_start(self.work_submit_check, False, False, 0)
        page.pack_start(label('允许不代表每题都会搜索，以实际调用和来源为准。联网方式在设置中选择。', 'subtitle'), False, False, 0)
        row = self._row(page)
        self.work_read_button = self._button(row, '读取题目', lambda _: self._work_action('work_load'))
        self.work_solve_button = self._button(row, 'AI 解答', lambda _: self._work_action('work_solve'))
        self.work_complete_button = self._button(row, '一键完成', lambda _: self._work_action('work_complete'), True)
        self.work_page_button = self._button(row, '查看官方作业页', self._open_work_page)
        self.work_note = label('尚未读取作业。', 'subtitle')
        page.pack_start(self.work_note, False, False, 0)
        self.work_image_notice = label('读取题目后显示题图载入状态。', 'subtitle')
        self.work_image_notice.set_max_width_chars(90)
        page.pack_start(self.work_image_notice, False, False, 0)
        self.answer_store = Gtk.ListStore(str, str, str)
        tree = Gtk.TreeView(model=self.answer_store)
        for index, title in enumerate(('题号', '题目', '答案（双击编辑）')):
            renderer = Gtk.CellRendererText()
            renderer.set_property('wrap-width', 440 if index == 1 else 280)
            renderer.set_property('wrap-mode', 2)
            renderer.set_property('yalign', 0.0)
            if index == 2:
                renderer.set_property('editable', True)
                renderer.connect('edited', self._answer_edited)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            tree.append_column(column)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(220)
        scroll.add(tree)
        page.pack_start(scroll, True, True, 0)
        self.work_images = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.pack_start(self.work_images, False, False, 0)
        row = self._row(page)
        self.work_save_button = self._button(row, '保存草稿', lambda _: self._submit_work('save'))
        self.work_submit_button = self._button(row, '正式提交当前答案', lambda _: self._submit_work('submit'), True)
        self._button(row, '编辑完整作业数据', self._edit_work)
        page.pack_start(label('按题号查看联网状态与来源', 'section-title'), False, False, 0)
        self.work_sources = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.pack_start(self.work_sources, False, False, 0)
        self.work_sources.pack_start(label('读取或解题后显示每题的实际联网状态与可点击来源。', 'subtitle'), False, False, 0)
        raw = Gtk.Expander(label='详情 · 原始任务数据')
        self.work_output, scroll = textbox(160)
        raw.add(scroll)
        page.pack_start(raw, False, False, 0)
        self._refresh_work_actions()

    def _reset_work_data(self, message='尚未读取题目。'):
        self.work_generation += 1
        self.work_data = {}
        if hasattr(self, 'answer_store'):
            self.answer_store.clear()
        if hasattr(self, 'work_note'):
            self.work_note.set_text(message)
        if hasattr(self, 'work_output'):
            put(self.work_output, '')
        if hasattr(self, 'work_sources'):
            self._render_work_sources()
        if hasattr(self, 'work_images'):
            self._render_work_images()
        self._refresh_work_actions()

    def _selected_work(self):
        index = self.work_select.get_active()
        if 0 <= index < len(self.work_items):
            item = self.work_items[index]
            if str(item.get('url') or '').strip() == self.work_url.get_text().strip():
                return item
        return {}

    def _work_can_write(self):
        return self._selected_work().get('actionable') is not False

    def _refresh_work_actions(self):
        if not hasattr(self, 'work_submit_button'):
            return
        has_url = bool(self.work_url.get_text().strip())
        writable = has_url and self._work_can_write() and not self._work_has_image_failures()
        self.work_read_button.set_sensitive(has_url)
        self.work_solve_button.set_sensitive(has_url)
        self.work_page_button.set_sensitive(has_url)
        self.work_complete_button.set_sensitive(writable)
        has_questions = bool(self.work_data.get('questions'))
        self.work_save_button.set_sensitive(writable and has_questions)
        self.work_submit_button.set_sensitive(writable and has_questions)

    def _open_work_page(self, *_):
        url = self.work_url.get_text().strip()
        if urlsplit(url).scheme not in ('http', 'https'):
            self.work_note.set_text('当前作业未提供有效网页链接。')
            return
        self._async(lambda: webbrowser.open(url), lambda opened: self._message('已请求系统浏览器打开当前作业页面。' if opened else '系统浏览器未能打开，请展开高级链接复制到浏览器。'))

    def _work_course_changed(self, *_):
        self.work_course_generation += 1
        self.work_list_generation += 1
        self.work_items = []
        self.work_select.remove_all()
        self.work_url.set_text('')
        self._reset_work_data('已切换课程，请读取作业列表。')
        self.work_list_note.set_text('已切换课程，请读取该课程的作业列表。')

    @staticmethod
    def _work_status(item):
        value = item.get('status_text') or item.get('status_name') or item.get('status')
        if value is None or str(value).strip() == '':
            return '状态未提供'
        # Numeric values are backend-specific; do not guess completed/submitted.
        names = {'not_submitted': '未提交', 'submitted': '已提交', 'graded': '已批阅',
                 'not_started': '未开始', 'in_progress': '作答中', 'expired': '已截止',
                 'ended': '已结束', 'draft': '草稿', 'open': '可作答'}
        text = str(value)
        return names.get(text, STATUS_NAMES.get(text, text))

    def _load_work_list(self, *_):
        course = self._selected_course(self.work_course)
        if not course:
            self.work_list_note.set_text('请先同步登录状态或刷新课程，再选择课程。')
            return
        self.work_list_generation += 1
        token = (self.work_course_generation, self.work_list_generation)
        self.work_list_note.set_text('正在读取所选课程的作业列表…')
        def loaded(data):
            if token != (self.work_course_generation, self.work_list_generation):
                return
            self._works_loaded(data)
        def failed(message):
            if token == (self.work_course_generation, self.work_list_generation):
                self.work_list_note.set_text('作业列表读取失败：' + str(message))
        self._async(lambda: self.service.request('work_list', {'course': course}), loaded, failed)

    def _works_loaded(self, data):
        if not isinstance(data, dict) or data.get('ok') is False:
            self.work_list_note.set_text(str(data.get('message') or '作业列表未能读取。') if isinstance(data, dict) else '作业列表格式无法识别。')
            return
        self.work_items = [item for item in data.get('works', []) if isinstance(item, dict)]
        self.work_select.remove_all()
        self.work_url.set_text('')
        self._reset_work_data()
        for item in self.work_items:
            title = str(item.get('title') or item.get('name') or item.get('id') or '未命名作业')
            self.work_select.append_text(title + ' · ' + self._work_status(item))
        if self.work_items:
            self.work_select.set_active(0)
        else:
            self.work_list_note.set_text(data.get('message') or '该课程暂未读取到作业。可稍后重试，或展开高级入口使用作业链接。')

    def _work_selection_changed(self, *_):
        index = self.work_select.get_active()
        if not 0 <= index < len(self.work_items):
            return
        item = self.work_items[index]
        self._reset_work_data('已选择作业，请点击“读取题目”。')
        self.work_url.set_text(str(item.get('url') or ''))
        title = str(item.get('title') or item.get('name') or item.get('id') or '未命名作业')
        notes = []
        if not item.get('url'):
            notes.append('作业未提供可访问链接，请使用高级入口或在官方页面处理。')
        if item.get('requires_start'):
            notes.append('首次进入可能按学习通规则建立作答记录；选中不会进入，点击“读取题目”后才会打开。')
        if item.get('actionable') is False:
            notes.append('此项不可提交，已禁用一键完成、保存与提交；可查看官方原页面。')
        self.work_list_note.set_text('当前作业：%s · %s%s' % (title, self._work_status(item), ('\n' + '\n'.join(notes)) if notes else ''))
        self.work_note.set_text('已选择作业，请点击“读取题目”。')
        self._refresh_work_actions()

    def _work_url_changed(self, *_):
        self.work_list_generation += 1
        self._reset_work_data('作业目标已改变，请重新读取题目。')
        index = self.work_select.get_active()
        if 0 <= index < len(self.work_items) and str(self.work_items[index].get('url') or '').strip() != self.work_url.get_text().strip():
            self.work_select.set_active(-1)
            self.work_list_note.set_text('当前使用高级入口的手动链接；未沿用原作业题目。')

    def _work_result_callback(self):
        generation = self.work_generation
        url = self.work_url.get_text().strip()
        def loaded(data):
            if generation == self.work_generation and url == self.work_url.get_text().strip():
                self._work_loaded(data)
        return loaded

    def _work_action(self, action):
        url = self.work_url.get_text().strip()
        if not url:
            self.work_note.set_text('请先选择带链接的作业，再点击“读取题目”；也可使用高级链接入口。')
            self._message('请先选择作业。')
            return
        if action == 'work_complete' and self._work_has_image_failures():
            self.work_note.set_text('题图载入或发送不完整，已阻止一键完成。请重新读题或在官方页面处理。')
            return
        if action == 'work_complete' and not self._work_can_write():
            self.work_note.set_text('该作业不可提交，请查看官方作业页。没有执行一键完成。')
            return
        self._start(action, {'url': url, 'search': self.work_search.get_active(), 'submit': self.work_submit_check.get_active()}, self._work_result_callback())

    def _work_loaded(self, data):
        if not isinstance(data, dict):
            return
        # Submission results may omit questions; preserve the loaded editable data.
        if 'questions' in data or 'answers' in data:
            if 'questions' in data and 'answers' not in data:
                self.work_data['answers'] = []
            self.work_data.update(data)
            self._render_answers()
        self.work_note.set_text('已读取 %d 道题。可双击答案编辑；复杂题型可编辑完整作业数据。' % len(self.work_data.get('questions', [])))
        put(self.work_output, data)
        self._refresh_work_actions()

    def _render_answers(self):
        self.answer_store.clear()
        questions = self.work_data.get('questions', [])
        answers = self.work_data.get('answers', [])
        indexed = {str(a.get('id')): a for a in answers if isinstance(a, dict) and a.get('id') is not None} if isinstance(answers, list) else answers
        normalized = []
        for index, question in enumerate(questions):
            qid = str(question.get('id', question.get('question_id', index + 1))) if isinstance(question, dict) else str(index + 1)
            title = str(question.get('title') or question.get('text') or question.get('question') or '') if isinstance(question, dict) else str(question)
            if isinstance(question, dict):
                if not title.strip() and question.get('images'):
                    title = '【图片题：请查看下方题图预览】'
                options = question.get('options', [])
                title += ''.join('\n%s. %s' % (option.get('key', ''), option.get('text', '')) for option in options if isinstance(option, dict))
            answer = indexed.get(qid, '') if isinstance(indexed, dict) else ''
            record = dict(answer) if isinstance(answer, dict) else {'id': qid, 'answer': answer, 'status': 'ok' if answer else 'unknown'}
            record['id'] = qid
            normalized.append(record)
            value = record.get('answer')
            self.answer_store.append([qid, title, '' if value is None else pretty(value)])
        self.work_data['answers'] = normalized
        self._render_work_images()
        self._render_work_sources()
        self._refresh_work_actions()

    @staticmethod
    def _image_count(value):
        try:
            return max(0, int(value))
        except (ValueError, TypeError, OverflowError):
            return 0

    def _work_has_image_failures(self):
        if self.work_image_errors:
            return True
        for question in self.work_data.get('questions') or []:
            if not isinstance(question, dict):
                continue
            status = question.get('image_status') or {}
            if isinstance(status, dict) and status.get('state') in ('partial', 'failed', 'error'):
                return True
            images = question.get('images') or []
            if not isinstance(images, list):
                return True
            if isinstance(status, dict) and self._image_count(status.get('count')) > len(images):
                return True
            if any(not isinstance(image, dict) or image.get('status') != 'ready' for image in images):
                return True
        for answer in self.work_data.get('answers') or []:
            if not isinstance(answer, dict):
                continue
            status = answer.get('image_status') or {}
            if isinstance(status, dict):
                if status.get('state') in ('partial', 'failed', 'error', 'not_sent'):
                    return True
                if status.get('sent') is not None and self._image_count(status.get('sent')) < self._image_count(status.get('count')):
                    return True
        return False

    def _cached_question_image(self, image):
        from .question_images import normalize_images
        root = self.work_image_cache_root
        if root is None:
            raise ValueError('题图缓存目录不可用，请重新读取作业。')
        # The shared verifier requires the private assignment-images directory,
        # owned regular files, hash-named paths, matching MIME and SHA256.
        verified = normalize_images([image], data_dir=root.parent)[0]
        path = Path(verified['local_path'])
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            data = stream.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != verified['sha256']:
            raise ValueError('图片缓存校验失败，请重新读题。')
        image_format, width, height = GdkPixbuf.Pixbuf.get_file_info(str(path))
        if image_format is None or image_format.get_name() not in ('png', 'jpeg', 'gif', 'webp'):
            raise ValueError('图片缓存不是支持的位图格式。')
        if not 0 < width * height <= 20_000_000:
            raise ValueError('图片尺寸超过安全预览范围。')
        return data

    def _render_work_images(self):
        for child in self.work_images.get_children():
            self.work_images.remove(child)
        self.work_image_errors = []
        total = ready = 0
        for index, question in enumerate(self.work_data.get('questions') or [], 1):
            if not isinstance(question, dict):
                continue
            images = question.get('images') or []
            status = question.get('image_status') or {}
            if not isinstance(images, list):
                images = []
                self.work_image_errors.append('题图数据格式错误')
            declared = self._image_count(status.get('count')) if isinstance(status, dict) else 0
            count = max(len(images), declared)
            if not count and not (isinstance(status, dict) and status.get('state') in ('failed', 'partial', 'error')):
                continue
            total += count
            qid = str(question.get('id') or index)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            states = []
            successes = 0
            for number, image in enumerate(images, 1):
                image = image if isinstance(image, dict) else {'status': 'error', 'error': '题图数据格式错误'}
                caption = '选项 ' + str(image.get('option_key') or '?') if image.get('scope') == 'option' else '题干'
                caption += ' · ' + str(image.get('label') or ('图片 ' + str(number)))
                error = str(image.get('error') or '') if image.get('status') != 'ready' else ''
                if not error and image.get('status') == 'ready':
                    try:
                        self._cached_question_image(image)
                    except (OSError, ValueError, GLib.Error) as exc:
                        error = str(exc)
                    else:
                        successes += 1
                elif not error:
                    error = '图片尚未载入成功'
                if error:
                    self.work_image_errors.append('题目 %s：%s' % (qid, error))
                row = label(caption + (' · 载入失败：' + error if error else ' · 已缓存，可展开预览'))
                row.set_max_width_chars(85)
                body.pack_start(row, False, False, 0)
                if not error:
                    preview = Gtk.Image()
                    preview.set_halign(Gtk.Align.START)
                    body.pack_start(preview, False, False, 0)
                    states.append((dict(image), preview))
            ready += successes
            failed = max(0, count - successes)
            expander = Gtk.Expander(label='题目 %s · %d 图，成功 %d，失败 %d · 展开预览' % (qid, count, successes, failed))
            expander.add(body)
            token = self.work_generation
            def expand(widget, _, items=states, generation=token, loaded=[False]):
                if not widget.get_expanded() or loaded[0] or generation != self.work_generation:
                    return
                loaded[0] = True
                for metadata, preview in items:
                    try:
                        data = self._cached_question_image(metadata)
                        loader = GdkPixbuf.PixbufLoader.new()
                        def prepare(reader, width, height):
                            scale = min(420 / width, 240 / height, 1)
                            reader.set_size(max(1, int(width * scale)), max(1, int(height * scale)))
                        loader.connect('size-prepared', prepare)
                        loader.write(data)
                        loader.close()
                        picture = loader.get_pixbuf()
                        if picture is None:
                            raise ValueError('图片未能解码为预览。')
                        scale = min(420 / picture.get_width(), 240 / picture.get_height(), 1)
                        picture = picture.scale_simple(max(1, int(picture.get_width() * scale)), max(1, int(picture.get_height() * scale)), GdkPixbuf.InterpType.BILINEAR)
                        preview.set_from_pixbuf(picture)
                    except (OSError, ValueError, GLib.Error) as exc:
                        self.work_image_errors.append(str(exc))
                        preview.set_from_icon_name('image-missing', Gtk.IconSize.DIALOG)
                        preview.set_tooltip_text('图片预览失败：' + str(exc))
                        self.work_image_notice.set_text('图片预览失败，已阻止保存与提交。请重新读取作业。')
                        self._refresh_work_actions()
            expander.connect('notify::expanded', expand)
            self.work_images.pack_start(expander, False, False, 0)
        if not self.work_data.get('questions'):
            message = '读取题目后显示题图载入状态。'
        elif total:
            message = '题图：共 %d 张，成功载入 %d 张，失败 %d 张。' % (total, ready, max(0, total - ready))
        else:
            message = '本次读取的题目未声明图片。'
        if self._work_has_image_failures():
            message += ' 图片载入或发送不完整，已阻止一键完成、保存和提交。请重新读题或查看官方页。'
        self.work_image_notice.set_text(message)
        self.work_images.show_all()

    def _render_work_sources(self):
        for child in self.work_sources.get_children():
            self.work_sources.remove(child)
        answers = self.work_data.get('answers') or []
        if not isinstance(answers, list):
            answers = []
        status_names = {'ok': '已执行联网搜索', 'not_used': '模型未调用搜索', 'disabled': '未允许联网',
                        'failed': '联网失败', 'error': '联网失败', 'empty': '已搜索，无来源',
                        'not_requested': '未请求搜索', 'unsupported': '服务不支持此联网方式'}
        if not answers:
            self.work_sources.pack_start(label('尚无解题结果；联网是否发生以每题实际状态为准。', 'subtitle'), False, False, 0)
        for answer in answers:
            if not isinstance(answer, dict):
                continue
            search = answer.get('search_status') or {}
            if not isinstance(search, dict):
                search = {'state': str(search)}
            state = str(search.get('state') or '未提供')
            text = '题目 %s · %s [%s]' % (str(answer.get('id', '未提供')), status_names.get(state, '联网状态待确认'), state)
            if search.get('error'):
                text += '\n原因：' + str(search['error'])
            image_status = answer.get('image_status') or {}
            if isinstance(image_status, dict) and image_status:
                image_state = str(image_status.get('state') or '未提供')
                count = self._image_count(image_status.get('count'))
                sent = self._image_count(image_status.get('sent'))
                if image_state in ('failed', 'error', 'not_sent'):
                    text += '\n题图发送失败 [%s]：%s' % (image_state, str(image_status.get('error') or '请重新读题或检查模型图片能力。'))
                elif image_state == 'partial' or sent < count:
                    text += '\n仅随模型请求发送 %d / %d 图 [%s]；已阻止提交。' % (sent, count, image_state)
                elif sent:
                    text += '\n已随模型请求发送 %d 图 [%s]；不代表图片已被正确识别。' % (sent, image_state)
                else:
                    text += '\n题图发送状态：%s；已发送 %d 图。' % (image_state, sent)
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            title = label(text)
            title.set_max_width_chars(90)
            group.pack_start(title, False, False, 0)
            sources = answer.get('sources') or answer.get('citations') or []
            links = 0
            if isinstance(sources, list):
                for source in sources:
                    if isinstance(source, str):
                        source = {'url': source}
                    if not isinstance(source, dict):
                        continue
                    url = str(source.get('url') or source.get('uri') or '').strip()
                    try:
                        parsed = urlsplit(url)
                        valid = parsed.scheme in ('http', 'https') and bool(parsed.netloc)
                    except ValueError:
                        valid = False
                    if not valid:
                        continue
                    link = Gtk.LinkButton.new_with_label(url, str(source.get('title') or parsed.netloc))
                    link.set_halign(Gtk.Align.START)
                    link.get_style_context().add_class('source-link')
                    link.set_tooltip_text(url)
                    for child in link.get_children():
                        if isinstance(child, Gtk.Label):
                            child.set_ellipsize(Pango.EllipsizeMode.END)
                            child.set_max_width_chars(75)
                    group.pack_start(link, False, False, 0)
                    links += 1
            if not links:
                group.pack_start(label('没有可点击来源；未将模型回答当作已取得的网页证据。', 'subtitle'), False, False, 0)
            self.work_sources.pack_start(group, False, False, 0)
        self.work_sources.show_all()

    def _answer_edited(self, _, path, value):
        self.answer_store[path][2] = value
        index = int(path)
        # Preserve structured answer types (multi-choice arrays / boolean judges).
        parsed = value
        try:
            candidate = json.loads(value)
            if isinstance(candidate, (list, bool)):
                parsed = candidate
        except (ValueError, TypeError):
            pass
        answers = self.work_data.setdefault('answers', [])
        if index < len(answers):
            answers[index].update(answer=parsed, status='ok' if value.strip() else 'unknown')

    def _submit_work(self, mode):
        if self._work_has_image_failures():
            self.work_note.set_text('题图载入或发送不完整，已阻止保存与提交。请重新读题或在官方页面处理。')
            return
        if not self._work_can_write():
            self.work_note.set_text('该作业不可提交，请查看官方作业页。没有保存或提交。')
            return
        if not self.work_data.get('questions'):
            self._message('请先读取作业并生成或填写答案。')
            return
        payload = {key: self.work_data.get(key, [] if key != 'context' else {}) for key in ('questions', 'answers', 'context')}
        payload.update(url=self.work_url.get_text().strip(), mode=mode)
        self._start('work_submit', payload, self._work_result_callback())

    def _edit_work(self, *_):
        dialog = Gtk.Dialog(title='编辑作业数据', transient_for=self, modal=True)
        dialog.set_default_size(800, 620)
        dialog.add_buttons('取消', Gtk.ResponseType.CANCEL, '应用修改', Gtk.ResponseType.OK)
        view, scroll = textbox(480, True)
        put(view, self.work_data)
        dialog.get_content_area().pack_start(scroll, True, True, 0)
        dialog.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            try:
                data = json.loads(contents(view))
                if not isinstance(data, dict) or not isinstance(data.get('questions', []), list) or not isinstance(data.get('answers', []), (dict, list)):
                    raise ValueError('questions 应为数组，answers 应为数组或对象。')
                self.work_data = data
                self._render_answers()
                break
            except (ValueError, TypeError) as exc:
                self._message('作业数据格式有误：' + str(exc))
        dialog.destroy()

    def _sign(self):
        page = self.pages['sign']
        self.saved_locations = []
        self.sign_location_data = {}
        self.sign_local_loaded = False
        self.sign_busy = False
        self.activity_generation = 0
        self.activity_all_items = []
        self.sign_modes = {'normal': '普通签到', 'code': '签到码', 'gesture': '手势签到',
                           'location': '位置签到', 'qr': '二维码签到'}
        page.pack_start(label('选择活动 → 补充所需信息 → 核对并提交。地图选点和常用位置保存只修改表单。', 'subtitle'), False, False, 0)
        card = self._sign_card(page, '1 · 选择课程活动')
        self.sign_course = self._course_selector(card)
        self.sign_course.connect('changed', self._sign_course_changed)
        row = self._row(card)
        self._button(row, '读取活动', self._activities)
        self.activity_filter = Gtk.ComboBoxText()
        for key, text in [('all', '全部'), ('active', '进行中'), ('ended', '已结束')]:
            self.activity_filter.append(key, text)
        self.activity_filter.set_active_id('all')
        self.activity_filter.connect('changed', lambda _: self._render_activity_choices())
        row.pack_start(self.activity_filter, False, False, 0)
        self.activity_select = constrain_combo(Gtk.ComboBoxText())
        self.activity_select.set_hexpand(True)
        self.activity_select.connect('changed', self._activity_changed)
        row.pack_start(self.activity_select, True, True, 0)
        self.sign_activity_note = label('先刷新课程，再读取该课程的签到活动。', 'subtitle')
        card.pack_start(self.sign_activity_note, False, False, 0)
        self.activity_overview = label('活动概览：尚未选择活动。')
        self.activity_overview.set_max_width_chars(90)
        self.activity_overview.set_lines(3)
        self.activity_overview.set_ellipsize(Pango.EllipsizeMode.END)
        card.pack_start(self.activity_overview, False, False, 0)
        more = Gtk.Expander(label='展开更多 · 活动时间与人数')
        self.activity_detail = label('开始：未提供\n截止：未提供\n已签人数：未提供')
        self.activity_detail.set_max_width_chars(85)
        more.add(self.activity_detail)
        card.pack_start(more, False, False, 0)
        advanced = Gtk.Expander(label='高级 · 手动填写活动 ID')
        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.activity_id = self._entry(fields, '活动 ID', '选择活动后自动填写')
        self.activity_id.connect('changed', self._manual_activity_id_changed)
        advanced.add(fields)
        card.pack_start(advanced, False, False, 0)

        card = self._sign_card(page, '2 · 填写签到信息')
        row = self._row(card)
        row.pack_start(label('签到方式'), False, False, 0)
        self.sign_mode = Gtk.ComboBoxText()
        self.sign_mode.append('', '请选择签到方式')
        for key, text in self.sign_modes.items():
            self.sign_mode.append(key, text)
        self.sign_mode.set_active_id('')
        row.pack_start(self.sign_mode, False, False, 0)
        self.sign_blocks = {}
        for kind in self.sign_modes:
            block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            block.set_no_show_all(True)
            card.pack_start(block, False, False, 0)
            self.sign_blocks[kind] = block
        self.sign_blocks['normal'].pack_start(label('普通签到无需额外参数。请先核对上方活动。', 'subtitle'), False, False, 0)
        self.sign_code = self._entry(self.sign_blocks['code'], '现场签到码', '输入老师提供的数字签到码')
        gesture = self.sign_blocks['gesture']
        gesture.pack_start(label('按顺序点击九宫格；数字顺序与手机手势点位一致。', 'subtitle'), False, False, 0)
        gesture_row = self._row(gesture)
        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        gesture_row.pack_start(grid, False, False, 0)
        self.gesture_buttons = []
        for number in range(1, 10):
            button = Gtk.Button(label=str(number))
            button.set_size_request(48, 42)
            button.connect('clicked', lambda _, value=str(number): self._gesture_append(value))
            grid.attach(button, (number-1) % 3, (number-1) // 3, 1, 1)
            self.gesture_buttons.append(button)
        gesture_fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        gesture_row.pack_start(gesture_fields, True, True, 6)
        self.sign_gesture = self._entry(gesture_fields, '手势顺序', '例如 1 → 2 → 3 → 6 → 9')
        self._button(gesture_fields, '清除手势', lambda _: self.sign_gesture.set_text(''))

        location = self.sign_blocks['location']
        row = self._row(location)
        self.location_title = label('尚未选择地点', 'section-title')
        self.location_title.set_hexpand(True)
        self.location_title.set_max_width_chars(55)
        self.location_title.set_lines(1)
        self.location_title.set_ellipsize(Pango.EllipsizeMode.END)
        row.pack_start(self.location_title, True, True, 0)
        self._button(row, '地图选点', self._choose_sign_location, True)
        self.sign_address = self._entry(location, '提交地址', '地图选点后自动填写，也可补充楼宇或教室')
        self.location_note = label('在地图中搜索地点或拖动地图选点，再点击“使用此位置”。', 'subtitle')
        self.location_note.set_max_width_chars(90)
        location.pack_start(self.location_note, False, False, 0)
        row = self._row(location)
        self.location_select = constrain_combo(Gtk.ComboBoxText())
        self.location_select.connect('changed', self._saved_location_changed)
        self.location_select.set_hexpand(True)
        row.pack_start(self.location_select, True, True, 0)
        self.location_use_button = self._button(row, '使用常用位置', self._use_saved_location)
        self.location_delete_button = self._button(row, '删除', self._delete_saved_location)
        self.location_use_button.set_sensitive(False)
        self.location_delete_button.set_sensitive(False)
        row = self._row(location)
        self.location_name = Gtk.Entry(placeholder_text='常用位置标签，例如：图书馆（仅本机可见）')
        self.location_name.set_hexpand(True)
        row.pack_start(self.location_name, True, True, 0)
        self._button(row, '保存为常用位置', self._save_sign_location)
        self.location_saved_note = label('常用位置保存在本机，标签不会作为签到姓名提交。', 'subtitle')
        location.pack_start(self.location_saved_note, False, False, 0)
        advanced = Gtk.Expander(label='手动编辑 / 详细坐标')
        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = self._row(fields)
        self.latitude = Gtk.Entry(placeholder_text='纬度 latitude')
        self.longitude = Gtk.Entry(placeholder_text='经度 longitude')
        for entry in (self.latitude, self.longitude):
            entry.set_width_chars(12)
            entry.set_hexpand(True)
            row.pack_start(entry, True, True, 0)
        self.coordinate_system = Gtk.ComboBoxText()
        self.coordinate_system.append('gcj02', 'GCJ-02 · 国内地图坐标')
        self.coordinate_system.append('wgs84', 'WGS84 · GPS / OSM 坐标')
        self.coordinate_system.set_active_id('gcj02')
        fields.pack_start(label('选择当前输入坐标所属坐标系', 'subtitle'), False, False, 0)
        fields.pack_start(self.coordinate_system, False, False, 0)
        fields.pack_start(label('此选择只标明输入坐标的来源，不改变数字。提交时由服务统一转换；不要混用不同地图的坐标。', 'subtitle'), False, False, 0)
        advanced.add(fields)
        location.pack_start(advanced, False, False, 0)
        self.sign_qr = self._entry(self.sign_blocks['qr'], '二维码内容', '粘贴完整二维码链接，或单独的 enc 字符串')
        self.sign_blocks['qr'].pack_start(label('二维码可能有时效，请使用当前活动的有效内容。完整链接会按二维码内容解析。', 'subtitle'), False, False, 0)
        for entry in (self.sign_code, self.sign_gesture, self.latitude, self.longitude, self.sign_address, self.sign_qr):
            entry.connect('changed', lambda _: self._sign_validate())
        self.coordinate_system.connect('changed', lambda _: self._sign_validate())
        self.location_name.connect('changed', lambda _: self._sign_validate())
        self.sign_mode.connect('changed', self._sign_mode_changed)

        card = self._sign_card(page, '3 · 核对并提交')
        self.sign_summary = label('请选择课程与活动。')
        self.sign_summary.set_max_width_chars(90)
        self.sign_summary.set_lines(4)
        self.sign_summary.set_ellipsize(Pango.EllipsizeMode.END)
        card.pack_start(self.sign_summary, False, False, 0)
        self.sign_validation = label('', 'subtitle')
        card.pack_start(self.sign_validation, False, False, 0)
        row = self._row(card)
        self.sign_submit_button = self._button(row, '提交当前签到', self._sign_start, True)
        self.sign_notice = label('尚未执行签到。', 'subtitle')
        self.sign_notice.set_max_width_chars(70)
        row.pack_start(self.sign_notice, True, True, 0)
        history = self._sign_card(page, '最近签到结果')
        self._button(self._row(history), '刷新本地记录', self._refresh_sign_history)
        self.sign_history_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        history.pack_start(self.sign_history_box, False, False, 0)
        self.sign_history_box.pack_start(label('打开签到页后显示本机最近记录。', 'subtitle'), False, False, 0)
        details = Gtk.Expander(label='详情 · 活动信息与原始返回数据')
        self.sign_output, scroll = textbox(200)
        details.add(scroll)
        page.pack_start(details, False, False, 0)
        self._sign_validate()

    def _sign_card(self, page, title):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.get_style_context().add_class('card')
        card.pack_start(label(title, 'section-title'), False, False, 0)
        page.pack_start(card, False, False, 0)
        return card

    def _sign_page_changed(self, *_):
        if self.stack.get_visible_child_name() == 'sign' and not self.sign_local_loaded:
            self.sign_local_loaded = True
            self._load_saved_locations()
            self._refresh_sign_history()

    def _sign_mode_changed(self, *_):
        mode = self.sign_mode.get_active_id()
        for kind, block in self.sign_blocks.items():
            if mode == kind:
                block.set_no_show_all(False)
                block.show_all()
                block.set_no_show_all(True)
            else:
                block.hide()
        index = self.activity_select.get_active()
        if 0 <= index < len(self.activity_items):
            inferred = self._activity_sign_mode(self.activity_items[index])
            if inferred and mode and inferred != mode:
                self.sign_activity_note.set_text('活动识别为%s，当前选择%s，请核对活动要求。' % (self.sign_modes[inferred], self.sign_modes[mode]))
            elif inferred:
                self.sign_activity_note.set_text('已识别：' + self.sign_modes[inferred])
            elif mode:
                self.sign_activity_note.set_text('活动未提供可识别类型，当前选择%s，请核对活动要求。' % self.sign_modes[mode])
        self._sign_validate()

    def _gesture_append(self, value):
        current = self.sign_gesture.get_text().strip()
        if value not in current:
            self.sign_gesture.set_text(current + value)

    def _sign_course_changed(self, *_):
        self.activity_generation += 1
        self.activity_all_items = []
        self.activity_items = []
        self.activity_select.remove_all()
        self.activity_id.set_text('')
        self.sign_mode.set_active_id('')
        self.sign_activity_note.set_text('已切换课程，请重新读取活动。')
        self._sign_validate()

    def _activities(self, *_):
        course = self._selected_course(self.sign_course)
        if course:
            self.sign_activity_note.set_text('正在读取所选课程活动…')
            self.activity_generation += 1
            generation = self.activity_generation
            def loaded(data):
                if generation == self.activity_generation:
                    self._activities_loaded(data)
            self._request('activities', {'course': course}, loaded)

    def _activities_loaded(self, data):
        self.activity_all_items = data.get('activities', []) if isinstance(data, dict) else data
        self._render_activity_choices()
        put(self.sign_output, data)

    def _render_activity_choices(self):
        selection = self.activity_filter.get_active_id() or 'all'
        candidates = [item for item in self.activity_all_items if not self._activity_non_sign(item)]
        def include(item):
            state, ended = self._activity_state(item)
            return selection == 'all' or (selection == 'ended' and ended) or (selection == 'active' and state == '进行中')
        self.activity_items = [item for item in candidates if include(item)]
        self.activity_select.remove_all()
        for item in self.activity_items:
            name = self._activity_name(item) or str(item.get('id', item.get('activeId', '活动')))
            state, _ = self._activity_state(item)
            self.activity_select.append_text(name + (' · ' + state if state != '未提供' else ''))
        self.activity_id.set_text('')
        self.sign_mode.set_active_id('')
        if self.activity_items:
            self.activity_select.set_active(0)
        elif self.activity_all_items:
            excluded = len(self.activity_all_items) - len(candidates)
            text = '当前分类没有签到活动。共读取 %d 项活动' % len(self.activity_all_items)
            if excluded:
                text += '，已排除 %d 项非签到活动' % excluded
            self.sign_activity_note.set_text(text + '。')
        else:
            self.sign_activity_note.set_text('该课程暂未读取到活动。可稍后刷新或在高级区填写活动 ID。')
        self._sign_validate()

    @classmethod
    def _activity_non_sign(cls, item):
        kind = cls._activity_field(item, 'type')
        # The upstream ActivityType enum defines SIGNIN=2. Missing type fields
        # stay visible for manual choice instead of being guessed.
        return kind is not None and str(kind).isdigit() and str(kind) != '2'

    @staticmethod
    def _activity_field(item, *keys):
        for source in (item, item.get('data', {})):
            if isinstance(source, dict):
                for key in keys:
                    value = source.get(key)
                    if value is not None and str(value).strip():
                        return value
        return None

    @staticmethod
    def _activity_name(item):
        return str(Window._activity_field(item, 'nameOne', 'name', 'title', 'nameTwo') or '')

    @staticmethod
    def _activity_time(value):
        if value is None or str(value).strip() in ('', '0', '-1'):
            return '未提供', None
        raw = str(value).strip()
        try:
            # Epoch fields are seconds or milliseconds; formatted date strings
            # remain readable even when they do not match an ISO date.
            if re.fullmatch(r'\d{9,13}(?:\.\d+)?', raw):
                stamp = float(raw)
                if stamp > 100_000_000_000:
                    stamp /= 1000
                date = datetime.fromtimestamp(stamp, ZoneInfo('Asia/Shanghai'))
                return date.strftime('%Y-%m-%d %H:%M:%S'), stamp
            date = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if date.tzinfo is None:
                date = date.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
            else:
                date = date.astimezone(ZoneInfo('Asia/Shanghai'))
            return date.strftime('%Y-%m-%d %H:%M:%S'), date.timestamp()
        except (ValueError, TypeError, OverflowError, OSError):
            return raw, None

    @classmethod
    def _activity_state(cls, item, now=None):
        if not item:
            return '未提供', False
        now = time.time() if now is None else now
        _, end = cls._activity_time(cls._activity_field(item, 'endTime', 'endtime', 'end_time', 'endDate', 'enddate'))
        _, start = cls._activity_time(cls._activity_field(item, 'startTime', 'starttime', 'start_time', 'startDate', 'startdate', 'beginTime'))
        raw = cls._activity_field(item, 'statusText', 'statusName', 'status', 'activeStatus', 'state')
        state = str(raw).strip() if raw is not None else ''
        if cls._activity_field(item, 'isEnded', 'isEnd', 'ended') in (True, 'true', 'True'):
            return '已结束', True
        if state.lower() in ('ended', 'end', 'expired', 'closed', 'cancelled', 'canceled', '已结束', '已过期', '已取消', '结束'):
            return ('已取消' if 'cancel' in state.lower() or '取消' in state else '已结束'), True
        if end is not None and end <= now:
            return '已结束 · 截止时间已过', True
        if state == '2':
            return '已结束', True
        if state == '1':
            return '进行中', False
        if state:
            names = {'active': '进行中', 'running': '进行中', 'ongoing': '进行中',
                     'not_started': '未开始', 'pending': '未开始', '进行中': '进行中', '未开始': '未开始'}
            fallback = ('未提供（状态编号 ' + state + '）') if state.isdigit() else state
            return names.get(state.lower(), fallback), False
        if start is not None and start > now:
            return '未开始', False
        if start is not None and start <= now and end is not None and end > now:
            return '进行中', False
        return '未提供', False

    def _update_activity_overview(self, item):
        if not item:
            ident = self.activity_id.get_text().strip()
            self.activity_overview.set_text(('名称：手动活动 ID %s\n类型：未提供  ·  状态：未提供（尚未核对）' % ident)
                                            if ident else '活动概览：尚未选择活动。')
            self.activity_detail.set_text('开始：未提供\n截止：未提供\n已签人数：未提供')
            return
        mode = self._activity_sign_mode(item)
        kind = self.sign_modes.get(mode)
        if not kind:
            type_name = self._activity_field(item, 'typeName')
            raw_type = self._activity_field(item, 'type')
            kind = str(type_name) if type_name else ('签到（方式未提供）' if str(raw_type) == '2' else '未提供')
        state, _ = self._activity_state(item)
        name = self._activity_name(item) or '未提供'
        self.activity_overview.set_text('名称：%s\n类型：%s  ·  状态：%s' % (name, kind, state))
        self.activity_overview.set_tooltip_text(self.activity_overview.get_text())
        start, _ = self._activity_time(self._activity_field(item, 'startTime', 'starttime', 'start_time', 'startDate', 'startdate', 'beginTime'))
        end, _ = self._activity_time(self._activity_field(item, 'endTime', 'endtime', 'end_time', 'endDate', 'enddate'))
        count = self._activity_field(item, 'signedCount', 'signCount', 'signedNum', 'signNum', 'alreadySignCount', 'signInCount')
        total = self._activity_field(item, 'totalCount', 'totalNum', 'studentCount')
        count = '未提供' if count is None else str(count)
        if total is not None:
            count += ' / ' + str(total)
        extra = self._activity_field(item, 'nameTwo')
        people = '已签人数：' + count
        attended = self._activity_field(item, 'attendNum')
        released = self._activity_field(item, 'releaseNum')
        if attended is not None or released is not None:
            people = '已到 / 发布人数：%s / %s' % ('未提供' if attended is None else attended, '未提供' if released is None else released)
        self.activity_detail.set_text('开始：%s\n截止：%s\n%s%s' % (start, end, people, '\n补充：' + str(extra) if extra and str(extra) != name else ''))

    @staticmethod
    def _activity_sign_mode(item):
        if Window._activity_non_sign(item):
            return None
        for key in ('sign_type', 'signType', 'sign_mode'):
            value = str(item.get(key, '')).lower()
            if value in ('normal', 'code', 'gesture', 'location', 'qr'):
                return value
        detail = item.get('data', {})
        if not isinstance(detail, dict):
            detail = {}
        other = item.get('otherId', item.get('otherid', detail.get('otherId')))
        if other is not None:
            return {'0': 'normal', '2': 'qr', '3': 'gesture', '4': 'location', '5': 'code'}.get(str(other))
        return None

    @classmethod
    def _activity_ident(cls, item):
        return str(cls._activity_field(item, 'id', 'activeId', 'active_id') or '').strip()

    def _selected_sign_activity(self):
        index = self.activity_select.get_active()
        if 0 <= index < len(self.activity_items):
            item = self.activity_items[index]
            if self._activity_ident(item) == self.activity_id.get_text().strip():
                return item
        return {}

    def _manual_activity_id_changed(self, *_):
        # An ID edit changes the remote target. Never retain a different
        # activity's name, inferred mode, deadline or submission metadata.
        if self._selected_sign_activity():
            self._sign_validate()
            return
        self.activity_select.set_active(-1)
        if hasattr(self, 'sign_mode'):
            self.sign_mode.set_active_id('')
        ident = self.activity_id.get_text().strip()
        self.sign_activity_note.set_text(('手动活动 ID %s：活动详情尚未读取，请核对课程并选择签到方式。' % ident)
                                         if ident else '活动 ID 已清空，请选择活动或输入有效 ID。')
        if hasattr(self, 'sign_output'):
            put(self.sign_output, {'activity_id': ident, 'metadata_status': 'unverified'})
        self._sign_validate()

    def _activity_changed(self, selector):
        index = selector.get_active()
        if 0 <= index < len(self.activity_items):
            item = self.activity_items[index]
            self.activity_id.set_text(self._activity_ident(item))
            mode = self._activity_sign_mode(item)
            self.sign_mode.set_active_id(mode or '')
            self.sign_activity_note.set_text('已识别：' + self.sign_modes[mode] if mode else '活动未提供可识别的签到类型，请手动选择签到方式。')
            put(self.sign_output, item)
        self._sign_validate()

    def _location_payload(self):
        from .location import location_record
        return location_record(self.latitude.get_text().strip(), self.longitude.get_text().strip(),
                               coordinate_system=self.coordinate_system.get_active_id() or 'gcj02',
                               name=self.location_name.get_text().strip(), address=self.sign_address.get_text().strip(),
                               source=self.sign_location_data.get('source', 'manual'))

    def _set_sign_location(self, value):
        self.sign_location_data = dict(value)
        self.latitude.set_text(str(value['latitude']))
        self.longitude.set_text(str(value['longitude']))
        self.coordinate_system.set_active_id(value.get('coordinate_system', 'gcj02'))
        self.sign_address.set_text(str(value.get('address', '')))
        self.location_name.set_text(str(value.get('name', '')))
        self.location_title.set_text(str(value.get('name') or '已选择地图位置'))
        self.location_note.set_text('已填入地图位置。可补充地址并保存为常用位置；尚未提交签到。')
        self._sign_validate()

    def _choose_sign_location(self, *_):
        try:
            from .map_picker import choose_location
            try:
                initial = self._location_payload()
            except ValueError:
                initial = None
            value = choose_location(self, initial=initial)
            if value:
                self._set_sign_location(value)
        except Exception as exc:
            self.location_note.set_text('地图暂时无法打开：' + str(exc) + '。可展开详细坐标手动填写。')

    def _local_sign_request(self, action, payload, done, notice):
        def loaded(value):
            if not isinstance(value, dict) or value.get('ok') is False:
                notice.set_text(str(value.get('message', '本地数据操作未完成。')) if isinstance(value, dict) else '本地数据格式无法识别。')
                return
            done(value)
        self._async(lambda: self.service.request(action, payload), loaded, lambda text: notice.set_text('本地数据操作失败：' + str(text)))

    def _load_saved_locations(self):
        self._local_sign_request('locations_list', {}, self._locations_loaded, self.location_saved_note)

    def _locations_loaded(self, data):
        self.saved_locations = data.get('locations', [])
        self.location_select.remove_all()
        for item in self.saved_locations:
            text = str(item.get('name') or '未命名位置')
            if item.get('address'):
                text += ' · ' + str(item['address'])
            self.location_select.append(str(item['id']), text)
        if self.saved_locations:
            self.location_select.set_active(0)
        else:
            self.location_select.append('', '暂无常用位置，地图选点后可保存')
            self.location_select.set_active(0)
        self.location_use_button.set_sensitive(bool(self.saved_locations))
        self.location_delete_button.set_sensitive(bool(self.saved_locations))
        self.location_saved_note.set_text('已保存 %d 个常用位置。标签仅本机可见，地址用于提交。' % len(self.saved_locations))

    def _saved_location_changed(self, *_):
        ident = self.location_select.get_active_id()
        item = next((item for item in self.saved_locations if str(item.get('id')) == ident), None)
        if item:
            self.location_select.set_tooltip_text('%s\n%s\n纬度 %s · 经度 %s · %s' % (
                item.get('name', ''), item.get('address', ''), item.get('latitude', ''),
                item.get('longitude', ''), str(item.get('coordinate_system', 'gcj02')).upper()))
        else:
            self.location_select.set_tooltip_text('地图选点后可保存常用位置。')

    def _use_saved_location(self, *_):
        ident = self.location_select.get_active_id()
        item = next((item for item in self.saved_locations if str(item.get('id')) == ident), None)
        if item:
            self._set_sign_location(item)
        else:
            self.location_saved_note.set_text('请先选择一个已保存的常用位置。')

    def _save_sign_location(self, *_):
        try:
            value = self._location_payload()
            if not value['name']:
                raise ValueError('请先填写常用位置标签。')
            if not value['address']:
                raise ValueError('请先填写提交地址。')
        except ValueError as exc:
            self.location_saved_note.set_text(str(exc))
            return
        def saved(data):
            self._load_saved_locations()
            self.location_saved_note.set_text('常用位置已保存；没有提交签到。')
        self._local_sign_request('locations_save', value, saved, self.location_saved_note)

    def _delete_saved_location(self, *_):
        ident = self.location_select.get_active_id()
        if not ident:
            self.location_saved_note.set_text('请先选择要删除的常用位置。')
            return
        self._local_sign_request('locations_delete', {'id': ident}, lambda _: self._load_saved_locations(), self.location_saved_note)

    def _sign_validate(self):
        if not hasattr(self, 'sign_summary'):
            return ['签到表单尚未就绪']
        course_index = self.sign_course.get_active()
        course = self.courses[course_index] if 0 <= course_index < len(self.courses) else {}
        activity = self._selected_sign_activity()
        ident = self.activity_id.get_text().strip()
        mode = self.sign_mode.get_active_id()
        self._update_activity_overview(activity)
        errors = []
        _, ended = self._activity_state(activity)
        if ended:
            errors.append('该活动已结束，请选择当前有效活动')
        if not course:
            errors.append('选择课程')
        if not ident.isdigit():
            errors.append('选择有效活动或填写数字活动 ID')
        if not mode:
            errors.append('选择签到方式')
        elif mode in ('code', 'gesture'):
            text = (self.sign_code if mode == 'code' else self.sign_gesture).get_text().strip()
            if not text.isdigit() or (mode == 'gesture' and (set(text) - set('123456789') or len(set(text)) != len(text))):
                errors.append('填写数字签到码' if mode == 'code' else '填写不重复的九宫格手势序列')
        elif mode == 'location':
            try:
                value = self._location_payload()
                self.location_title.set_text(value['name'] or '已填写坐标')
            except ValueError:
                errors.append('在地图选择地点或填写有效坐标')
            if not self.sign_address.get_text().strip():
                errors.append('补充提交地址')
        elif mode == 'qr':
            qr = self.sign_qr.get_text().strip()
            if not qr:
                errors.append('粘贴当前二维码内容')
            elif '://' in qr or '?' in qr:
                try:
                    if not parse_qs(urlsplit(qr).query).get('enc', [''])[0]:
                        errors.append('使用包含 enc 参数的完整二维码链接')
                except ValueError:
                    errors.append('检查二维码链接格式')
        if mode == 'gesture':
            selected = self.sign_gesture.get_text().strip()
            for index, button in enumerate(self.gesture_buttons, 1):
                style = button.get_style_context()
                (style.add_class if str(index) in selected else style.remove_class)('primary')
        course_name = str(course.get('name', course.get('id', '未选择课程')))
        activity_name = self._activity_name(activity) or ('手动活动 ' + ident if ident else '未选择活动')
        summary = '课程：%s  /  活动：%s\n方式：%s' % (course_name, activity_name, self.sign_modes.get(mode, '尚未选择'))
        if mode == 'location':
            summary += '\n地址：' + (self.sign_address.get_text().strip() or '尚未填写')
        self.sign_summary.set_text(summary)
        self.sign_summary.set_tooltip_text(summary)
        self.sign_validation.set_text('还需：' + '；'.join(errors) if errors else '信息已齐全。核对后点击“提交当前签到”。')
        self.sign_submit_button.set_sensitive(not errors and not self.sign_busy)
        return errors

    def _sign_start(self, *_):
        if self.sign_busy:
            return
        if self._sign_validate():
            self.sign_notice.set_text('请补充上方缺少的信息，尚未提交。')
            return
        mode = self.sign_mode.get_active_id()
        payload = {'activity_id': self.activity_id.get_text().strip(), 'sign_type': mode,
                   'course': self.courses[self.sign_course.get_active()]}
        if mode in ('code', 'gesture'):
            payload['sign_code'] = (self.sign_code if mode == 'code' else self.sign_gesture).get_text().strip()
        elif mode == 'location':
            value = self._location_payload()
            # Location labels belong to local favourites, not the signed-in person's name.
            payload.update({key: val for key, val in value.items() if key != 'name'})
        elif mode == 'qr':
            qr = self.sign_qr.get_text().strip()
            payload['qr_content' if '://' in qr or '?' in qr else 'enc'] = qr
        activity = self._selected_sign_activity()
        if activity:
            payload['activity'] = activity
        self.sign_busy = True
        self._sign_validate()
        self.sign_notice.set_text('正在提交签到信息，等待服务器结果…')
        self._start('sign', payload, done=self._sign_result, output=self.sign_output)

    def _sign_result(self, data):
        self.sign_busy = False
        self._sign_validate()
        if not isinstance(data, dict):
            self.sign_notice.set_text('签到结果无法识别，请查看详情。')
            return
        state = str(data.get('status') or ('success' if data.get('ok') else 'needs_input'))
        title = '签到成功' if data.get('ok') and state in ('success', 'completed') else ('需补充信息' if state == 'needs_input' else STATUS_NAMES.get(state, '未完成'))
        self.sign_notice.set_text(title + '：' + human_message(data.get('message') or '服务器未返回说明，请在手机中核对。'))
        put(self.sign_output, data)
        if self.sign_local_loaded:
            self._refresh_sign_history()

    def _refresh_sign_history(self, *_):
        self._local_sign_request('sign_history', {}, self._sign_history_loaded, self.sign_notice)

    def _sign_history_loaded(self, data):
        for child in self.sign_history_box.get_children():
            self.sign_history_box.remove(child)
        records = data.get('history', [])
        if not records:
            self.sign_history_box.pack_start(label('暂无本机签到记录。提交后的结果将显示在这里。', 'subtitle'), False, False, 0)
        for record in records[:8]:
            result = record.get('result') or {}
            payload = record.get('payload') or {}
            course = payload.get('course') or {}
            summary = record.get('summary') or {}
            title = str(summary.get('course') or course.get('name') or record.get('course_name') or '签到活动')
            if summary.get('activity_id'):
                title += ' · 活动 ' + str(summary['activity_id'])
            if summary.get('sign_type'):
                title += ' · ' + self.sign_modes.get(str(summary['sign_type']), str(summary['sign_type']))
            state = str(result.get('status') or record.get('status') or '')
            message = human_message(result.get('message') or record.get('message') or '')
            stamp = str(record.get('finished_at') or record.get('updated_at') or record.get('created_at') or '')
            text = '%s · %s%s\n%s' % (title, STATUS_NAMES.get(state, state or '结果待核对'), (' · ' + stamp) if stamp else '', message)
            if summary.get('address'):
                text += '\n地址：' + str(summary['address'])
            item = label(text)
            item.set_max_width_chars(85)
            self.sign_history_box.pack_start(item, False, False, 0)
        self.sign_history_box.show_all()

    def _phone(self):
        page = self.pages['phone']
        page.pack_start(label('连接 USB 手机并允许调试。画面只在此页手动刷新或开启自动刷新后读取；请先在手机打开学习通。点击画面可操作手机。', 'subtitle'), False, False, 0)
        row = self._row(page)
        self.phone_select = constrain_combo(Gtk.ComboBoxText())
        self.phone_select.set_hexpand(True)
        self.phone_select.connect('changed', self._device_changed)
        row.pack_start(self.phone_select, True, True, 0)
        self._button(row, '刷新设备', lambda b: self._request('phone_devices', done=self._devices_loaded, button=b))
        self._button(row, '打开学习通', lambda _: self._phone_call('phone_focus', {}, refresh=True), True)
        self._button(row, '打开手机窗口', lambda _: self._phone_open())
        row = self._row(page)
        self._button(row, '刷新画面', lambda _: self._phone_screen())
        self.phone_auto = Gtk.CheckButton(label='自动刷新（仅此页显示时）')
        row.pack_start(self.phone_auto, False, False, 0)
        for key, title in [('BACK', '返回'), ('HOME', '主屏幕'), ('APP_SWITCH', '最近应用')]:
            self._button(row, title, lambda _, k=key: self._phone_call('phone_key', {'key': k}, refresh=True))
        row = self._row(page)
        self.phone_text = Gtk.Entry(placeholder_text='输入英文或数字；中文请用手机键盘或独立手机窗口')
        self.phone_text.set_hexpand(True)
        row.pack_start(self.phone_text, True, True, 0)
        self._button(row, '发送文字', lambda _: self._phone_call('phone_text', {'text': self.phone_text.get_text()}, refresh=True))
        row = self._row(page)
        self._button(row, '向上翻页', lambda _: self._phone_swipe(False))
        self._button(row, '向下翻页', lambda _: self._phone_swipe(True))
        self.phone_note = label('尚未获取手机画面。', 'subtitle')
        page.pack_start(self.phone_note, False, False, 0)
        self.phone_image = Gtk.Image()
        self.phone_event = Gtk.EventBox()
        self.phone_event.add(self.phone_image)
        self.phone_event.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.phone_event.connect('button-press-event', self._phone_tap)
        self.phone_event.set_halign(Gtk.Align.CENTER)
        page.pack_start(self.phone_event, True, True, 0)

    def _devices_loaded(self, data):
        self.devices = data.get('devices', [])
        self.phone_select.remove_all()
        for item in self.devices:
            if isinstance(item, str):
                self.phone_select.append(item, item)
            else:
                serial = str(item.get('serial', item.get('id', '')))
                self.phone_select.append(serial, str(item.get('model', serial)) + ' · ' + str(item.get('state', '')))
        if self.devices:
            self.phone_select.set_active(0)
        self._message('已读取 %d 台设备。' % len(self.devices))

    def _device_changed(self, *_):
        self.phone_size = None
        self.phone_frame_serial = None
        self.phone_image.clear()
        self.phone_note.set_text('设备已选择，请刷新画面。')

    def _phone_call(self, action, payload=None, refresh=False):
        serial = self.phone_select.get_active_id()
        if not serial:
            self._message('请先刷新设备并选择已授权的手机。')
            return
        data = dict(payload or {}, serial=serial)
        self._request(action, data, lambda _: self._phone_screen() if refresh else None)

    def _phone_open(self):
        serial = self.phone_select.get_active_id()
        if not serial:
            self._message('请先选择手机。')
            return
        self._start('phone_open', {'serial': serial})

    def _phone_tick(self):
        if self.closed or self.closing:
            return False
        if self.stack.get_visible_child_name() == 'phone' and self.phone_auto.get_active():
            self._phone_screen()
        return True

    def _phone_screen(self):
        serial = self.phone_select.get_active_id()
        if self.phone_loading or not serial:
            if not serial:
                self._message('请先刷新并选择手机。')
            return
        self.phone_loading = True
        def loaded(data):
            self.phone_loading = False
            if serial != self.phone_select.get_active_id():
                return
            try:
                if data.get('ok') is False:
                    raise ValueError(data.get('message') or '手机画面暂不可用。')
                raw = GdkPixbuf.Pixbuf.new_from_file(str(data['path']))
                self.phone_size = (int(data.get('width') or raw.get_width()), int(data.get('height') or raw.get_height()))
                scale = min(640 / raw.get_width(), 540 / raw.get_height(), 1)
                picture = raw.scale_simple(max(1, int(raw.get_width() * scale)), max(1, int(raw.get_height() * scale)), GdkPixbuf.InterpType.BILINEAR)
                self.phone_image.set_from_pixbuf(picture)
                self.phone_frame_serial = serial
                self.phone_note.set_text('已连接 · 点击画面操作手机 · %s × %s' % self.phone_size)
            except Exception as exc:
                self._message('画面无法显示：' + str(exc))
        def failed(message):
            self.phone_loading = False
            self.phone_auto.set_active(False)
            self.phone_size = None
            self.phone_frame_serial = None
            self.phone_image.clear()
            self.phone_note.set_text('画面读取已暂停：' + message)
        self._async(lambda: self.service.request('phone_screen', {'serial': serial}), loaded, failed)

    def _phone_swipe(self, down):
        if not self.phone_size:
            self._message('请先刷新手机画面。')
            return
        width, height = self.phone_size
        self._phone_call('phone_swipe', {'x1': width // 2, 'x2': width // 2,
            'y1': int(height * (0.75 if down else 0.25)),
            'y2': int(height * (0.25 if down else 0.75))}, refresh=True)

    def _phone_tap(self, _, event):
        picture = self.phone_image.get_pixbuf()
        if not picture or not self.phone_size or self.phone_frame_serial != self.phone_select.get_active_id():
            return False
        allocation = self.phone_image.get_allocation()
        x = event.x - (allocation.width - picture.get_width()) / 2
        y = event.y - (allocation.height - picture.get_height()) / 2
        if 0 <= x < picture.get_width() and 0 <= y < picture.get_height():
            self._phone_call('phone_tap', {'x': int(x * self.phone_size[0] / picture.get_width()), 'y': int(y * self.phone_size[1] / picture.get_height())}, True)
        return True

    def _settings(self):
        page = self.pages['settings']
        page.pack_start(label('优先复用本机可读取的学习通已登录网页会话，无需再次填写账号密码。手机应用的私有登录状态不会因 USB 连接而自动开放。', 'subtitle'), False, False, 0)
        row = self._row(page)
        self.session_sync_button = self._button(row, '同步学习通登录状态', self._sync_session, True)
        self._button(row, '打开官方网页登录', self._open_official_login)
        self.session_notice = label('先在系统浏览器登录学习通，再点击同步。仅在你点击时读取本机会话。', 'subtitle')
        self.session_notice.set_max_width_chars(90)
        page.pack_start(self.session_notice, False, False, 0)
        self.setting_entries = {}
        self.account_expander = Gtk.Expander(label='备用登录 · 账号密码 / Cookie 导入')
        backup = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for key, title, placeholder, secret in [('username', '学习通账号', '手机号 / 账号', False), ('password', '学习通密码', '', True)]:
            self.setting_entries[key] = self._entry(backup, title, placeholder, secret)
        self._button(self._row(backup), '使用账号密码登录', lambda _: self._save_then_start('login'))
        self.cookie_path = self._entry(backup, '导入已有会话', '本地 Cookie 文本文件的绝对路径')
        row = self._row(backup)
        self._button(row, '选择文件', self._choose_cookie)
        self._button(row, '导入会话', self._import_cookie)
        self.account_expander.add(backup)
        page.pack_start(self.account_expander, False, False, 0)
        page.pack_start(label('AI 与联网', 'section-title'), False, False, 0)
        self.ai_provider = constrain_combo(Gtk.ComboBoxText())
        self.ai_provider.append('chatgpt', 'ChatGPT 官方账号授权')
        self.ai_provider.append('api_key', 'API Key（DeepSeek、OpenAI 等）')
        self.ai_provider.set_active_id('api_key')
        page.pack_start(self.ai_provider, False, False, 0)
        self._chatgpt_settings(page)
        self.ai_manual_expander = Gtk.Expander(label='API Key 配置 · DeepSeek、OpenAI 与其他服务')
        manual = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.ai_manual_expander.add(manual)
        page.pack_start(self.ai_manual_expander, False, False, 0)
        for key, title, placeholder, secret in [('api_base', 'AI API 地址', 'https://…/v1', False),
                                               ('api_key', 'AI API Key', '', True),
                                               ('model', '模型名称', '填写服务支持的模型名称', False)]:
            self.setting_entries[key] = self._entry(manual, title, placeholder, secret)
        manual_actions = self._row(manual)
        flash_preset = self._button(manual_actions, '填入 DeepSeek Flash 与官方搜索配置', self._use_deepseek_flash)
        flash_preset.set_tooltip_text('会将搜索服务切为 DeepSeek 官方搜索，与模型共用官方 Key，按服务计费；仍可在搜索服务中选择免费的 bing_rss。')
        self._button(manual_actions, 'OpenAI API Key 配置', self._use_openai_gpt)
        manual.pack_start(label('API Key 模式的联网方式', 'subtitle'), False, False, 0)
        self.search_mode = constrain_combo(Gtk.ComboBoxText())
        for key, text in [('tools', '模型调用搜索工具'), ('native', '模型原生联网（Responses web_search）'), ('prefetch', '预搜索（兼容）')]:
            self.search_mode.append(key, text)
        self.search_mode.set_active_id('tools')
        self.search_mode.connect('changed', self._search_mode_changed)
        manual.pack_start(self.search_mode, False, False, 0)
        self.search_mode_notice = label('', 'subtitle')
        self.search_mode_notice.set_max_width_chars(90)
        manual.pack_start(self.search_mode_notice, False, False, 0)
        provider_expander = Gtk.Expander(label='搜索服务 / Key · 适用于模型搜索工具与兼容预搜索')
        provider = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for key, title, placeholder, secret in [('search_provider', '搜索服务', 'bing_rss / deepseek / duckduckgo / tavily', False), ('search_key', '搜索服务 Key', 'DeepSeek 官方 API 可留空复用 AI Key；网关需单独搜索 Key', True)]:
            self.setting_entries[key] = self._entry(provider, title, placeholder, secret)
        provider.pack_start(label('deepseek 搜索使用官方 API，按供应商规则计费，可复用上方官方 AI API Key；若模型地址是第三方网关，请填写单独的 DeepSeek 官方搜索 Key。', 'subtitle'), False, False, 0)
        provider_expander.add(provider)
        manual.pack_start(provider_expander, False, False, 0)
        self.search_enabled = Gtk.CheckButton(label='允许模型联网')
        page.pack_start(self.search_enabled, False, False, 0)
        page.pack_start(label('模型可按需搜索，每题会显示实际使用情况和来源。', 'subtitle'), False, False, 0)
        self.ai_test_notice = label('尚未测试。连接测试发送一条短消息。', 'subtitle')
        self.ai_test_notice.set_max_width_chars(90)
        page.pack_start(self.ai_test_notice, False, False, 0)
        row = self._row(page)
        self._button(row, '保存设置', self._save_settings, True)
        self.ai_test_button = self._button(row, '测试 AI 连接', lambda _: self._save_then_start('ai_test'))
        self._button(row, '重新读取', lambda _: self._async(self.service.settings, self._settings_loaded))
        self._search_mode_changed()
        advanced = Gtk.Expander(label='高级 · 课程服务设置与安装')
        backend = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for key, title, placeholder in [('backend_path', '课程服务项目路径', '包含 main.py 的 chaoxing 项目目录'),
                                        ('backend_python', '课程服务 Python', '留空则使用服务默认解释器')]:
            self.setting_entries[key] = self._entry(backend, title, placeholder)
        self._button(self._row(backend), '安装课程服务组件', lambda _: self._start('install_backend', {}))
        self.settings_paths = label('', 'subtitle')
        backend.pack_start(self.settings_paths, False, False, 0)
        advanced.add(backend)
        page.pack_start(advanced, False, False, 0)
        for key in ('api_base', 'model'):
            self.setting_entries[key].connect('changed', self._search_mode_changed)
        self.ai_provider.connect('changed', self._ai_provider_changed)
        self.stack.connect('notify::visible-child-name', self._chatgpt_settings_visible)
        self._async(self.service.settings, self._settings_loaded)

    def _chatgpt_settings(self, page):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class('card')
        page.pack_start(card, False, False, 0)
        card.pack_start(label('使用 ChatGPT 账号', 'section-title'), False, False, 0)
        self.chatgpt_notice = label('在浏览器登录 ChatGPT 并授权此应用使用符合条件的套餐。', 'subtitle')
        self.chatgpt_notice.set_max_width_chars(72)
        card.pack_start(self.chatgpt_notice, False, False, 0)

        self.chatgpt_active_id = ''
        self.chatgpt_profiles = {}
        self.chatgpt_connected = False
        self.chatgpt_authenticated = False
        self.chatgpt_last_model = ''
        self.chatgpt_job_id = ''
        self.chatgpt_busy = False
        self.chatgpt_updating = False
        self.chatgpt_status_request = 0
        self.chatgpt_model_request = 0

        choices = Gtk.Grid(column_spacing=12, row_spacing=8)
        card.pack_start(choices, False, False, 0)
        self.chatgpt_accounts = constrain_combo(Gtk.ComboBoxText())
        self.chatgpt_models = constrain_combo(Gtk.ComboBoxText())
        for index, (title, combo) in enumerate((('ChatGPT 账号', self.chatgpt_accounts), ('ChatGPT 模型', self.chatgpt_models))):
            choices.attach(label(title), 0, index, 1, 1)
            combo.set_hexpand(True)
            choices.attach(combo, 1, index, 1, 1)
        self.chatgpt_accounts.append('', '未连接')
        self.chatgpt_accounts.set_active(0)
        self.chatgpt_accounts.connect('changed', self._chatgpt_select)
        self.chatgpt_models.append('', '自动选择')
        self.chatgpt_models.set_active(0)
        self.chatgpt_models.connect('changed', self._chatgpt_model_changed)

        row = self._row(card)
        self.chatgpt_sign_in = self._button(row, '使用 ChatGPT 继续', self._chatgpt_start_sign_in, True)
        self.chatgpt_add = self._button(row, '添加账号', lambda _: self._chatgpt_start_sign_in(new_account=True))
        self.chatgpt_sign_out = self._button(row, '退出授权', self._chatgpt_disconnect)
        self.chatgpt_cancel = self._button(row, '取消登录', self._chatgpt_cancel_sign_in)
        self.chatgpt_refresh = self._button(row, '刷新状态', lambda _: self._chatgpt_load_status())
        usage = Gtk.LinkButton.new_with_label('https://chatgpt.com/settings/usage', '管理 ChatGPT 用量')
        card.pack_start(usage, False, False, 0)
        usage.set_halign(Gtk.Align.START)
        self._chatgpt_update_controls()

    def _ai_provider_changed(self, *_):
        self.ai_manual_expander.set_expanded(self.ai_provider.get_active_id() == 'api_key')

    def _chatgpt_settings_visible(self, *_):
        if self.stack.get_visible_child_name() == 'settings' and not self.chatgpt_busy:
            self._chatgpt_load_status()

    def _chatgpt_update_controls(self):
        ready = not self.chatgpt_busy
        selected = self.chatgpt_accounts.get_active_id()
        can_use_selection = self.chatgpt_connected and selected == self.chatgpt_active_id
        self.chatgpt_sign_in.set_sensitive(ready)
        self.chatgpt_sign_in.set_label('重新授权' if selected else '使用 ChatGPT 继续')
        self.chatgpt_add.set_sensitive(ready and bool(self.chatgpt_profiles))
        self.chatgpt_sign_out.set_sensitive(ready and self.chatgpt_authenticated and selected == self.chatgpt_active_id)
        self.chatgpt_cancel.set_sensitive(bool(self.chatgpt_job_id))
        self.chatgpt_accounts.set_sensitive(ready and bool(self.chatgpt_profiles))
        self.chatgpt_models.set_sensitive(ready and can_use_selection)
        self.chatgpt_refresh.set_sensitive(ready)

    def _chatgpt_error(self, message):
        self.chatgpt_busy = False
        self.chatgpt_job_id = ''
        self.chatgpt_notice.set_text(str(message or 'ChatGPT 账号操作未完成。'))
        self._chatgpt_update_controls()

    def _chatgpt_load_status(self):
        self.chatgpt_status_request += 1
        generation = self.chatgpt_status_request

        def loaded(status):
            if generation == self.chatgpt_status_request and not self.chatgpt_busy:
                self._chatgpt_show_status(status)

        def failed(message):
            if generation == self.chatgpt_status_request and not self.chatgpt_busy:
                self._chatgpt_error(message)

        self._async(lambda: self.service.request('chatgpt_status', {}), loaded, failed)

    def _chatgpt_show_status(self, status):
        if not isinstance(status, dict) or not isinstance(status.get('profiles'), list):
            self._chatgpt_error(status.get('message') if isinstance(status, dict) else None)
            return
        self.chatgpt_updating = True
        try:
            self.chatgpt_accounts.remove_all()
            profiles = [profile for profile in status['profiles'] if isinstance(profile, dict) and profile.get('id')]
            self.chatgpt_profiles = {str(profile['id']): profile for profile in profiles}
            for profile in profiles:
                ident = str(profile['id'])
                title = str(profile.get('label') or 'ChatGPT 账号')
                if not profile.get('connected'):
                    title += '（需授权）'
                self.chatgpt_accounts.append(ident, title)
            if profiles:
                if not self.chatgpt_accounts.set_active_id(str(status.get('active_id') or '')):
                    self.chatgpt_accounts.set_active(0)
            else:
                self.chatgpt_accounts.append('', '未连接')
                self.chatgpt_accounts.set_active(0)
        finally:
            self.chatgpt_updating = False
        self.chatgpt_active_id = str(status.get('active_id') or '')
        self.chatgpt_connected = status.get('connected') is True
        self.chatgpt_authenticated = status.get('authenticated') is True or self.chatgpt_connected
        notice = 'ChatGPT 套餐使用已授权。' if self.chatgpt_connected else (
            '请授权此应用使用 ChatGPT 套餐。' if self.chatgpt_active_id else '在浏览器登录 ChatGPT 并授权此应用使用符合条件的套餐。')
        if status.get('revocation_confirmed') is False:
            notice = '已在本机退出授权。可在 ChatGPT 用量设置中核对连接状态。'
        self.chatgpt_notice.set_text(str(status.get('message') or notice))
        self._chatgpt_update_controls()
        if self.chatgpt_connected:
            self._chatgpt_load_models()
        else:
            self._chatgpt_show_models([])

    def _chatgpt_show_models(self, models):
        self.chatgpt_updating = True
        try:
            self.chatgpt_models.remove_all()
            self.chatgpt_models.append('', '自动选择')
            for model in models:
                if isinstance(model, dict) and model.get('slug'):
                    slug = str(model['slug'])
                    self.chatgpt_models.append(slug, str(model.get('display_name') or slug))
            if not self.chatgpt_models.set_active_id(self.chatgpt_last_model):
                self.chatgpt_models.set_active(0)
        finally:
            self.chatgpt_updating = False

    def _chatgpt_load_models(self):
        self.chatgpt_model_request += 1
        generation, account = self.chatgpt_model_request, self.chatgpt_active_id
        self._chatgpt_show_models([])

        def current():
            return (generation == self.chatgpt_model_request and account == self.chatgpt_active_id
                    and self.chatgpt_connected and not self.chatgpt_busy)

        def loaded(data):
            if current():
                if isinstance(data, dict) and data.get('ok') is True and isinstance(data.get('models'), list):
                    self._chatgpt_show_models(data['models'])
                else:
                    failed(data.get('message') if isinstance(data, dict) else '')

        def failed(message):
            if current():
                self.chatgpt_notice.set_text(str(message or '暂时无法取得 ChatGPT 模型列表。'))

        self._async(lambda: self.service.request('chatgpt_models', {'profile_id': account}), loaded, failed)

    def _chatgpt_model_changed(self, *_):
        if not self.chatgpt_updating:
            self.chatgpt_last_model = self.chatgpt_models.get_active_id() or ''

    def _chatgpt_select(self, *_):
        profile = self.chatgpt_accounts.get_active_id()
        if self.chatgpt_updating or self.chatgpt_busy or not profile:
            return
        if profile == self.chatgpt_active_id:
            self._chatgpt_update_controls()
            self._chatgpt_load_status()
        elif any(self.chatgpt_profiles.get(profile, {}).get(flag) for flag in ('authenticated', 'connected')):
            self._chatgpt_profile_action('chatgpt_select', {'profile_id': profile})
        else:
            self.chatgpt_notice.set_text('选择的账号需要授权。点击「重新授权」继续。')
            self._chatgpt_update_controls()

    def _chatgpt_disconnect(self, *_):
        if not self.chatgpt_busy and self.chatgpt_authenticated and self.chatgpt_accounts.get_active_id() == self.chatgpt_active_id:
            self._chatgpt_profile_action('chatgpt_sign_out', {'profile_id': self.chatgpt_active_id})

    def _chatgpt_profile_action(self, action, payload):
        self.chatgpt_busy = True
        self.chatgpt_status_request += 1
        self._chatgpt_update_controls()

        def failed(message):
            self.chatgpt_updating = True
            try:
                self.chatgpt_accounts.set_active_id(self.chatgpt_active_id)
            finally:
                self.chatgpt_updating = False
            self._chatgpt_error(message)

        def completed(status):
            self.chatgpt_busy = False
            if isinstance(status, dict) and status.get('ok') is True:
                self._chatgpt_show_status(status)
            else:
                failed(status.get('message') if isinstance(status, dict) else '')

        self._async(lambda: self.service.request(action, payload), completed, failed)

    def _chatgpt_start_sign_in(self, *_, new_account=False):
        if self.chatgpt_busy:
            return
        self.chatgpt_busy = True
        self.chatgpt_status_request += 1
        self.chatgpt_notice.set_text('正在打开浏览器，请完成 ChatGPT 登录与授权。')
        self._chatgpt_update_controls()
        profile = '' if new_account else self.chatgpt_accounts.get_active_id() or ''
        self._start('chatgpt_sign_in', {'profile_id': profile}, done=self._chatgpt_sign_in_finished)

    def _chatgpt_cancel_sign_in(self, *_):
        ident = self.chatgpt_job_id
        if not ident:
            return
        self.chatgpt_cancel.set_sensitive(False)

        def requested(_):
            if self.chatgpt_job_id == ident:
                self.chatgpt_notice.set_text('正在取消登录…')

        def failed(message):
            if self.chatgpt_job_id == ident:
                self.chatgpt_notice.set_text(str(message))
                self._chatgpt_update_controls()

        self._async(lambda: self.service.cancel(ident), requested, failed)

    def _chatgpt_sign_in_finished(self, status):
        first_connection = not any(profile.get('connected') for profile in self.chatgpt_profiles.values())
        self.chatgpt_busy = False
        self.chatgpt_job_id = ''
        self._chatgpt_show_status(status)
        if isinstance(status, dict) and status.get('ok') is True and status.get('connected') is True:
            self.ai_provider.set_active_id('chatgpt')
            settings = {'ai_provider': 'chatgpt', 'chatgpt_model': self.chatgpt_last_model}

            def saved(result):
                if isinstance(result, dict) and result.get('ok') is False:
                    failed(result.get('message') or '设置未保存。')
                elif first_connection:
                    self._chatgpt_welcome()

            def failed(message):
                self.chatgpt_notice.set_text('授权成功。请点击「保存设置」后使用 ChatGPT。' + str(message))

            self._async(lambda: self.service.save_settings(settings), saved, failed)

    def _chatgpt_welcome(self):
        welcome = Gtk.MessageDialog(transient_for=self, modal=True, destroy_with_parent=True,
                                    message_type=Gtk.MessageType.INFO, text='已启用 ChatGPT 套餐')
        welcome.format_secondary_text('符合条件的 AI 请求会使用你的 ChatGPT 套餐或可用额度。可在 ChatGPT 用量设置中管理授权和限额。')
        welcome.add_button('知道了', Gtk.ResponseType.CLOSE)
        welcome.connect('response', lambda dialog, _: dialog.destroy())
        welcome.show()

    def _use_deepseek_flash(self, *_):
        self.ai_provider.set_active_id('api_key')
        if self._ai_hostname() == 'api.openai.com':
            self.setting_entries['api_key'].set_text('')
        self.setting_entries['api_base'].set_text('https://api.deepseek.com')
        self.setting_entries['model'].set_text('deepseek-flash')
        self.setting_entries['search_provider'].set_text('deepseek')
        self.search_mode.set_active_id('tools')
        self._search_mode_changed()
        self._message('已填入 DeepSeek Flash 与官方搜索配置。请核对 DeepSeek API Key 后保存。')

    def _ai_hostname(self):
        try:
            return urlsplit(self.setting_entries['api_base'].get_text().strip()).hostname
        except ValueError:
            return None

    def _use_openai_gpt(self, *_):
        self.ai_provider.set_active_id('api_key')
        if self._ai_hostname() != 'api.openai.com':
            self.setting_entries['api_key'].set_text('')
        self.setting_entries['api_base'].set_text('https://api.openai.com/v1')
        self.setting_entries['model'].set_text('gpt-5-mini')
        self.search_mode.set_active_id('native')
        self._message('已填入 OpenAI API Key 配置。填写 OpenAI API Key 后保存，或点击测试 AI 连接。')

    def _open_official_login(self, *_):
        self._async(lambda: webbrowser.open('https://i.chaoxing.com'),
                    lambda opened: self.session_notice.set_text('已请求系统浏览器打开学习通官方登录页。登录后回到此处点击同步。' if opened else '系统浏览器未能打开，请手动访问 https://i.chaoxing.com 登录后同步。'))

    def _sync_session(self, *_):
        self.session_sync_button.set_sensitive(False)
        self.session_notice.set_text('正在读取可用的本机学习通登录会话并验证课程…')
        def loaded(data):
            self.session_sync_button.set_sensitive(True)
            if not isinstance(data, dict):
                self.session_notice.set_text('同步结果无法识别。请先在浏览器登录学习通后重试。')
                return
            message = str(data.get('message') or ('登录状态已同步。' if data.get('ok') else '未找到有效会话，请先在浏览器登录学习通。'))
            self.session_notice.set_text(message)
            if data.get('ok'):
                courses = data.get('courses')
                if isinstance(courses, list):
                    self._courses_loaded({'courses': courses})
                if data.get('source'):
                    self.session_notice.set_text(message + '\n会话来源：' + str(data['source']))
            self._message(message)
        def failed(message):
            self.session_sync_button.set_sensitive(True)
            self.session_notice.set_text('同步失败：' + str(message) + '。可在浏览器登录后重试，或展开备用登录。')
        self._async(lambda: self.service.request('session_sync', {}), loaded, failed)

    def _choose_cookie(self, *_):
        dialog = Gtk.FileChooserDialog(title='选择 Cookie 文件', transient_for=self, action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons('取消', Gtk.ResponseType.CANCEL, '选择', Gtk.ResponseType.OK)
        if dialog.run() == Gtk.ResponseType.OK:
            self.cookie_path.set_text(dialog.get_filename())
        dialog.destroy()

    def _import_cookie(self, *_):
        path = self.cookie_path.get_text().strip()
        if not path or not Path(path).expanduser().is_absolute():
            self._message('请先选择 Cookie 文本文件的绝对路径。')
            return
        self._request('import_session', {'path': path}, lambda data: self._message(data.get('message', '会话已导入。')))

    def _settings_loaded(self, settings):
        data_dir = settings.get('data_dir')
        self.work_image_cache_root = Path(str(data_dir)).expanduser().resolve() / 'assignment-images' if data_dir and Path(str(data_dir)).expanduser().is_absolute() else None
        for key, entry in self.setting_entries.items():
            entry.set_text(str(settings.get(key) or ''))
        self.settings_paths.set_text('设置目录：%s\n任务数据目录：%s' % (settings.get('config_dir', '未提供'), settings.get('data_dir', '未提供')))
        self.search_enabled.set_active(bool(settings.get('search_enabled', True)))
        self.work_search.set_active(bool(settings.get('search_enabled', True)))
        mode = str(settings.get('search_mode') or 'tools')
        self.search_mode.set_active_id(mode if mode in ('tools', 'native', 'prefetch') else 'tools')
        self.chatgpt_last_model = str(settings.get('chatgpt_model') or '')
        self.chatgpt_updating = True
        try:
            if not self.chatgpt_models.set_active_id(self.chatgpt_last_model):
                self.chatgpt_models.set_active(0)
        finally:
            self.chatgpt_updating = False
        self.ai_provider.set_active_id('chatgpt' if settings.get('ai_provider') == 'chatgpt' else 'api_key')
        self._ai_provider_changed()
        if self.work_data.get('questions'):
            self._render_work_images()
            self._refresh_work_actions()

    def _search_mode_changed(self, *_):
        if not hasattr(self, 'search_mode_notice'):
            return
        notices = {
            'tools': '模型自行决定是否调用搜索工具；搜索工具使用下方配置的服务与 Key。',
            'native': '需要 API 服务支持 Responses / web_search；此模式不使用下方搜索服务与 Key。',
            'prefetch': '兼容模式先检索再把来源发给模型；检索服务与 Key 使用下方配置。',
        }
        message = notices.get(self.search_mode.get_active_id(), '请选择模型联网方式。')
        if self.setting_entries['model'].get_text().strip().lower() == 'deepseek-flash':
            message += '\nDeepSeek Flash 请使用“模型调用搜索工具”；DeepSeek 官方原生 web_search 仅支持 Pro，请勿为 Flash 选择原生联网。'
        self.search_mode_notice.set_text(message)

    def _settings_payload(self):
        data = {key: entry.get_text() for key, entry in self.setting_entries.items()}
        data['search_enabled'] = self.search_enabled.get_active()
        data['search_mode'] = self.search_mode.get_active_id() or 'tools'
        data['ai_provider'] = self.ai_provider.get_active_id() or 'api_key'
        data['chatgpt_model'] = self.chatgpt_last_model
        return data

    def _ai_test_result(self, data):
        self.ai_test_button.set_sensitive(True)
        state = str(data.get('status', '')).lower() if isinstance(data, dict) else ''
        success = isinstance(data, dict) and data.get('ok') is True and state not in ('error', 'failed', 'needs_input', 'cancelled', 'timeout')
        reason = str(data.get('message') or data.get('error') or ('服务已返回有效模型响应。' if success else '服务未返回通过测试的结果。')) if isinstance(data, dict) else '连接测试结果无法识别。'
        title = 'AI 连接成功' if success else 'AI 连接失败'
        self.ai_test_notice.set_text(title + '：' + reason + ('\n连接成功不代表已执行联网搜索。' if success else ''))
        context = self.ai_test_notice.get_style_context()
        context.remove_class('subtitle')
        context.remove_class('result-error' if success else 'result-success')
        context.add_class('result-success' if success else 'result-error')
        self._message(title + '：' + reason)

    def _save_then_start(self, action):
        data = self._settings_payload()
        if action == 'ai_test':
            self.ai_test_button.set_sensitive(False)
            self.ai_test_notice.set_text('正在保存设置并测试 AI 连接…')
            self.ai_test_notice.get_style_context().remove_class('result-success')
            self.ai_test_notice.get_style_context().remove_class('result-error')
            self.ai_test_notice.get_style_context().add_class('subtitle')
        def failed(message):
            if action == 'ai_test':
                self._ai_test_result({'ok': False, 'error': str(message)})
            else:
                self._message('设置保存失败：' + str(message))
        def saved(result):
            if isinstance(result, dict) and result.get('ok') is False:
                failed(result.get('message') or '设置未保存。')
                return
            self.work_search.set_active(data['search_enabled'])
            self._start(action, {}, done=self._ai_test_result if action == 'ai_test' else None)
        self._async(lambda: self.service.save_settings(data), saved, failed)

    def _save_settings(self, *_):
        data = self._settings_payload()
        self._async(lambda: self.service.save_settings(data), lambda _: self._message('设置已保存。'))

    def _jobs(self):
        page = self.pages['jobs']
        page.pack_start(label('后台任务状态与返回结果。取消会请求任务停止；已经提交的远程操作不能通过取消撤回。', 'subtitle'), False, False, 0)
        self.job_store = Gtk.ListStore(str, str, str, str)
        self.job_tree = Gtk.TreeView(model=self.job_store)
        for index, title in enumerate(('任务 ID', '任务', '状态', '进度 / 提示')):
            renderer = Gtk.CellRendererText()
            renderer.set_property('ellipsize', Pango.EllipsizeMode.END)
            renderer.set_property('max-width-chars', 65 if index == 3 else 28)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_min_width((125, 120, 120, 240)[index])
            column.set_expand(index == 3)
            self.job_tree.append_column(column)
        self.job_tree.get_selection().connect('changed', self._job_selected)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(260)
        scroll.add(self.job_tree)
        page.pack_start(scroll, True, True, 0)
        row = self._row(page)
        self._button(row, '刷新任务', lambda _: self._poll())
        self._button(row, '取消选中任务', self._cancel_job)
        self.job_progress = Gtk.ProgressBar(show_text=True)
        page.pack_start(self.job_progress, False, False, 0)
        self.job_output, scroll = textbox(260)
        page.pack_start(scroll, True, True, 0)

    def _poll(self):
        if self.closed or self.closing:
            return False
        if self.polling:
            return True
        self.polling = True
        def loaded(jobs):
            self.polling = False
            if isinstance(jobs, dict):
                jobs = jobs.get('jobs', [])
            selected = self._selected_job_id()
            self.job_store.clear()
            for job in jobs:
                ident = str(job.get('id', job.get('job_id', '')))
                state = str(job.get('status', job.get('state', '')))
                result = job.get('result')
                if isinstance(result, dict) and result.get('ok') is False and state.lower() in ('done', 'completed', 'success', 'succeeded'):
                    state = str(result.get('status') or 'error')
                    if state.lower() in ('done', 'completed', 'success', 'succeeded'):
                        state = 'error'
                progress = job.get('progress', '')
                message = str(job.get('message') or job.get('error') or (result.get('message') or result.get('error') if isinstance(result, dict) else '') or '')
                if job.get('action') == 'ai_test' and isinstance(result, dict):
                    if result.get('ok') is False:
                        message = 'AI 连接失败：' + str(result.get('message') or result.get('error') or '服务未通过连接测试。')
                    elif result.get('ok') is True:
                        message = 'AI 连接成功：' + str(result.get('message') or '服务已返回有效模型响应。')
                self.job_store.append([ident, ACTION_NAMES.get(str(job.get('action')), str(job.get('name', '任务'))), STATUS_NAMES.get(state, state), '%d%%' % int(progress_value(progress) * 100) + (' · ' + human_message(message) if message else '')])
                self.seen_jobs[ident] = job
                if ident == self.review_panel.job_id:
                    self.review_panel.update_progress(state, progress_value(progress), human_message(message))
                if ident == self.chatgpt_job_id and state.lower() not in TERMINAL and message:
                    self.chatgpt_notice.set_text(human_message(message))
                if ident == self.course_job_id:
                    fraction = progress_value(progress)
                    self.course_progress.set_fraction(fraction)
                    self.course_progress.set_text('%s · %d%%' % (STATUS_NAMES.get(state, state), int(fraction * 100)))
                    if message:
                        self.course_notice.set_text(human_message(message))
                    events = job.get('events', [])[-15:]
                    if events:
                        put(self.course_events, '\n'.join(human_message(event.get('message', '')) if isinstance(event, dict) else human_message(event) for event in events))
                if ident == selected:
                    self.job_tree.get_selection().select_path(len(self.job_store) - 1)
                callbacks = self.job_callbacks.get(ident)
                if callbacks and (state.lower() in TERMINAL or job.get('finished_at')):
                    self.job_callbacks.pop(ident, None)
                    done, output = callbacks
                    result = job.get('result')
                    if output:
                        put(output, result if result is not None else job)
                    if done and job.get('action') == 'chatgpt_sign_in' and state.lower() in {'cancelled', 'canceled', 'stopped'}:
                        done({'ok': False, 'status': state, 'message': 'ChatGPT 登录已取消。'})
                    elif done and result is not None:
                        done(result)
                    elif done and job.get('action') in ('sign', 'ai_test', 'review_export', 'chatgpt_sign_in'):
                        done({'ok': False, 'status': state, 'message': message or '任务未完成，请检查后重试。'})
                    self._message(human_message(message) or ('任务完成。' if state.lower() in {'done', 'completed', 'success', 'succeeded'} else '任务已结束：' + STATUS_NAMES.get(state, state)))
        def failed(_):
            self.polling = False
        self._async(self.service.jobs, loaded, failed)
        return True

    def _selected_job_id(self):
        model, iterator = self.job_tree.get_selection().get_selected()
        return model[iterator][0] if iterator is not None else None

    def _job_selected(self, *_):
        job = self.seen_jobs.get(self._selected_job_id())
        if not job:
            return
        put(self.job_output, job)
        value = job.get('progress', 0)
        try:
            value = float(value)
            if value > 1:
                value /= 100
            self.job_progress.set_fraction(max(0, min(1, value)))
            self.job_progress.set_text('%d%%' % int(max(0, min(1, value)) * 100))
        except (ValueError, TypeError):
            self.job_progress.set_fraction(0)
            self.job_progress.set_text(str(value))

    def _cancel_job(self, *_):
        ident = self._selected_job_id()
        if ident:
            self._async(lambda: self.service.cancel(ident), lambda _: self._message('已请求停止任务。'))
        else:
            self._message('请先选择任务。')

    def _screenshot(self):
        try:
            window = self.get_window()
            width, height = self.get_size()
            image = Gdk.pixbuf_get_from_window(window, 0, 0, width, height)
            Path(self.smoke_path).parent.mkdir(parents=True, exist_ok=True)
            image.savev(str(self.smoke_path), 'png', [], [])
        finally:
            self.destroy()
        return False


def main(smoke_path=None):
    window = Window(smoke_path=smoke_path)
    try:
        Gtk.main()
    finally:
        window._shutdown_service()
