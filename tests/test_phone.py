import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from chaoxing_linux.phone import PhoneRuntime, parse_devices, select_device
from chaoxing_linux.runtime import RuntimeFailure


class PhoneTests(unittest.TestCase):
    def test_only_usb_phones_are_candidates(self):
        devices = parse_devices('''List of devices attached
192.168.240.112:5555 device product:waydroid transport_id:2
emulator-5554 device usb:fake model:emulator
phone-one device usb:3-9 product:test model:My_Phone transport_id:9
phone-two unauthorized usb:3-8 transport_id:10
''')
        self.assertEqual([d['serial'] for d in devices], ['phone-one', 'phone-two'])
        self.assertEqual(devices[0]['model'], 'My Phone')

    def test_requires_authorization_and_unambiguous_selection(self):
        ready = {'serial': 'one', 'state': 'device'}
        pending = {'serial': 'two', 'state': 'unauthorized'}
        for devices, serial in [([], None), ([pending], None), ([ready, pending], None), ([ready], 'wrong')]:
            with self.subTest(devices=devices, serial=serial), self.assertRaises(RuntimeFailure):
                select_device(devices, serial)
        self.assertEqual(select_device([ready, pending], 'one'), ready)

    def test_missing_app_does_not_open_an_unrelated_phone_screen(self):
        def runner(args, **kw):
            output = 'scrcpy 5.0.1\n' if args[-1] == '--version' else ''
            return subprocess.CompletedProcess(args, 0, output, '')
        phone = PhoneRuntime(runner=runner)
        with patch.object(phone, 'devices', return_value=[{'serial': 'one', 'state': 'device'}]), \
             patch('chaoxing_linux.phone.scrcpy_path', return_value='/tmp/scrcpy'), \
             patch('chaoxing_linux.phone.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(RuntimeFailure, '未找到学习通'):
                phone.launch()
            spawn.assert_not_called()

    def test_launch_pins_the_selected_phone_and_exact_app(self):
        calls = []
        def runner(args, **kw):
            calls.append(args)
            output = 'scrcpy 5.0.1\n' if args[-1] == '--version' else 'package:/data/app/base.apk\n'
            return subprocess.CompletedProcess(args, 0, output, '')
        phone = PhoneRuntime(runner=runner)
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary}), \
             patch.object(phone, 'devices', return_value=[{'serial': 'chosen', 'state': 'device'}]), \
             patch('chaoxing_linux.phone.scrcpy_path', return_value='/tmp/scrcpy'), \
             patch('chaoxing_linux.phone.proc_identity', return_value='start'), \
             patch('chaoxing_linux.phone.time.sleep'), \
             patch('chaoxing_linux.phone.subprocess.Popen') as spawn:
            spawn.return_value.pid = 123
            spawn.return_value.poll.return_value = None
            phone.launch('chosen')
            command = spawn.call_args.args[0]
            self.assertEqual(command[1:3], ['--serial', 'chosen'])
            self.assertIn('--start-app=com.chaoxing.mobile', command)
            self.assertTrue(any(call[1:3] == ['-s', 'chosen'] for call in calls))
            self.assertNotIn('--tcpip', command)

    def test_stop_preserves_recycled_process(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary}):
            folder = Path(temporary) / 'chaoxing-linux'
            folder.mkdir()
            (folder / 'phone-runtime.json').write_text(json.dumps({'pid': 42, 'start': 'old'}))
            with patch('chaoxing_linux.phone.proc_identity', return_value='new'), patch('os.kill') as kill:
                PhoneRuntime().stop()
                kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
