from pathlib import Path
import tempfile
import time
import unittest

from chaoxing_linux.service import AppService
from chaoxing_linux.location import wgs84_to_gcj02

class Backend:
    calls=[]
    def __init__(self,settings):pass
    def execute(self,action,payload,**kwargs):
        type(self).calls.append((action,dict(payload)))
        if action=='sign':return {'ok':True,'status':'success','message':'success'}
        raise AssertionError('Unexpected online action')

class AttendanceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);Backend.calls=[]
        self.service=AppService(root/'config',root/'data',backend_factory=Backend)
        self.addCleanup(self.service.shutdown)
    def point(self,**overrides):
        return {'name':'学校东门','address':'校园东门','latitude':40.0,'longitude':116.3,'coordinate_system':'wgs84',**overrides}
    def test_local_save_reuse_edit_delete_never_signs(self):
        p=self.service.request('locations_save',self.point())['location']
        self.assertEqual(self.service.request('locations_list')['locations'][0]['id'],p['id'])
        p['name']='学校南门'
        self.service.request('locations_save',p)
        path=self.service.data_dir/'locations.json'
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.service.request('locations_delete',{'id':p['id']})
        self.assertEqual(self.service.request('locations_list')['locations'],[])
        self.assertEqual(Backend.calls,[])
    def test_duplicate_and_invalid_coordinates_rejected(self):
        self.service.request('locations_save',self.point())
        with self.assertRaises(ValueError):self.service.request('locations_save',self.point())
        for value in ('nan','inf',91):
            with self.assertRaises(ValueError):self.service.request('locations_save',self.point(name='other',latitude=value))
    def test_corrupt_store_is_not_silently_overwritten(self):
        path=self.service.data_dir/'locations.json'
        for content in ('broken', '[null]', '[1]', '{}'):
            with self.subTest(content=content):
                path.write_text(content)
                with self.assertRaises(ValueError):self.service.request('locations_save',self.point())
                self.assertEqual(path.read_text(),content)
    def test_wgs84_sign_converts_once_and_gcj_is_preserved(self):
        payload=self.point(sign_type='location',activity_id='123',course={'name':'课程'})
        self.service.request('sign',payload)
        expected=wgs84_to_gcj02(40,116.3)
        sent=Backend.calls[-1][1]
        self.assertEqual((sent['latitude'],sent['longitude']),expected)
        self.service.request('sign',{**payload,'latitude':expected[0],'longitude':expected[1],'coordinate_system':'gcj02'})
        sent=Backend.calls[-1][1]
        self.assertEqual((sent['latitude'],sent['longitude']),expected)
    def test_history_survives_restart_with_activity_summary(self):
        job=self.service.start('sign',self.point(sign_type='location',activity_id='123',course={'name':'课程'}))
        for _ in range(100):
            if self.service.request('sign_history')['history']:break
            time.sleep(.01)
        other=AppService(self.service.config_dir,self.service.data_dir)
        history=other.request('sign_history')['history']
        self.assertEqual(history[0]['summary']['activity_id'],'123')
        self.assertEqual(history[0]['summary']['course'],'课程')
        self.assertEqual((self.service.data_dir/'sign-history.json').stat().st_mode & 0o777,0o600)
