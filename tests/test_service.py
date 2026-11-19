import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from chaoxing_linux.service import AppService


class FakeBackend:
    def __init__(self,settings):self.settings=settings
    def execute(self,action,payload,on_event=None,cancel=None):
        if action=='work_load':return {'ok':True,'questions':[{'id':'q1','type':'single','text':'test','options':[{'key':'A','text':'one'}]}],'context':{'enc':'server-token'}}
        if action=='work_submit':return {'ok':True,'status':'success','message':payload['mode'],'mode':payload['mode']}
        if action=='courses':return {'ok':True,'courses':[]}
        if action=='course_run':
            while not cancel.wait(.01):pass
            return {'ok':False,'status':'cancelled','message':'stopped'}
        raise ValueError('unsupported')


class FakeAI:
    unknown=False
    def __init__(self,settings):pass
    def solve_questions(self,questions,search=True,progress=None,cancel=None):
        return [{'id':'q1','answer':None if self.unknown else 'A','status':'unknown' if self.unknown else 'ok','sources':[{'url':'https://example.org'}],'search_status':{'state':'ok'}}]


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.service=AppService(root/'config',root/'data',backend_factory=FakeBackend,ai_factory=FakeAI)
    def wait(self,job):
        for _ in range(200):
            current=next(x for x in self.service.jobs() if x['id']==job['id'])
            if current['status'] not in ('queued','running','cancelling'):return current
            time.sleep(.01)
        self.fail('job did not finish')
    def test_private_settings_and_redacted_persistent_error(self):
        self.service.save_settings({'api_key':'private-api-secret','password':'sensitive-password'})
        path=self.service.config_dir/'settings.json'
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.service._clean('private-api-secret sensitive-password'),'[已隐藏] [已隐藏]')
        self.assertNotIn('api_key',self.service._clean({'api_key':'private-api-secret','ok':True}))
    def test_settings_keep_boolean_and_integer_types_before_ai_test(self):
        for enabled in (True,False):
            self.service.save_settings({'search_enabled':enabled,'request_timeout':45,'max_retries':'1','search_mode':'tools','model':' example '})
            settings=self.service.settings()
            self.assertIs(settings['search_enabled'],enabled)
            self.assertIs(type(settings['request_timeout']),int)
            self.assertEqual(settings['max_retries'],1)
            self.assertEqual(settings['model'],'example')
        persisted=json.loads((self.service.config_dir/'settings.json').read_text())
        self.assertIs(persisted['search_enabled'],False)
        with self.assertRaises(ValueError):self.service.save_settings({'request_timeout':True})
        with self.assertRaises(ValueError):self.service.save_settings({'search_mode':'unsupported'})
    def test_ai_connection_failure_is_not_reported_as_completed_success(self):
        class Unavailable(FakeAI):
            def test_connection(self):return {'ok':False,'error':'HTTP 401'}
        self.service._ai_factory=Unavailable
        self.service.save_settings({'search_enabled':True})
        result=self.service.request('ai_test')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'],'needs_input')
        self.assertIn('HTTP 401',result['message'])
    def test_work_complete_save_and_explicit_submit(self):
        result=self.service.request('work_complete',{'url':'https://example.org','submit':False})
        self.assertEqual(result['submission']['mode'],'save')
        result=self.service.request('work_complete',{'url':'https://example.org','submit':True})
        self.assertEqual(result['submission']['mode'],'submit')
    def test_unknown_answer_blocks_automatic_submission(self):
        class UnknownAI(FakeAI):unknown=True
        self.service._ai_factory=UnknownAI
        result=self.service.request('work_complete',{'url':'url','submit':True})
        self.assertEqual(result['status'],'needs_input')
        self.assertNotIn('submission',result)
    def test_background_cancellation_and_persistence(self):
        job=self.service.start('course_run',{})
        self.service.cancel(job['id'])
        self.assertEqual(self.wait(job)['status'],'cancelled')
        records=json.loads((self.service.data_dir/'jobs.json').read_text())
        self.assertEqual(records[0]['status'],'cancelled')
    def test_import_session_copies_privately_and_does_not_change_source(self):
        source=Path(self.tmp.name)/'cookies.txt';source.write_text('UID=example; _uid=example')
        self.service.request('import_session',{'path':str(source)})
        target=self.service.data_dir/'cookies.txt'
        self.assertEqual(target.read_text(),source.read_text())
        self.assertEqual(target.stat().st_mode & 0o777,0o600)
    def test_restart_marks_running_jobs_interrupted_without_replay(self):
        path=self.service.data_dir/'jobs.json'
        path.write_text(json.dumps([{'id':'old','status':'running','action':'work_submit'}]))
        other=AppService(self.service.config_dir,self.service.data_dir)
        self.assertEqual(other.jobs()[0]['status'],'interrupted')

    def test_shutdown_cancels_and_waits_for_background_work(self):
        job=self.service.start('course_run',{})
        self.service.shutdown()
        self.assertEqual(self.wait(job)['status'],'cancelled')
        with self.assertRaises(ValueError):self.service.start('courses',{})

    def test_failed_search_never_auto_submits(self):
        class NoSources(FakeAI):
            def solve_questions(self,*args,**kwargs):
                return [{'id':'q1','answer':'A','status':'ok','sources':[],'search_status':{'state':'failed'}}]
        self.service._ai_factory=NoSources
        result=self.service.request('work_complete',{'url':'url','submit':True,'search':True})
        self.assertEqual(result['status'],'needs_input')
        self.assertNotIn('submission',result)
    def test_allowing_search_does_not_require_model_to_use_it(self):
        class KnowledgeAnswer(FakeAI):
            def solve_questions(self,*args,**kwargs):
                return [{'id':'q1','answer':'A','status':'ok','sources':[],
                         'search_status':{'state':'not_used','mode':'tools','calls':[]}}]
        self.service._ai_factory=KnowledgeAnswer
        result=self.service.request('work_complete',{'url':'url','submit':False,'search':True})
        self.assertTrue(result['ok'])
        self.assertEqual(result['submission']['mode'],'save')
        self.assertEqual(result['answers'][0]['search_status']['state'],'not_used')

    def test_duplicate_course_and_work_writes_are_not_queued(self):
        job=self.service.start('course_run',{'course':{'id':'c1'}})
        with self.assertRaises(ValueError):self.service.start('course_run',{'course':{'id':'c1'}})
        self.service.cancel(job['id']);self.wait(job)
        job=self.service.start('work_complete',{'url':'https://mooc1.chaoxing.com/work?workId=123','submit':True})
        self.wait(job)
        with self.assertRaises(ValueError):self.service.start('work_complete',{'url':'https://mooc1.chaoxing.com/work?workId=123','submit':True})
