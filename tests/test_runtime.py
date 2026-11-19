import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from chaoxing_linux.runtime import Runtime, RuntimeFailure, parse_status, status_uid, run, app_installed


class RuntimeTests(unittest.TestCase):
    def test_status_ansi_and_uid(self):
        state = parse_status("\x1b[32mSession:\x1b[0m RUNNING\nContainer: RUNNING\nSession user: demo (1000)\nWayland display: custom\n")
        self.assertEqual(state['session'], 'RUNNING')
        self.assertEqual(state['wayland_display'], 'custom')
        self.assertEqual(status_uid(state), 1000)
        self.assertIsNone(status_uid({'session_user':'unknown'}))

    def test_command_timeout_is_bounded_failure(self):
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('waydroid',1)):
            with self.assertRaises(RuntimeFailure):
                run(['waydroid','status'],timeout=1)

    def test_launch_does_not_claim_an_uninstalled_app(self):
        calls=[]
        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args,0,'Name: Other\nPackage: com.example.other\n','')
        runtime=Runtime(runner=runner)
        with patch.object(runtime,'ensure_ready'):
            with self.assertRaises(RuntimeFailure):runtime.launch()
        self.assertFalse(any('launch' in x for x in calls))

    def test_launch_checks_exact_package(self):
        calls=[]
        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args,0,'Name: 学习通\npackageName: com.chaoxing.mobile\ncategories:\n  android.intent.category.LAUNCHER\n','')
        runtime=Runtime(runner=runner)
        with patch.object(runtime,'ensure_ready'):runtime.launch()
        self.assertEqual(calls[-1][-2:],['launch','com.chaoxing.mobile'])

    def test_app_list_uses_the_actual_waydroid_field(self):
        self.assertTrue(app_installed('Name: 学习通\npackageName: com.chaoxing.mobile\ncategories:\n'))
        self.assertFalse(app_installed('packageName: com.chaoxing.mobile.fake\n'))

    def test_arm_only_apk_does_not_install_in_x86_runtime(self):
        calls=[]
        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args,0,'x86_64,x86\n','')
        runtime=Runtime(runner=runner)
        with patch.object(runtime,'ensure_ready'):
            with self.assertRaises(RuntimeFailure):runtime.install('/tmp/test.apk',{'abis':['arm64-v8a']})
        self.assertFalse(any('install' in x for x in calls))

    def test_unknown_abi_does_not_install(self):
        runtime=Runtime(runner=lambda args,**kw:subprocess.CompletedProcess(args,0,'',''))
        with patch.object(runtime,'ensure_ready'):
            with self.assertRaises(RuntimeFailure):runtime.install('/tmp/test.apk',{'abis':[]})

    def test_zero_exit_without_android_service_is_not_ready(self):
        messages=[]
        runtime=Runtime(messages.append,runner=lambda args,**kw:subprocess.CompletedProcess(args,0,'','Failed to access IPlatform service'))
        with patch('chaoxing_linux.runtime.shutil.which',return_value='/usr/bin/waydroid'), \
             patch('chaoxing_linux.runtime.initialized',return_value=True), \
             patch.object(runtime,'prepare_display'), \
             patch.object(runtime,'status',return_value={'session':'RUNNING'}), \
             patch('chaoxing_linux.runtime.time.monotonic',side_effect=[0,0,2]), \
             patch('chaoxing_linux.runtime.time.sleep'):
            with self.assertRaises(RuntimeFailure):runtime.ensure_ready(timeout=1)
        self.assertFalse(any('已就绪' in message for message in messages))

    def test_running_session_requires_boot_completed_and_real_apps(self):
        def runner(args,**kw):
            output='1\n' if args[-1]=='sys.boot_completed' else 'Name: Android\npackageName: com.android.settings\n'
            return subprocess.CompletedProcess(args,0,output,'')
        runtime=Runtime(runner=runner)
        with patch('chaoxing_linux.runtime.shutil.which',return_value='/usr/bin/waydroid'), \
             patch('chaoxing_linux.runtime.initialized',return_value=True), \
             patch.object(runtime,'prepare_display'), \
             patch.object(runtime,'status',return_value={'session':'RUNNING'}):
            runtime.ensure_ready(timeout=1)

    def test_existing_apk_is_not_reported_as_successful_update(self):
        runtime=Runtime(runner=lambda args,**kw:subprocess.CompletedProcess(args,0,'packageName: com.chaoxing.mobile\n',''))
        with patch.object(runtime,'ensure_ready'):
            with self.assertRaisesRegex(RuntimeFailure,'已有学习通'):
                runtime.install('/tmp/new.apk',{'abis':['arm64-v8a']})

    def test_install_waits_for_asynchronous_package_registration(self):
        listings = iter(['', '', 'packageName: com.chaoxing.mobile\n'])
        messages = []
        def runner(args, **kw):
            output = next(listings) if args[-2:] == ['app', 'list'] else 'arm64-v8a\n'
            return subprocess.CompletedProcess(args, 0, output, '')
        runtime = Runtime(messages.append, runner=runner)
        with patch.object(runtime, 'ensure_ready'), patch('chaoxing_linux.runtime.time.sleep') as sleep:
            runtime.install('/tmp/test.apk', {'abis': ['arm64-v8a']})
        sleep.assert_called_once_with(1)
        self.assertIn('已出现在', messages[-1])

    def test_install_zero_exit_without_package_never_claims_success(self):
        messages = []
        def runner(args, **kw):
            output = '' if args[-2:] == ['app', 'list'] else 'arm64-v8a\n'
            return subprocess.CompletedProcess(args, 0, output, '')
        runtime = Runtime(messages.append, runner=runner)
        with patch.object(runtime, 'ensure_ready'), \
             patch('chaoxing_linux.runtime.time.monotonic', side_effect=[0, 0, 91]), \
             patch('chaoxing_linux.runtime.time.sleep'):
            with self.assertRaisesRegex(RuntimeFailure, '仍未'):
                runtime.install('/tmp/test.apk', {'abis': ['arm64-v8a']})
        self.assertFalse(any('已出现在' in message for message in messages))

    def test_current_dbus_pid_must_match_owned_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'chaoxing-linux';folder.mkdir()
            (folder/'owned-runtime.json').write_text(json.dumps({'owned_session':True,'session_pid':42,'session_start':'10','display':'custom'}))
            calls=[]
            def runner(args,**kw):
                calls.append(args)
                return subprocess.CompletedProcess(args,0,f'Session: RUNNING\nSession user: me({os.getuid()})\nWayland display: custom\n','')
            runtime=Runtime(runner=runner);runtime.env['XDG_RUNTIME_DIR']=tmp
            with patch('chaoxing_linux.runtime.proc_identity',return_value='10'), \
                 patch('chaoxing_linux.runtime.current_session',return_value={'pid':'999','user_id':str(os.getuid()),'wayland_display':'custom'}), \
                 patch('os.kill') as kill:
                with self.assertRaises(RuntimeFailure):runtime.stop_owned()
                kill.assert_not_called()
            self.assertFalse(any('stop' in command for command in calls))

    def test_does_not_stop_another_session_or_recycled_pid(self):
        for owned,start in [(False,'10'),(True,'old')]:
            with self.subTest(owned=owned),tempfile.TemporaryDirectory() as tmp:
                folder=Path(tmp)/'chaoxing-linux';folder.mkdir()
                (folder/'owned-runtime.json').write_text(json.dumps({'owned_session':owned,'session_pid':42,'session_start':start,'display':'custom'}))
                def runner(args,**kw):
                    return subprocess.CompletedProcess(args,0,f'Session: RUNNING\nSession user: me({os.getuid()})\nWayland display: custom\n','')
                runtime=Runtime(runner=runner);runtime.env['XDG_RUNTIME_DIR']=tmp
                with patch('chaoxing_linux.runtime.proc_identity',return_value='10'),patch('os.kill') as kill:
                    with self.assertRaises(RuntimeFailure):runtime.stop_owned()
                    kill.assert_not_called()


if __name__=='__main__':unittest.main()
