from pathlib import Path
from types import SimpleNamespace
import struct
import subprocess
import tempfile
import unittest

from chaoxing_linux.phone_control import PhoneController
from chaoxing_linux.runtime import RuntimeFailure

class FakeRuntime:
    adb='adb'
    def devices(self):return [{'serial':'usb123','state':'device','model':'Android'}]

class PhoneControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.calls=[];self.foreground='com.chaoxing.mobile'
        def runner(args,**kwargs):
            self.calls.append(args)
            if 'screencap' in args:
                data=b'\x89PNG\r\n\x1a\n'+b'\0'*8+struct.pack('>II',1080,2400)+b'mock'
            else:data=f'mCurrentFocus=Window{{123 u0 {self.foreground}/.MainActivity}}\n'
            return SimpleNamespace(returncode=0,stdout=data,stderr='')
        self.control=PhoneController(FakeRuntime(),runner=runner,directory=Path(self.tmp.name))
    def test_preview_private_file_and_coordinate_mapping(self):
        result=self.control.execute('screen',{'serial':'usb123'})
        self.assertEqual((result['width'],result['height']),(1080,2400))
        self.assertEqual(Path(result['path']).stat().st_mode & 0o777,0o600)
        self.control.execute('tap',{'serial':'usb123','x':100,'y':300})
        self.assertEqual(self.calls[-1][-4:],['input','tap','100','300'])
        with self.assertRaises(ValueError):self.control.execute('tap',{'serial':'usb123','x':1080,'y':0})
    def test_other_app_capture_and_tap_refused(self):
        self.foreground='com.example.other'
        with self.assertRaises(RuntimeFailure):self.control.execute('screen',{'serial':'usb123'})
        self.assertFalse(any('screencap' in x for x in self.calls))
    def test_text_is_shell_quoted_and_non_ascii_is_explicit(self):
        self.control.execute('text',{'serial':'usb123','text':"x'; reboot; 'y"})
        self.assertTrue(self.calls[-1][-1].startswith("'"))
        with self.assertRaises(ValueError):self.control.execute('text',{'serial':'usb123','text':'中文'})
    def test_never_accept_unknown_key(self):
        with self.assertRaises(ValueError):self.control.execute('key',{'serial':'usb123','key':'POWER;reboot'})
