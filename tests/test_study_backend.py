import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from chaoxing_linux.study_backend import BackendClient, backend_paths


class BackendBridgeTests(unittest.TestCase):
    def fake_backend(self, temporary, script):
        root = Path(temporary) / 'source'
        (root / 'api').mkdir(parents=True)
        (root / 'api/base.py').write_text('')
        python = Path(temporary) / 'python'
        python.write_text('#!' + sys.executable + '\n' + script)
        python.chmod(0o700)
        return {'backend_path': str(root), 'backend_python': str(python)}

    def test_missing_backend_has_actionable_result_and_no_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = BackendClient({'backend_path': temporary}).execute('courses')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'needs_input')

    def test_installed_layout_discovers_sibling_interpreter(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            (parent / 'source/api').mkdir(parents=True)
            (parent / 'source/api/base.py').touch()
            (parent / '.venv/bin').mkdir(parents=True)
            (parent / '.venv/bin/python').symlink_to(sys.executable)
            root, python = backend_paths({'backend_path': str(parent / 'source')})
            self.assertEqual(root, parent / 'source')
            self.assertEqual(python, parent / '.venv/bin/python')

    def test_secrets_use_stdin_and_events_are_delivered(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.fake_backend(temporary, '''import json, os, sys
r = json.loads(sys.stdin.readline())
assert r['settings']['password'] == 'private-password'
assert 'private-password' not in ' '.join(sys.argv)
assert 'private-password' not in str(dict(os.environ))
print(json.dumps({'type':'event','event':{'message':'step'}}), flush=True)
print(json.dumps({'type':'result','result':{'ok':True,'status':'success'}}), flush=True)
''')
            settings['password'] = 'private-password'
            events = []
            result = BackendClient(settings).execute('courses', on_event=events.append)
            self.assertTrue(result['ok'])
            self.assertEqual(events, [{'message': 'step'}])

    def test_cancel_terminates_the_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.fake_backend(temporary, 'import time\ntime.sleep(30)\n')
            cancel = threading.Event()
            timer = threading.Timer(0.15, cancel.set)
            timer.start()
            try:
                result = BackendClient(settings).execute('course_run', cancel=cancel)
            finally:
                timer.cancel()
            self.assertEqual(result['status'], 'cancelled')

    def test_malformed_or_missing_result_is_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.fake_backend(temporary, "print('not json')\n")
            self.assertFalse(BackendClient(settings).execute('courses')['ok'])

    def test_unknown_action_and_pre_cancel_do_not_spawn(self):
        self.assertFalse(BackendClient({}).execute('unexpected')['ok'])
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.fake_backend(temporary, 'raise RuntimeError()\n')
            cancel = threading.Event()
            cancel.set()
            self.assertEqual(BackendClient(settings).execute('courses', cancel=cancel)['status'], 'cancelled')

    def test_real_upstream_font_resources_survive_private_working_directory(self):
        source = Path(__file__).resolve().parents[2] / 'chaoxing'
        python = source / '.venv/bin/python'
        if not python.is_file() or not (source / 'resource/font_map_table.json').is_file():
            self.skipTest('optional upstream checkout and Python 3.13 environment are not installed')
        with tempfile.TemporaryDirectory() as temporary:
            result = BackendClient({'backend_path': str(source), 'backend_python': str(python),
                                    'data_dir': temporary}).execute('status')
            self.assertTrue(result['ok'], result)
            self.assertGreater(result['font_table_entries'], 1000)
            self.assertFalse((Path(temporary) / 'cookies.txt').exists())
            self.assertFalse((Path(temporary) / 'resource').exists())
