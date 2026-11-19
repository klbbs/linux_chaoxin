"""GTK integration checks with no account, network or phone side effects."""
import os
import json
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from chaoxing_linux.workspace import Window, Gdk, GLib
    GTK_AVAILABLE = bool(os.environ.get('DISPLAY') and Gdk.Display.get_default())
except (ImportError, ValueError):
    GTK_AVAILABLE = False


class FakeService:
    def __init__(self):
        self.calls = []
        self.records = []
        self.locations = []
        self.works = []
        self.synced_courses = []
        self.setting_values = {'backend_path': '/tmp/backend', 'search_enabled': False, 'search_mode': 'tools'}
        self.saved_settings = []
        self.chatgpt_status = {'ok': True, 'profiles': [], 'active_id': '', 'connected': False}
        self.chatgpt_models = []

    def settings(self):
        return dict(self.setting_values)

    def save_settings(self, values):
        self.saved_settings.append(dict(values))
        self.setting_values.update(values)
        return {'ok': True}

    def jobs(self):
        return self.records

    def request(self, action, payload):
        self.calls.append((action, payload))
        if action == 'chatgpt_status':
            return self.chatgpt_status
        if action == 'chatgpt_models':
            return {'ok': True, 'models': self.chatgpt_models}
        if action == 'work_list':
            return {'ok': True, 'works': list(self.works)}
        if action == 'session_sync':
            return {'ok': True, 'courses': list(self.synced_courses), 'source': '本机已登录网页会话', 'message': '同步成功'}
        if action == 'locations_list':
            return {'ok': True, 'locations': list(self.locations)}
        if action == 'locations_save':
            location = dict(payload, id='saved-local')
            self.locations.append(location)
            return {'ok': True, 'location': location}
        if action == 'locations_delete':
            self.locations = [item for item in self.locations if item['id'] != payload['id']]
            return {'ok': True}
        if action == 'sign_history':
            return {'ok': True, 'history': list(self.records)}
        return {'ok': True}

    def start(self, action, payload):
        self.calls.append((action, payload))
        return {'id': 'local-job', 'status': 'queued'}

    def cancel(self, ident):
        self.calls.append(('cancel', ident))
        return {'ok': True}


@unittest.skipUnless(GTK_AVAILABLE, 'Requires GTK3 and a display')
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.image_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.image_tmp.cleanup)
        self.service.setting_values['data_dir'] = self.image_tmp.name
        self.window = Window(self.service)
        self.pump()
        self.addCleanup(self.window.destroy)

    def pump(self):
        end = time.monotonic() + 0.08
        while time.monotonic() < end:
            while GLib.MainContext.default().iteration(False):
                pass
            time.sleep(.002)

    def test_startup_does_not_access_account_or_phone(self):
        self.assertEqual(self.service.calls, [])
        self.assertFalse(self.window.phone_auto.get_active())
        self.assertEqual(self.window.setting_entries['backend_path'].get_text(), '/tmp/backend')

    def test_navigation_stays_selected_when_entering_ai_settings_from_review(self):
        self.window.nav_buttons['review'].clicked()
        self.assertEqual(self.window.stack.get_visible_child_name(), 'review')
        self.window.review_panel.open_ai_settings()
        self.assertEqual(self.window.stack.get_visible_child_name(), 'settings')
        self.assertEqual(self.window.settings_sections.get_visible_child_name(), 'ai')
        self.assertTrue(self.window.nav_buttons['settings'].get_active())
        self.assertFalse(self.window.nav_buttons['review'].get_active())

    def test_review_export_routes_the_selected_course_and_shows_partial_result(self):
        panel = self.window.review_panel
        self.window.stack.set_visible_child_name('review')
        self.assertFalse(panel.export_button.get_sensitive())
        courses = [{'id': '1', 'name': '示例课程一'}, {'id': '2', 'name': '示例课程二'}]
        self.window._courses_loaded({'courses': courses})
        panel.course_select.set_active(1)
        destination = Path(self.image_tmp.name)
        panel.folder.set_filename(str(destination))
        self.assertTrue(panel.export_button.get_sensitive())
        self.assertEqual(self.service.calls, [])
        panel.export_button.clicked()
        self.pump()
        self.assertEqual(self.service.calls, [('review_export', {
            'course': courses[1], 'output_dir': str(destination),
        })])
        self.assertFalse(panel.export_button.get_sensitive())
        self.service.records = [{'id': 'local-job', 'action': 'review_export', 'status': 'running',
                                 'progress': .5, 'message': '正在读取作业'}]
        self.window._poll()
        self.pump()
        self.assertEqual(panel.progress.get_fraction(), .5)
        self.assertEqual(panel.notice.get_text(), '正在读取作业')

        self.service.records[0].update(status='partial', progress=1, result={
            'ok': True, 'status': 'partial', 'message': '部分内容不可读，已导出可用内容。',
            'files': {'docx': str(destination / '复习.docx'), 'pdf': str(destination / '复习.pdf')},
            'counts': {'chapters': 8, 'assignments': 3, 'materials': 2}, 'summary_mode': 'local',
            'issues': ['一项资料需要课程教师开放。'],
        })
        self.window._poll()
        self.pump()
        self.assertIn('章节 8 项', panel.coverage.get_text())
        self.assertIn('本机整理', panel.coverage.get_text())
        self.assertTrue(panel.issues.get_visible())
        self.assertIn('课程教师开放', panel.issues_text.get_text())
        self.assertTrue(panel.file_buttons['docx'].get_sensitive())
        self.assertTrue(panel.file_buttons['pdf'].get_sensitive())
        self.assertTrue(panel.export_button.get_sensitive())

    def test_review_export_recovers_after_job_start_failure_and_terminal_error(self):
        panel = self.window.review_panel
        self.window._courses_loaded({'courses': [{'id': '1', 'name': '示例课程'}]})
        panel.folder.set_filename(self.image_tmp.name)
        with patch.object(self.service, 'start', side_effect=ValueError('已有四个任务')):
            panel.export_button.clicked()
            self.pump()
        self.assertIn('已有四个任务', panel.notice.get_text())
        self.assertTrue(panel.export_button.get_sensitive())

        panel.export_button.clicked()
        self.pump()
        self.service.records = [{'id': 'local-job', 'action': 'review_export', 'status': 'error',
                                 'message': '未找到 PDF 导出程序'}]
        self.window._poll()
        self.pump()
        self.assertIn('未找到 PDF 导出程序', panel.notice.get_text())
        self.assertFalse(panel.file_buttons['pdf'].get_sensitive())
        self.assertTrue(panel.export_button.get_sensitive())

    def test_manual_answers_keep_ids_and_explicit_submission_mode(self):
        self.window.work_url.set_text('https://example.org/work')
        self.window._work_loaded({'questions': [{'id': 'q1', 'text': 'Question', 'options': []}], 'context': {'token': 'local'}})
        self.window._answer_edited(None, '0', 'A')
        self.window._submit_work('save')
        self.pump()
        action, payload = self.service.calls[-1]
        self.assertEqual(action, 'work_submit')
        self.assertEqual(payload['mode'], 'save')
        self.assertEqual(payload['answers'], [{'id': 'q1', 'answer': 'A', 'status': 'ok'}])
        self.window._work_loaded({'questions': [{'id': 'q2', 'text': 'Other question'}]})
        self.assertEqual(self.window.work_data['answers'][0]['status'], 'unknown')

    def test_needs_input_job_still_delivers_editable_result(self):
        delivered = []
        self.window.job_callbacks['local-job'] = (delivered.append, None)
        self.service.records = [{'id': 'local-job', 'status': 'needs_input', 'result': {'questions': [], 'answers': []}}]
        self.window._poll()
        self.pump()
        self.assertEqual(len(delivered), 1)
        self.assertNotIn('local-job', self.window.job_callbacks)

    def test_secrets_stay_invisible_and_long_labels_do_not_expand_window(self):
        self.assertEqual(self.window.course_speed.get_text(), '1.0')
        from gi.repository import Gtk
        color = self.window.course_speed.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        self.assertLess((color.red + color.green + color.blue) / 3, .65)
        self.assertTrue(self.window.setting_entries['search_provider'].get_placeholder_text().startswith('bing_rss'))
        for key in ('password', 'api_key', 'search_key'):
            self.assertFalse(self.window.setting_entries[key].get_visibility())
        width, height = self.window.get_size()
        self.window._courses_loaded({'courses': [{'id': 'one', 'name': 'A' * 10000}]})
        self.window._message('B' * 10000)
        self.pump()
        after = self.window.get_size()
        preferred_width, _ = self.window.get_preferred_width()
        self.assertLessEqual(preferred_width, max(1240, width))
        # The window manager may maximize a new window after GTK has mapped it.
        if not self.window.is_maximized():
            self.assertLessEqual(after.width, max(1240, width))
            self.assertLessEqual(after.height, max(880, height))

    def test_destroy_shuts_down_service_once(self):
        calls = []
        self.service.shutdown = lambda timeout=5: calls.append(timeout)
        self.window.destroy()
        self.window._shutdown_service()
        self.assertEqual(calls, [5])

    def test_close_waits_asynchronously_for_shutdown(self):
        calls = []
        def shutdown(timeout=5):
            time.sleep(.025)
            calls.append(timeout)
        self.service.shutdown = shutdown
        started = time.monotonic()
        self.assertTrue(self.window._close_request())
        self.assertLess(time.monotonic() - started, .02)
        self.pump()
        self.assertTrue(self.window.closed)
        self.assertEqual(calls, [5])

    def test_course_table_explains_unverified_and_blocked_tasks(self):
        self.window._course_result({'tasks': [
            {'id': 'a', 'name': '视频一', 'type': 'video', 'status': 'pending', 'point': {'title': '第一章'}},
            {'id': 'b', 'name': '后续章节', 'type': 'chapter', 'status': 'locked'},
            {'id': 'c', 'name': '新章节', 'type': 'chapter', 'status': 'not_open'},
        ]})
        self.assertEqual(self.window.course_store[0][0], '第一章 / 视频一')
        self.assertEqual(self.window.course_store[0][1], '视频')
        self.assertEqual(self.window.course_store[1][2], '未解锁')
        self.assertEqual(self.window.course_store[2][2], '未开放')
        self.window._course_result({'results': [
            {'id': 'a', 'name': '视频一', 'type': 'video', 'status': 'reported', 'verified': False},
            {'id': 'b', 'name': '直播', 'type': 'live', 'status': 'unsupported'},
        ]})
        self.assertIn('未核对完成', self.window.course_store[0][2])
        self.assertEqual(self.window.course_store[0][0], '第一章 / 视频一')
        self.assertEqual(self.window.course_store[1][2], '需手机处理')

    def test_course_progress_and_events_use_readable_states(self):
        self.window.course_job_id = 'local-job'
        self.service.records = [{'id': 'local-job', 'action': 'course_run', 'status': 'running',
                                 'progress': .5, 'message': '视频一：reported',
                                 'events': [{'message': '视频一：reported'}]}]
        self.window._poll()
        self.pump()
        self.assertEqual(self.window.course_progress.get_fraction(), .5)
        self.assertIn('执行中', self.window.course_progress.get_text())
        self.assertIn('未核对完成', self.window.course_notice.get_text())
        self.assertEqual(self.window.job_store[0][1], '后台执行课程')
        self.assertEqual(self.window.job_store[0][2], '执行中')

    def test_sign_result_distinguishes_success_from_missing_information(self):
        self.window._sign_result({'ok': False, 'status': 'needs_input', 'message': '请提供有效二维码'})
        self.assertIn('需补充信息', self.window.sign_notice.get_text())
        self.assertIn('请提供有效二维码', self.window.sign_notice.get_text())
        self.window._sign_result({'ok': True, 'status': 'success', 'message': '您已签到过了'})
        self.assertIn('签到成功', self.window.sign_notice.get_text())

    def test_sign_uses_backend_contract(self):
        self.window._courses_loaded({'courses': [{'id': 'course1', 'name': 'Course'}]})
        self.window.activity_id.set_text('123')
        self.window.sign_mode.set_active_id('code')
        self.window.sign_code.set_text('456')
        self.window._sign_start()
        self.pump()
        action, payload = self.service.calls[-1]
        self.assertEqual(action, 'sign')
        self.assertEqual(payload['sign_type'], 'code')
        self.assertEqual(payload['sign_code'], '456')

    def prepare_sign(self, mode='normal'):
        self.window._courses_loaded({'courses': [{'id': 'course1', 'name': '示例课程'}]})
        self.window._activities_loaded({'activities': [{'id': '123', 'name': '示例签到', 'sign_type': mode}]})

    def test_sign_only_shows_fields_for_the_selected_mode(self):
        self.assertFalse(any(block.get_visible() for block in self.window.sign_blocks.values()))
        for mode in self.window.sign_modes:
            self.window.sign_mode.set_active_id(mode)
            self.assertEqual([key for key, block in self.window.sign_blocks.items() if block.get_visible()], [mode])

    def test_activity_mode_inference_and_course_changes_do_not_submit(self):
        self.window._courses_loaded({'courses': [{'id': '1', 'name': 'One'}, {'id': '2', 'name': 'Two'}]})
        for value, expected in [('0', 'normal'), ('2', 'qr'), ('3', 'gesture'), ('4', 'location'), ('5', 'code')]:
            self.window._activities_loaded({'activities': [{'id': '123', 'otherId': value}]})
            self.assertEqual(self.window.sign_mode.get_active_id(), expected)
        self.window._activities_loaded({'activities': [{'id': '123', 'name': '类型未知'}]})
        self.assertEqual(self.window.sign_mode.get_active_id(), '')
        self.assertIn('手动选择', self.window.sign_activity_note.get_text())
        self.assertFalse(self.window.sign_submit_button.get_sensitive())
        self.window.sign_course.set_active(1)
        self.assertEqual(self.window.activity_id.get_text(), '')
        self.assertEqual(self.window.activity_items, [])
        self.assertEqual(self.service.calls, [])

    def test_map_pick_updates_location_and_summary_without_submitting(self):
        self.prepare_sign('location')
        point = {'latitude': 39.9, 'longitude': 116.4, 'coordinate_system': 'gcj02',
                 'name': '示例图书馆', 'address': '示例大学图书馆三楼'}
        with patch('chaoxing_linux.map_picker.choose_location', return_value=point) as choose:
            self.window._choose_sign_location()
        self.assertEqual(choose.call_count, 1)
        self.assertEqual(self.window.sign_address.get_text(), point['address'])
        self.assertIn(point['address'], self.window.sign_summary.get_text())
        self.assertTrue(self.window.sign_submit_button.get_sensitive())
        self.assertEqual(self.service.calls, [])
        with patch('chaoxing_linux.map_picker.choose_location', return_value=None):
            self.window._choose_sign_location()
        self.assertEqual(self.window.sign_address.get_text(), point['address'])

    def test_locations_save_use_and_delete_are_local_only(self):
        self.prepare_sign('location')
        self.window._set_sign_location({'latitude': 39.9, 'longitude': 116.4, 'coordinate_system': 'wgs84',
                                        'name': '自定义标签', 'address': '示例地址'})
        self.window._save_sign_location()
        self.pump()
        self.assertEqual([call[0] for call in self.service.calls], ['locations_save', 'locations_list'])
        self.assertEqual(len(self.service.locations), 1)
        self.window.sign_address.set_text('changed')
        self.window._use_saved_location()
        self.assertEqual(self.window.sign_address.get_text(), '示例地址')
        self.assertEqual(self.window.coordinate_system.get_active_id(), 'wgs84')
        self.window._delete_saved_location()
        self.pump()
        self.assertEqual(self.service.locations, [])
        self.assertNotIn('sign', [call[0] for call in self.service.calls])

    def test_missing_location_information_is_visible_and_blocks_submit(self):
        self.prepare_sign('location')
        self.assertIn('地图选择地点', self.window.sign_validation.get_text())
        self.assertFalse(self.window.sign_submit_button.get_sensitive())
        self.window._sign_start()
        self.pump()
        self.assertEqual(self.service.calls, [])
        self.window.latitude.set_text('nan')
        self.window.longitude.set_text('116.4')
        self.window.sign_address.set_text('示例地址')
        self.assertFalse(self.window.sign_submit_button.get_sensitive())

    def test_qr_full_url_and_opaque_token_use_distinct_payload_fields(self):
        self.prepare_sign('qr')
        url = 'https://example.org/sign?enc=secret&activeId=123'
        self.window.sign_qr.set_text(url)
        self.window._sign_start()
        self.pump()
        action, payload = self.service.calls[-1]
        self.assertEqual(action, 'sign')
        self.assertEqual(payload['qr_content'], url)
        self.assertNotIn('enc', payload)
        self.window._sign_result({'ok': False, 'status': 'needs_input', 'message': '示例失效'})
        self.window.sign_qr.set_text('opaque-token')
        self.window._sign_start()
        self.pump()
        self.assertEqual(self.service.calls[-1][1]['enc'], 'opaque-token')
        self.assertNotIn('qr_content', self.service.calls[-1][1])

    def test_qr_url_without_enc_stays_in_missing_information_state(self):
        self.prepare_sign('qr')
        self.window.sign_qr.set_text('https://example.org/sign?activeId=123')
        self.assertIn('enc', self.window.sign_validation.get_text())
        self.assertFalse(self.window.sign_submit_button.get_sensitive())
        self.window._sign_start()
        self.assertEqual(self.service.calls, [])

    def test_gesture_grid_builds_sequence_and_submit_does_not_send_hidden_fields(self):
        self.prepare_sign('gesture')
        for value in ['1', '2', '3', '3', '6', '9']:
            self.window._gesture_append(value)
        self.assertEqual(self.window.sign_gesture.get_text(), '12369')
        self.assertTrue(self.window.gesture_buttons[0].get_style_context().has_class('primary'))
        self.assertFalse(self.window.gesture_buttons[3].get_style_context().has_class('primary'))
        self.window.sign_code.set_text('ignored')
        self.window.sign_qr.set_text('ignored')
        self.window._sign_start()
        self.pump()
        payload = self.service.calls[-1][1]
        self.assertEqual(payload['sign_code'], '12369')
        self.assertNotIn('enc', payload)
        self.assertNotIn('latitude', payload)

    def test_local_records_load_only_when_attendance_page_is_opened(self):
        self.window.stack.set_visible_child_name('sign')
        self.pump()
        self.assertCountEqual([call[0] for call in self.service.calls], ['locations_list', 'sign_history'])
        self.window.stack.set_visible_child_name('home')
        self.window.stack.set_visible_child_name('sign')
        self.pump()
        self.assertEqual(len(self.service.calls), 2)

    def test_local_history_results_and_long_addresses_fit_window(self):
        self.prepare_sign('location')
        self.window.stack.set_transition_duration(0)
        self.window.stack.set_visible_child_name('sign')
        self.pump()
        self.window._set_sign_location({'latitude': 39.9, 'longitude': 116.4,
                                        'name': '长标签' * 150, 'address': '很长的地点地址' * 400})
        self.window._sign_history_loaded({'history': [{'status': 'completed',
            'summary': {'course': '示例课程', 'activity_id': '123', 'sign_type': 'location'},
            'result': {'status': 'success', 'message': '签到完成'}}]})
        self.pump()
        self.assertIn('示例课程', self.window.sign_history_box.get_children()[0].get_text())
        self.assertLessEqual(self.window.get_size().width, 1240)
        self.assertLessEqual(self.window.get_size().height, 880)
        scroll = self.window.stack.get_visible_child()
        # Measure scrolling with an allocated viewport, even when the window
        # manager has not painted this short-lived test window.
        viewport = Gdk.Rectangle()
        viewport.width = 1040
        viewport.height = 800
        scroll.size_allocate(viewport)
        adjustment = scroll.get_vadjustment()
        self.assertGreater(adjustment.get_upper(), adjustment.get_page_size())

    def test_stale_activity_response_is_ignored_after_course_change(self):
        self.window._courses_loaded({'courses': [{'id': '1', 'name': 'One'}, {'id': '2', 'name': 'Two'}]})
        callbacks = []
        self.window._request = lambda action, payload, done: callbacks.append(done)
        self.window._activities()
        self.window.sign_course.set_active(1)
        callbacks[0]({'activities': [{'id': '123', 'name': 'Old course', 'otherId': 0}]})
        self.assertEqual(self.window.activity_items, [])
        self.assertEqual(self.window.activity_id.get_text(), '')

    def test_activity_overview_uses_real_field_names_and_attendance_counts(self):
        self.window._courses_loaded({'courses': [{'id': '1', 'name': '示例课程'}]})
        self.window._activities_loaded({'activities': [{'id': '123', 'type': 2, 'status': 1,
            'nameOne': '实际字段中的签到名称', 'nameTwo': '老师填写的补充信息', 'otherId': '5',
            'startTime': 1700000000, 'endTime': 1999999999000, 'attendNum': 12, 'releaseNum': 32}]})
        self.assertIn('实际字段中的签到名称', self.window.activity_select.get_active_text())
        overview = self.window.activity_overview.get_text()
        self.assertIn('签到码', overview)
        self.assertIn('进行中', overview)
        detail = self.window.activity_detail.get_text()
        self.assertIn('2023-11-15 06:13:20', detail)
        self.assertIn('2033-05-18 11:33:19', detail)
        self.assertIn('已到 / 发布人数：12 / 32', detail)
        self.assertIn('老师填写的补充信息', detail)
        self.assertIn('实际字段中的签到名称', self.window.sign_summary.get_text())

    def test_activity_timestamp_seconds_milliseconds_and_custom_text(self):
        seconds = self.window._activity_time(1700000000)
        millis = self.window._activity_time(1700000000000)
        self.assertEqual(seconds, millis)
        self.assertEqual(self.window._activity_time('待老师通知'), ('待老师通知', None))
        self.assertEqual(self.window._activity_time(0), ('未提供', None))
        self.assertEqual(self.window._activity_time(None), ('未提供', None))
        self.assertEqual(self.window._activity_time('2026-11-10T02:00:00Z')[0], '2026-11-10 10:00:00')

    def test_ended_status_and_past_deadline_block_submission(self):
        self.window._courses_loaded({'courses': [{'id': '1', 'name': '示例课程'}]})
        for item in [{'id': '123', 'name': '已结束', 'status': 2, 'otherId': '0'},
                     {'id': '123', 'name': '已过期', 'endTime': 1700000000000, 'otherId': '0'},
                     {'id': '123', 'name': '已结束', 'status': 'ended', 'otherId': '0'}]:
            self.window._activities_loaded({'activities': [item]})
            self.assertIn('已结束', self.window.activity_overview.get_text())
            self.assertIn('已结束', self.window.sign_validation.get_text())
            self.assertFalse(self.window.sign_submit_button.get_sensitive())
            self.window._sign_start()
        self.assertEqual(self.service.calls, [])

    def test_activity_status_filter_excludes_non_sign_and_keeps_unknown_in_all(self):
        self.window._courses_loaded({'courses': [{'id': '1', 'name': '示例课程'}]})
        self.window._activities_loaded({'activities': [
            {'id': '1', 'nameOne': '进行中签到', 'type': 2, 'status': 1, 'otherId': '0'},
            {'id': '2', 'nameTwo': '已结束签到', 'type': 2, 'status': 2, 'otherId': '3'},
            {'id': '3', 'title': '未知状态签到', 'type': 2, 'status': 9},
            {'id': '4', 'name': '明确非签到活动', 'type': 3, 'status': 1, 'otherId': '4'},
        ]})
        self.assertEqual(len(self.window.activity_items), 3)
        self.window.activity_filter.set_active_id('active')
        self.assertEqual([item['id'] for item in self.window.activity_items], ['1'])
        self.window.activity_filter.set_active_id('ended')
        self.assertEqual([item['id'] for item in self.window.activity_items], ['2'])
        self.window.activity_filter.set_active_id('all')
        self.window.activity_select.set_active(2)
        self.assertIn('未提供（状态编号 9）', self.window.activity_overview.get_text())
        self.assertIn('签到（方式未提供）', self.window.activity_overview.get_text())
        self.assertEqual(self.window.sign_mode.get_active_id(), '')
        self.assertIsNone(self.window._activity_sign_mode({'type': 3, 'otherId': '4'}))
        self.assertEqual(self.service.calls, [])

    def test_activity_missing_values_are_explicitly_unprovided(self):
        self.window._courses_loaded({'courses': [{'id': '1', 'name': '示例课程'}]})
        self.window._activities_loaded({'activities': [{'activeId': '123'}]})
        self.assertIn('名称：未提供', self.window.activity_overview.get_text())
        self.assertIn('类型：未提供', self.window.activity_overview.get_text())
        self.assertIn('状态：未提供', self.window.activity_overview.get_text())
        self.assertIn('截止：未提供', self.window.activity_detail.get_text())
        self.assertIn('已签人数：未提供', self.window.activity_detail.get_text())
        self.assertEqual(self.window.activity_id.get_text(), '123')

    def test_manual_activity_id_clears_old_metadata_and_requires_mode_selection(self):
        self.prepare_sign('normal')
        self.window.activity_id.set_text('456')
        self.assertEqual(self.window.activity_select.get_active(), -1)
        self.assertEqual(self.window.sign_mode.get_active_id(), '')
        self.assertEqual(self.window._selected_sign_activity(), {})
        self.assertIn('手动活动 ID 456', self.window.activity_overview.get_text())
        self.assertIn('状态：未提供', self.window.activity_overview.get_text())
        self.assertIn('手动活动 456', self.window.sign_summary.get_text())
        self.assertNotIn('示例签到', self.window.sign_summary.get_text())
        self.assertFalse(self.window.sign_submit_button.get_sensitive())
        self.window._sign_start()
        self.assertEqual(self.service.calls, [])
        self.window.sign_mode.set_active_id('normal')
        self.window._sign_start()
        self.pump()
        action, payload = self.service.calls[-1]
        self.assertEqual(action, 'sign')
        self.assertEqual(payload['activity_id'], '456')
        self.assertNotIn('activity', payload)

    def test_manual_id_does_not_inherit_old_expired_activity_deadline(self):
        self.window._courses_loaded({'courses': [{'id': '1', 'name': '示例课程'}]})
        original = {'id': '123', 'nameOne': '旧的已结束活动', 'type': 2,
                    'status': 2, 'otherId': '0', 'endTime': 1700000000000}
        self.window._activities_loaded({'activities': [original]})
        self.assertIn('已结束', self.window.sign_validation.get_text())
        self.window.activity_id.set_text('789')
        self.assertNotIn('已结束', self.window.sign_validation.get_text())
        self.assertIn('截止：未提供', self.window.activity_detail.get_text())
        self.window.sign_mode.set_active_id('normal')
        self.assertTrue(self.window.sign_submit_button.get_sensitive())
        self.window._sign_start()
        self.pump()
        self.assertEqual(self.service.calls[-1][1]['activity_id'], '789')
        self.assertNotIn('activity', self.service.calls[-1][1])

    def test_selecting_activity_again_restores_only_matching_metadata(self):
        self.prepare_sign('location')
        self.window.activity_id.set_text('456')
        self.window.activity_select.set_active(0)
        self.assertEqual(self.window.activity_id.get_text(), '123')
        self.assertEqual(self.window.sign_mode.get_active_id(), 'location')
        self.assertEqual(self.window._selected_sign_activity()['id'], '123')
        self.assertIn('示例签到', self.window.sign_summary.get_text())
        self.assertNotIn('手动活动 456', self.window.sign_summary.get_text())
        self.assertEqual(self.service.calls, [])

    def test_sign_start_failure_unlocks_button_and_displays_result(self):
        self.prepare_sign('normal')
        self.service.start = lambda *args: (_ for _ in ()).throw(RuntimeError('示例连接失败'))
        self.window._sign_start()
        self.pump()
        self.assertFalse(self.window.sign_busy)
        self.assertTrue(self.window.sign_submit_button.get_sensitive())
        self.assertIn('示例连接失败', self.window.sign_notice.get_text())

    def prepare_work_list(self):
        self.window._courses_loaded({'courses': [{'id': 'c1', 'name': '课程一'}, {'id': 'c2', 'name': '课程二'}]})
        self.service.works = [
            {'id': 'w1', 'title': '第一次作业', 'url': 'https://example.org/work/1', 'status': 'not_submitted'},
            {'id': 'w2', 'title': '第二次作业', 'url': 'https://example.org/work/2', 'status': '已批阅'},
        ]
        self.window._load_work_list()
        self.pump()

    def test_work_course_list_selection_reads_questions_without_manual_link(self):
        self.prepare_work_list()
        action, payload = self.service.calls[-1]
        self.assertEqual(action, 'work_list')
        self.assertEqual(payload['course']['id'], 'c1')
        self.assertFalse(self.window.work_link_expander.get_expanded())
        self.assertIn('未提交', self.window.work_select.get_active_text())
        self.assertEqual(self.window.work_url.get_text(), 'https://example.org/work/1')
        self.window._work_action('work_load')
        self.pump()
        self.assertEqual(self.service.calls[-1][0], 'work_load')
        self.assertEqual(self.service.calls[-1][1]['url'], 'https://example.org/work/1')

    def test_work_and_course_changes_clear_questions_and_old_target(self):
        self.prepare_work_list()
        self.window._work_loaded({'questions': [{'id': 'old', 'text': '旧题'}]})
        self.window.work_select.set_active(1)
        self.assertEqual(self.window.work_data, {})
        self.assertEqual(len(self.window.answer_store), 0)
        self.assertEqual(self.window.work_url.get_text(), 'https://example.org/work/2')
        self.assertIn('已批阅', self.window.work_list_note.get_text())
        self.window._work_loaded({'questions': [{'id': 'old', 'text': '旧题'}]})
        self.window.work_course.set_active(1)
        self.assertEqual(self.window.work_items, [])
        self.assertEqual(self.window.work_url.get_text(), '')
        self.assertEqual(self.window.work_data, {})
        self.assertEqual(len(self.window.answer_store), 0)

    def test_stale_work_list_after_course_change_is_ignored(self):
        import threading
        self.window._courses_loaded({'courses': [{'id': 'c1', 'name': 'One'}, {'id': 'c2', 'name': 'Two'}]})
        entered, release = threading.Event(), threading.Event()
        def request(action, payload):
            entered.set()
            release.wait(2)
            return {'ok': True, 'works': [{'id': 'old', 'title': '旧课程作业', 'url': 'https://example.org/old'}]}
        self.service.request = request
        self.window._load_work_list()
        self.assertTrue(entered.wait(1))
        self.window.work_course.set_active(1)
        release.set()
        self.pump()
        self.assertEqual(self.window.work_items, [])
        self.assertEqual(self.window.work_url.get_text(), '')
        self.assertIn('切换课程', self.window.work_list_note.get_text())

    def test_stale_question_and_ai_results_are_ignored_after_work_switch(self):
        self.prepare_work_list()
        callback = self.window._work_result_callback()
        self.window.work_select.set_active(1)
        callback({'questions': [{'id': 'old', 'text': '旧作业答案'}], 'answers': [{'id': 'old', 'answer': 'A'}]})
        self.assertEqual(self.window.work_data, {})
        self.assertEqual(len(self.window.answer_store), 0)
        self.assertNotIn('旧作业答案', self.window.work_output.get_buffer().get_text(
            self.window.work_output.get_buffer().get_start_iter(), self.window.work_output.get_buffer().get_end_iter(), True))

    def test_work_empty_list_and_failure_are_visible_without_guessing_status(self):
        self.window._courses_loaded({'courses': [{'id': 'c1', 'name': '课程'}]})
        self.window._works_loaded({'ok': True, 'works': []})
        self.assertIn('暂未读取到作业', self.window.work_list_note.get_text())
        self.window._works_loaded({'ok': False, 'message': '登录状态失效，请同步'})
        self.assertIn('登录状态失效', self.window.work_list_note.get_text())
        self.assertEqual(self.window._work_status({'status': 7}), '7')
        self.assertEqual(self.window._work_status({}), '状态未提供')

    def test_manual_link_change_clears_previous_questions_and_selection(self):
        self.prepare_work_list()
        self.window._work_loaded({'questions': [{'id': 'old', 'text': '旧题'}]})
        self.window.work_url.set_text('https://example.org/manual')
        self.assertEqual(self.window.work_select.get_active(), -1)
        self.assertEqual(self.window.work_data, {})
        self.window._submit_work('submit')
        self.assertNotEqual(self.service.calls[-1][0], 'work_submit')

    def test_session_sync_is_explicit_and_refreshes_all_course_selectors(self):
        self.assertEqual(self.service.calls, [])
        self.assertFalse(self.window.account_expander.get_expanded())
        self.service.synced_courses = [{'id': 'synced', 'name': '会话课程'}]
        self.window._sync_session()
        self.pump()
        self.assertEqual(self.service.calls, [('session_sync', {})])
        self.assertEqual(self.window.courses[0]['id'], 'synced')
        self.assertEqual(self.window.work_course.get_active_text(), '会话课程')
        self.assertEqual(self.window.sign_course.get_active_text(), '会话课程')
        self.assertIn('同步成功', self.window.session_notice.get_text())
        self.assertTrue(self.window.session_sync_button.get_sensitive())

    def test_session_sync_failure_does_not_force_password_login(self):
        self.service.request = lambda *args: {'ok': False, 'message': '未找到有效网页会话'}
        self.window._sync_session()
        self.pump()
        self.assertIn('未找到有效网页会话', self.window.session_notice.get_text())
        self.assertFalse(self.window.account_expander.get_expanded())
        self.assertTrue(self.window.session_sync_button.get_sensitive())

    def test_official_login_opens_only_the_official_system_browser_url(self):
        with patch('chaoxing_linux.workspace.webbrowser.open', return_value=True) as opened:
            self.window._open_official_login()
            self.pump()
            opened.assert_called_once_with('https://i.chaoxing.com')
        self.assertEqual(self.service.calls, [])

    def test_combo_and_popup_colors_are_legible_for_normal_hover_selected_disabled(self):
        from gi.repository import Gtk
        menu = Gtk.Menu()
        item = Gtk.MenuItem(label='白底深字选项')
        menu.append(item)
        menu.show_all()
        self.addCleanup(menu.destroy)
        contexts = [self.window.sign_mode.get_style_context(), item.get_style_context(), item.get_child().get_style_context()]
        for context in contexts:
            for state in (Gtk.StateFlags.NORMAL, Gtk.StateFlags.PRELIGHT, Gtk.StateFlags.SELECTED, Gtk.StateFlags.INSENSITIVE):
                foreground = context.get_color(state)
                self.assertLess(max(foreground.red, foreground.green, foreground.blue), .5)
        for state in (Gtk.StateFlags.NORMAL, Gtk.StateFlags.PRELIGHT, Gtk.StateFlags.SELECTED, Gtk.StateFlags.INSENSITIVE):
            background = item.get_style_context().get_background_color(state)
            self.assertGreater(min(background.red, background.green, background.blue), .9)

    def test_completed_non_actionable_work_blocks_all_writes(self):
        self.window._courses_loaded({'courses': [{'id': 'c1', 'name': '课程'}]})
        self.window._works_loaded({'ok': True, 'works': [{'id': 'w1', 'title': '已完成作业',
            'url': 'https://example.org/work/1', 'status_text': '已完成', 'actionable': False}]})
        self.window._work_loaded({'questions': [{'id': 'q1', 'text': '题目'}]})
        self.assertIn('已完成', self.window.work_select.get_active_text())
        self.assertIn('不可提交', self.window.work_list_note.get_text())
        self.assertFalse(self.window.work_complete_button.get_sensitive())
        self.assertFalse(self.window.work_save_button.get_sensitive())
        self.assertFalse(self.window.work_submit_button.get_sensitive())
        self.assertTrue(self.window.work_page_button.get_sensitive())
        self.window._work_action('work_complete')
        self.window._submit_work('save')
        self.window._submit_work('submit')
        self.assertEqual(self.service.calls, [])

    def test_requires_start_is_disclosed_and_selection_does_not_fetch_questions(self):
        self.window._courses_loaded({'courses': [{'id': 'c1', 'name': '课程'}]})
        self.window._works_loaded({'ok': True, 'works': [{'id': 'w1', 'title': '未交作业',
            'url': 'https://example.org/work/1', 'status': '未交', 'actionable': True, 'requires_start': True}]})
        self.assertIn('建立作答记录', self.window.work_list_note.get_text())
        self.assertEqual(self.service.calls, [])
        self.window._work_action('work_load')
        self.pump()
        self.assertEqual(self.service.calls[-1][0], 'work_load')

    def test_search_modes_load_and_save_as_strings_separate_from_text_entries(self):
        self.assertNotIn('search_mode', self.window.setting_entries)
        self.assertEqual(self.window.search_mode.get_active_id(), 'tools')
        for mode in ('tools', 'native', 'prefetch'):
            self.window._settings_loaded({'search_enabled': True, 'search_mode': mode})
            self.assertEqual(self.window.search_mode.get_active_id(), mode)
            self.window._save_settings()
            self.pump()
            saved = self.service.saved_settings[-1]
            self.assertEqual(saved['search_mode'], mode)
            self.assertIs(saved['search_enabled'], True)
        self.assertEqual(self.window.search_enabled.get_label(), '允许模型联网')
        self.assertEqual(self.window.work_search.get_label(), '允许模型联网')
        self.window.search_mode.set_active_id('native')
        self.assertIn('Responses / web_search', self.window.search_mode_notice.get_text())

    def test_ai_connection_failure_without_status_is_visible_and_not_completed(self):
        self.window._save_then_start('ai_test')
        self.pump()
        self.service.records = [{'id': 'local-job', 'action': 'ai_test', 'status': 'completed',
            'message': '任务已完成', 'result': {'ok': False, 'error': '模拟模型拒绝访问'}}]
        self.window._poll()
        self.pump()
        self.assertIn('AI 连接失败', self.window.ai_test_notice.get_text())
        self.assertIn('模拟模型拒绝访问', self.window.ai_test_notice.get_text())
        self.assertEqual(self.window.job_store[0][2], '失败')
        self.assertIn('AI 连接失败', self.window.status.get_text())
        self.assertTrue(self.window.ai_test_button.get_sensitive())

    def test_ai_connection_result_success_and_start_failure_are_visible(self):
        self.window._ai_test_result({'ok': True})
        self.assertIn('AI 连接成功', self.window.ai_test_notice.get_text())
        self.assertIn('不代表已执行联网搜索', self.window.ai_test_notice.get_text())
        self.service.start = lambda *args: (_ for _ in ()).throw(ValueError('模拟任务启动失败'))
        self.window._save_then_start('ai_test')
        self.pump()
        self.assertIn('AI 连接失败', self.window.ai_test_notice.get_text())
        self.assertIn('模拟任务启动失败', self.window.ai_test_notice.get_text())
        self.assertTrue(self.window.ai_test_button.get_sensitive())
        self.service.start = lambda *args: {'ok': False, 'error': '模拟未创建任务'}
        self.window._save_then_start('ai_test')
        self.pump()
        self.assertIn('模拟未创建任务', self.window.ai_test_notice.get_text())
        self.assertTrue(self.window.ai_test_button.get_sensitive())

    def test_real_service_bool_settings_to_fake_ai_test_end_to_end(self):
        from chaoxing_linux.service import AppService
        captured = []
        class LocalAI:
            def __init__(self, settings):
                captured.append(settings)
            def test_connection(self):
                return {'ok': True, 'model': 'fake-local-model'}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        service = AppService(root / 'config', root / 'data', ai_factory=LocalAI)
        self.window.destroy()
        self.window = Window(service)
        self.addCleanup(self.window.destroy)
        self.pump()
        self.window.search_enabled.set_active(True)
        self.window.search_mode.set_active_id('native')
        self.window.setting_entries['model'].set_text('fake-local-model')
        self.window._save_then_start('ai_test')
        end = time.monotonic() + 3
        while time.monotonic() < end and 'AI 连接成功' not in self.window.ai_test_notice.get_text():
            self.window._poll()
            self.pump()
        self.assertIn('AI 连接成功', self.window.ai_test_notice.get_text())
        self.assertTrue(captured)
        self.assertIs(captured[0]['search_enabled'], True)
        self.assertEqual(captured[0]['search_mode'], 'native')
        persisted = json.loads((service.config_dir / 'settings.json').read_text())
        self.assertIs(persisted['search_enabled'], True)
        self.assertEqual(persisted['search_mode'], 'native')
        self.assertEqual(service.jobs()[0]['status'], 'success')

    def test_question_sources_are_clickable_and_show_actual_search_states(self):
        from gi.repository import Gtk
        self.window._work_loaded({'questions': [{'id': 'q1', 'text': '题目一'}, {'id': 'q2', 'text': '题目二'}],
            'answers': [
                {'id': 'q1', 'answer': 'A', 'status': 'ok', 'search_status': {'state': 'ok'},
                 'sources': [{'title': '参考文献', 'url': 'https://example.org/source'}, {'url': 'javascript:alert(1)'}]},
                {'id': 'q2', 'answer': 'B', 'status': 'ok', 'search_status': {'state': 'not_used'}, 'sources': []},
            ]})
        groups = self.window.work_sources.get_children()
        self.assertEqual(len(groups), 2)
        links = [child for group in groups for child in group.get_children() if isinstance(child, Gtk.LinkButton)]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].get_uri(), 'https://example.org/source')
        self.assertIn('题目 q1', groups[0].get_children()[0].get_text())
        self.assertIn('[ok]', groups[0].get_children()[0].get_text())
        self.assertIn('模型未调用搜索 [not_used]', groups[1].get_children()[0].get_text())
        self.window._reset_work_data()
        self.assertFalse(any(isinstance(child, Gtk.LinkButton) for group in self.window.work_sources.get_children()
                             if isinstance(group, Gtk.Box) for child in group.get_children()))

    def test_failed_and_disabled_search_reasons_are_visible(self):
        self.window._work_loaded({'questions': [{'id': '1', 'text': '题目'}], 'answers': [
            {'id': '1', 'answer': 'A', 'search_status': {'state': 'failed', 'error': '模拟搜索服务不可用'}}]})
        text = self.window.work_sources.get_children()[0].get_children()[0].get_text()
        self.assertIn('联网失败 [failed]', text)
        self.assertIn('模拟搜索服务不可用', text)
        self.window._work_loaded({'questions': [{'id': '1', 'text': '题目'}], 'answers': [
            {'id': '1', 'answer': 'A', 'search_status': {'state': 'disabled'}}]})
        self.assertIn('未允许联网 [disabled]', self.window.work_sources.get_children()[0].get_children()[0].get_text())

    def test_deepseek_flash_preset_preserves_keys_and_does_not_save_or_call_api(self):
        self.window.setting_entries['api_key'].set_text('fake-existing-ai-key')
        self.window.setting_entries['search_key'].set_text('fake-existing-search-key')
        self.window.search_mode.set_active_id('native')
        self.window._use_deepseek_flash()
        self.assertEqual(self.window.setting_entries['api_base'].get_text(), 'https://api.deepseek.com')
        self.assertEqual(self.window.setting_entries['model'].get_text(), 'deepseek-flash')
        self.assertEqual(self.window.setting_entries['search_provider'].get_text(), 'deepseek')
        self.assertEqual(self.window.search_mode.get_active_id(), 'tools')
        self.assertEqual(self.window.setting_entries['api_key'].get_text(), 'fake-existing-ai-key')
        self.assertEqual(self.window.setting_entries['search_key'].get_text(), 'fake-existing-search-key')
        self.assertIn('仅支持 Pro', self.window.search_mode_notice.get_text())
        self.assertIn('deepseek', self.window.setting_entries['search_provider'].get_placeholder_text())
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.service.saved_settings, [])

    def test_deepseek_flash_manual_native_choice_shows_provider_limit(self):
        self.window.setting_entries['model'].set_text('deepseek-flash')
        self.window.search_mode.set_active_id('native')
        self.assertIn('请勿为 Flash 选择原生联网', self.window.search_mode_notice.get_text())
        self.assertEqual(self.service.calls, [])

    def test_openai_preset_selects_official_api_and_never_reuses_another_services_key(self):
        entries = self.window.setting_entries
        entries['api_base'].set_text('https://api.deepseek.com')
        entries['api_key'].set_text('fake-deepseek-key')
        self.window._use_openai_gpt()
        self.assertEqual(entries['api_base'].get_text(), 'https://api.openai.com/v1')
        self.assertEqual(entries['model'].get_text(), 'gpt-5-mini')
        self.assertEqual(self.window.search_mode.get_active_id(), 'native')
        self.assertEqual(entries['api_key'].get_text(), '')
        entries['api_key'].set_text('fake-openai-key')
        self.window._use_openai_gpt()
        self.assertEqual(entries['api_key'].get_text(), 'fake-openai-key')
        self.window._use_deepseek_flash()
        self.assertEqual(entries['api_key'].get_text(), '')
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.service.saved_settings, [])

    def test_chatgpt_sign_in_enables_account_mode_without_replacing_api_profile(self):
        self.service.setting_values.update(api_base='https://api.deepseek.com', model='deepseek-flash', api_key='existing-key')
        self.window._settings_loaded(self.service.settings())
        self.window.stack.set_visible_child_name('settings')
        self.pump()
        self.assertEqual(self.service.calls, [('chatgpt_status', {})])
        self.window.chatgpt_sign_in.clicked()
        self.pump()
        self.assertIn(('chatgpt_sign_in', {'profile_id': ''}), self.service.calls)
        self.assertTrue(self.window.chatgpt_cancel.get_sensitive())
        self.service.chatgpt_models = [{'slug': 'available-model', 'display_name': '可用模型'}]
        self.service.records = [{'id': 'local-job', 'action': 'chatgpt_sign_in', 'status': 'success', 'result': {
            'ok': True, 'connected': True, 'active_id': 'profile-1',
            'profiles': [{'id': 'profile-1', 'label': '测试账户', 'connected': True}],
        }}]
        self.window._poll()
        self.pump()
        self.assertEqual(self.window.ai_provider.get_active_id(), 'chatgpt')
        self.assertEqual(self.service.saved_settings[-1], {'ai_provider': 'chatgpt', 'chatgpt_model': ''})
        self.assertEqual(self.service.setting_values['api_key'], 'existing-key')
        self.assertIn(('chatgpt_models', {'profile_id': 'profile-1'}), self.service.calls)
        self.assertEqual(self.window.chatgpt_accounts.get_active_id(), 'profile-1')
        self.assertTrue(self.window.chatgpt_models.set_active_id('available-model'))
        self.assertEqual(self.window._settings_payload()['chatgpt_model'], 'available-model')

    def test_chatgpt_authorization_cancel_leaves_saved_provider_unchanged(self):
        self.window.chatgpt_sign_in.clicked()
        self.pump()
        self.window.chatgpt_cancel.clicked()
        self.pump()
        self.assertIn(('cancel', 'local-job'), self.service.calls)
        self.service.records = [{'id': 'local-job', 'action': 'chatgpt_sign_in', 'status': 'cancelled'}]
        self.window._poll()
        self.pump()
        self.assertIn('已取消', self.window.chatgpt_notice.get_text())
        self.assertTrue(self.window.chatgpt_sign_in.get_sensitive())
        self.assertEqual(self.window.ai_provider.get_active_id(), 'api_key')
        self.assertEqual(self.service.saved_settings, [])

    def make_public_test_image(self):
        from chaoxing_linux.workspace import GdkPixbuf
        root = Path(self.image_tmp.name) / 'assignment-images'
        root.mkdir(mode=0o700, exist_ok=True)
        temporary = root / 'generated.png'
        image = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 24, 16)
        image.fill(0x2868CFFF)
        image.savev(str(temporary), 'png', [], [])
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        path = root / (digest + '.png')
        temporary.replace(path)
        path.chmod(0o600)
        return {'id': 'img-1', 'label': '公开生成的测试色块', 'scope': 'question',
                'source_url': 'https://example.org/generated.png', 'mime': 'image/png',
                'local_path': str(path), 'sha256': digest, 'status': 'ready'}

    def load_public_image_question(self, images=None, state='ready'):
        images = [self.make_public_test_image()] if images is None else images
        self.window.work_url.set_text('https://example.org/work/images')
        self.window._work_loaded({'questions': [{'id': 'img-q', 'text': '', 'images': images,
            'image_status': {'state': state, 'count': len(images), 'ready': sum(image.get('status') == 'ready' for image in images)}}]})

    def test_image_only_question_counts_scope_and_lazy_thumbnail_preview(self):
        from gi.repository import Gtk
        first = self.make_public_test_image()
        second = dict(first, id='option-image', scope='option', option_key='B', label='选项示意图')
        self.load_public_image_question([first, second])
        self.assertIn('图片题', self.window.answer_store[0][1])
        self.assertIn('成功载入 2 张', self.window.work_image_notice.get_text())
        expander = self.window.work_images.get_children()[0]
        self.assertIn('题目 img-q', expander.get_label())
        self.assertFalse(expander.get_expanded())
        body = expander.get_child()
        captions = [child.get_text() for child in body.get_children() if isinstance(child, Gtk.Label)]
        self.assertTrue(any('题干' in value for value in captions))
        self.assertTrue(any('选项 B' in value for value in captions))
        previews = [child for child in body.get_children() if isinstance(child, Gtk.Image)]
        self.assertTrue(all(child.get_pixbuf() is None for child in previews))
        expander.set_expanded(True)
        self.pump()
        self.assertTrue(all(child.get_pixbuf() is not None for child in previews))
        self.assertFalse(self.window._work_has_image_failures())
        self.assertEqual(self.service.calls, [])

    def test_late_settings_refreshes_image_cache_validation(self):
        self.window.work_image_cache_root = None
        self.load_public_image_question()
        self.assertTrue(self.window._work_has_image_failures())
        self.window._settings_loaded(self.service.settings())
        self.assertFalse(self.window._work_has_image_failures())
        self.assertIn('成功载入 1 张', self.window.work_image_notice.get_text())

    def test_failed_or_partial_images_visibly_block_all_writes(self):
        failed = {'id': 'bad', 'scope': 'question', 'status': 'error', 'error': '模拟图片下载失败'}
        self.load_public_image_question([failed], 'failed')
        self.assertIn('失败 1 张', self.window.work_image_notice.get_text())
        self.assertIn('已阻止', self.window.work_image_notice.get_text())
        self.assertFalse(self.window.work_complete_button.get_sensitive())
        self.assertFalse(self.window.work_save_button.get_sensitive())
        self.assertFalse(self.window.work_submit_button.get_sensitive())
        self.window._work_action('work_complete')
        self.window._submit_work('save')
        self.window._submit_work('submit')
        self.assertEqual(self.service.calls, [])
        self.assertIn('题图载入或发送不完整', self.window.work_note.get_text())
        self.load_public_image_question([self.make_public_test_image(), failed], 'partial')
        self.assertTrue(self.window._work_has_image_failures())

    def test_image_preview_rejects_external_paths_symlinks_and_changed_hash(self):
        image = self.make_public_test_image()
        external = Path(self.image_tmp.name) / 'outside.png'
        external.write_bytes(Path(image['local_path']).read_bytes())
        external.chmod(0o600)
        invalid = dict(image, local_path=str(external))
        with patch.object(Path, 'open', side_effect=AssertionError('外部文件不应打开')):
            with self.assertRaises(ValueError):
                self.window._cached_question_image(invalid)
        Path(image['local_path']).unlink()
        Path(image['local_path']).symlink_to(external)
        with self.assertRaises(ValueError):
            self.window._cached_question_image(image)
        Path(image['local_path']).unlink()
        Path(image['local_path']).write_bytes(external.read_bytes() + b'changed')
        Path(image['local_path']).chmod(0o600)
        self.load_public_image_question([image])
        self.assertTrue(self.window._work_has_image_failures())
        self.assertIn('失败 1 张', self.window.work_image_notice.get_text())

    def test_image_previews_and_stale_images_clear_on_work_switch(self):
        self.prepare_work_list()
        self.window._work_loaded({'questions': [{'id': 'image', 'text': '',
            'images': [self.make_public_test_image()], 'image_status': {'state': 'ready', 'count': 1, 'ready': 1}}]})
        callback = self.window._work_result_callback()
        old = dict(self.window.work_data)
        self.assertEqual(len(self.window.work_images.get_children()), 1)
        self.window.work_select.set_active(1)
        callback(old)
        self.assertEqual(self.window.work_data, {})
        self.assertEqual(self.window.work_images.get_children(), [])
        self.assertEqual(self.window.work_image_errors, [])

    def test_model_image_send_status_is_factual_and_failed_send_blocks_submission(self):
        self.load_public_image_question()
        self.window._work_loaded({'answers': [{'id': 'img-q', 'answer': 'A', 'status': 'ok',
            'search_status': {'state': 'not_used'}, 'image_status': {'state': 'sent', 'count': 1, 'sent': 1}}]})
        text = self.window.work_sources.get_children()[0].get_children()[0].get_text()
        self.assertIn('已随模型请求发送 1 图', text)
        self.assertIn('不代表图片已被正确识别', text)
        self.assertFalse(self.window._work_has_image_failures())
        self.window._work_loaded({'answers': [{'id': 'img-q', 'answer': 'A', 'status': 'ok',
            'image_status': {'state': 'failed', 'count': 1, 'sent': 0, 'error': '模拟模型不支持图片'}}]})
        text = self.window.work_sources.get_children()[0].get_children()[0].get_text()
        self.assertIn('题图发送失败', text)
        self.assertIn('模拟模型不支持图片', text)
        self.assertFalse(self.window.work_submit_button.get_sensitive())
        self.window._submit_work('submit')
        self.assertEqual(self.service.calls, [])


if __name__ == '__main__':
    unittest.main()
