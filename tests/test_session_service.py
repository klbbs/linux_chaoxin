from concurrent.futures import CancelledError
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from chaoxing_linux.service import AppService


class SessionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.calls=[]
        owner=self
        class Backend:
            def __init__(self,settings):self.settings=settings
            def execute(self,action,payload,**kwargs):
                path=Path(self.settings['data_dir'])/'cookies.txt'
                data=path.read_text() if path.exists() else ''
                owner.calls.append((action,dict(self.settings)))
                if data=='_uid=valid; token=private':
                    return {'ok':True,'courses':[{'id':'example','name':'Example'}]}
                return {'ok':False,'status':'needs_input','message':'expired'}
        self.service=AppService(root/'config',root/'data',backend_factory=Backend)
        self.addCleanup(self.service.shutdown)
    def candidates(self,*values):
        from chaoxing_linux.session_sync import SessionCandidate
        return [SessionCandidate(source='Firefox',cookies=v) for v in values]
    def test_verified_isolated_copy_is_saved_privately_without_password(self):
        self.service.save_settings({'username':'old','password':'must-not-use'})
        with patch('chaoxing_linux.session_sync.discover_sessions',return_value=self.candidates('_uid=bad','_uid=valid; token=private')):
            result=self.service.request('session_sync')
        self.assertTrue(result['ok']);self.assertEqual(result['source'],'Firefox')
        self.assertNotIn('private',str(result))
        path=self.service.data_dir/'cookies.txt'
        self.assertEqual(path.read_text(),'_uid=valid; token=private')
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        for action,settings in self.calls:
            self.assertEqual(action,'courses')
            self.assertEqual(settings['password'],'');self.assertEqual(settings['username'],'')
            self.assertNotEqual(settings['data_dir'],str(self.service.data_dir))
            self.assertFalse(Path(settings['data_dir']).exists())
    def test_failed_candidates_preserve_previous_session(self):
        path=self.service.data_dir/'cookies.txt';path.write_text('_uid=old')
        with patch('chaoxing_linux.session_sync.discover_sessions',return_value=self.candidates('_uid=bad')):
            result=self.service.request('session_sync')
        self.assertFalse(result['ok']);self.assertEqual(path.read_text(),'_uid=old')
    def test_refresh_courses_automatically_discovers_session_after_expiry(self):
        with patch('chaoxing_linux.session_sync.discover_sessions',return_value=self.candidates('_uid=valid; token=private')):
            result=self.service.request('courses')
        self.assertTrue(result['ok']);self.assertEqual(len(result['courses']),1)
    def test_cancel_before_validation_never_replaces_session(self):
        cancel=threading.Event();cancel.set()
        with patch('chaoxing_linux.session_sync.discover_sessions',return_value=self.candidates('_uid=valid; token=private')):
            with self.assertRaises(CancelledError):self.service._sync_session(lambda _:None,cancel)
        self.assertFalse((self.service.data_dir/'cookies.txt').exists())
    def test_no_switch_while_account_write_is_active(self):
        self.service._jobs['other']={'_write_key':'work:1','status':'running'}
        with patch('chaoxing_linux.session_sync.discover_sessions',return_value=self.candidates('_uid=valid; token=private')):
            result=self.service.request('session_sync')
        self.assertFalse(result['ok']);self.assertEqual(self.calls,[])
    def test_cancellation_after_server_check_keeps_old_session(self):
        path=self.service.data_dir/'cookies.txt';path.write_text('_uid=old')
        cancel=threading.Event()
        class Backend:
            def __init__(self,settings):pass
            def execute(self,*args,**kwargs):
                cancel.set()
                return {'ok':True,'courses':[]}
        self.service._backend_factory=Backend
        with patch('chaoxing_linux.session_sync.discover_sessions',return_value=self.candidates('_uid=valid; token=private')):
            with self.assertRaises(CancelledError):self.service._sync_session(lambda _:None,cancel)
        self.assertEqual(path.read_text(),'_uid=old')
    def test_new_writes_cannot_queue_while_identity_is_changing(self):
        owner=self
        class Backend:
            def __init__(self,settings):pass
            def execute(self,*args,**kwargs):
                with owner.assertRaises(ValueError):
                    owner.service.start('work_submit',{'url':'https://mooc1.chaoxing.com/work?workId=1'})
                return {'ok':True,'courses':[]}
        self.service._backend_factory=Backend
        with patch('chaoxing_linux.session_sync.discover_sessions',return_value=self.candidates('_uid=valid')):
            self.assertTrue(self.service.request('session_sync')['ok'])
        self.assertEqual(self.service.jobs(),[])
        self.assertFalse(self.service._session_syncing)
