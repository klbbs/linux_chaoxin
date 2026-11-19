"""Local application facade: private settings, persistent jobs and feature routing."""
from __future__ import annotations

from concurrent.futures import CancelledError
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

DEFAULTS = {
    'backend_path': '', 'backend_python': '', 'username': '', 'password': '',
    'api_base': '', 'api_key': '', 'model': '', 'search_provider': 'bing_rss',
    'ai_provider': 'api_key', 'chatgpt_model': '',
    'search_key': '', 'search_enabled': True, 'search_mode':'tools', 'request_timeout': 30,
    'max_retries': 2,
}
SECRETS = ('password', 'api_key', 'search_key')


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class AppService:
    def __init__(self, config_dir=None, data_dir=None, *, backend_factory=None,
                 ai_factory=None, phone=None):
        self.config_dir = Path(config_dir or Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'chaoxing-linux')
        self.data_dir = Path(data_dir or Path.home()/'.local/share/chaoxing-linux/study-data')
        self.config_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self._backend_lock = threading.Lock()
        self._backend_factory = backend_factory
        self._ai_factory = ai_factory
        self._chatgpt_auth = None
        self._phone = phone
        self._settings = dict(DEFAULTS)
        try:
            loaded = json.loads((self.config_dir/'settings.json').read_text())
            self._settings.update({k:v for k,v in loaded.items() if k in DEFAULTS})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self._jobs = {}
        self._cancel = {}
        self._threads = {}
        self._closing = threading.Event()
        self._recent_writes = {}
        try:
            history=json.loads((self.data_dir/'sign-history.json').read_text())
            self._sign_history=[item for item in history if isinstance(item,dict)][-80:] if isinstance(history,list) else []
        except (OSError,ValueError):
            self._sign_history=[]
        try:
            records = json.loads((self.data_dir/'jobs.json').read_text())
            if isinstance(records, list):
                for job in records[-100:]:
                    if not isinstance(job, dict) or not job.get('id'): continue
                    if job.get('status') in ('queued','running','cancelling'):
                        job.update(status='interrupted',message='上次程序退出，任务已中断；请先检查远端状态再重新执行。')
                    self._jobs[job['id']] = job
        except (OSError, ValueError):
            pass

    def settings(self):
        with self._lock:
            values = deepcopy(self._settings)
        values['data_dir'] = str(self.data_dir)
        values['config_dir'] = str(self.config_dir)
        return values

    def save_settings(self, values):
        if not isinstance(values, dict): raise ValueError('设置格式错误。')
        with self._lock:
            candidate = dict(self._settings)
            for key,value in values.items():
                if key not in DEFAULTS: continue
                if key in ('search_enabled',):
                    if not isinstance(value,bool):raise ValueError('联网搜索开关必须为布尔值。')
                elif key in ('request_timeout','max_retries'):
                    if isinstance(value,bool):raise ValueError('超时与重试次数需要整数。')
                    value = int(value)
                    if not (1 <= value <= 180 if key=='request_timeout' else 0 <= value <= 5):
                        raise ValueError('超时或重试次数超出范围。')
                elif not isinstance(value,str): raise ValueError('设置项需要文本。')
                if key=='search_mode' and value.strip() not in ('tools','native','prefetch'):
                    raise ValueError('请选择模型搜索工具、模型原生联网或兼容预搜索。')
                if key == 'ai_provider' and value.strip() not in ('chatgpt', 'api_key'):
                    raise ValueError('请选择 ChatGPT 账号或 API Key。')
                if key == 'chatgpt_model' and (len(value) > 128 or any(char.isspace() for char in value.strip())):
                    raise ValueError('请从 ChatGPT 模型列表选择有效模型。')
                candidate[key] = value.strip() if isinstance(value,str) and key not in SECRETS else value
            atomic_json(self.config_dir/'settings.json',candidate)
            self._settings = candidate
        return {'ok':True,'message':'设置已保存在本机当前用户目录。'}

    def _clean(self, value):
        # Results and persistent job records never include account credentials.
        secrets = [str(self._settings.get(k,'')) for k in (*SECRETS,'username')]
        if isinstance(value,dict):
            return {k:self._clean(v) for k,v in value.items()
                    if not re.search(r'password|api[_-]?key|search[_-]?key|cookies?|authorization|token',str(k),re.I)}
        if isinstance(value,(list,tuple)):return [self._clean(v) for v in value]
        if isinstance(value,str):
            for secret in secrets:
                if secret:value=value.replace(secret,'[已隐藏]')
            value=re.sub(r'(?i)(bearer\s+)[^\s\"\']+',r'\1[已隐藏]',value)
            value=re.sub(r'(https?://)[^/\s:@]+:[^/@\s]+@',r'\1[已隐藏]@',value)
            return value
        return value

    def _backend(self, settings=None):
        settings = self.settings() if settings is None else settings
        if settings.get('ai_provider') == 'chatgpt':
            settings = {**settings, 'api_key': '', 'search_key': ''}
        if self._backend_factory:return self._backend_factory(settings)
        from .study_backend import BackendClient
        return BackendClient(settings)

    @staticmethod
    def _write_cookie(path, text):
        path = Path(path)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(text)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _sync_session(self, event, cancel):
        """Called under the backend lock; validate isolated copies before replacing a session."""
        with self._lock:
            if any(job.get('_write_key') and job.get('status') in ('queued','running','cancelling')
                   for job in self._jobs.values()):
                return {'ok':False,'status':'needs_input','message':'请先等待或停止当前账号写入任务，再同步登录状态。'}
            self._session_syncing=True
        try:
            return self._discover_session(event,cancel)
        finally:
            with self._lock:self._session_syncing=False

    def _discover_session(self, event, cancel):
        from .session_sync import SessionCandidate, discover_sessions
        from .study_backend import backend_paths
        settings = self.settings()
        root, python = backend_paths(settings)
        if self._backend_factory is None and (root is None or python is None):
            return {'ok':False,'status':'needs_input','courses':[],
                    'message':'请先安装课程服务组件，再同步学习通登录状态。'}
        candidates = discover_sessions(backend_root=root)
        current = self.data_dir/'cookies.txt'
        try:
            if current.stat().st_size <= 65536:
                text = current.read_text().strip()
                if text:candidates.append(SessionCandidate(source='当前工作台',cookies=text))
        except (OSError,UnicodeError):
            pass
        seen = set()
        for candidate in candidates:
            if cancel.is_set():raise CancelledError()
            fingerprint = hashlib.sha256(candidate.cookies.encode()).digest()
            if fingerprint in seen:continue
            seen.add(fingerprint)
            event({'message':'正在检查本机已有的学习通登录状态。'})
            with tempfile.TemporaryDirectory(prefix='session-check-',dir=self.data_dir) as private:
                self._write_cookie(Path(private)/'cookies.txt',candidate.cookies)
                isolated = dict(settings,data_dir=private,backend_data_dir=private,
                                username='',password='',backend_timeout=60)
                result = self._backend(isolated).execute('courses',{},on_event=lambda _:None,cancel=cancel)
                if cancel.is_set():raise CancelledError()
                if result.get('ok') is not True or not isinstance(result.get('courses'),list):
                    continue
                # The source remains untouched; only a verified copy enters the workspace.
                self._write_cookie(current,(Path(private)/'cookies.txt').read_text())
                return {'ok':True,'status':'success','source':candidate.source,
                        'courses':result['courses'],
                        'message':'已同步学习通登录状态，无需填写账号密码。来源：'+candidate.source}
        return {'ok':False,'status':'needs_input','courses':[],
                'message':'未找到有效的本机学习通会话。请在本机 Firefox 打开学习通官网完成扫码登录，再点击同步；无需在工作台填写密码。'}

    def _ai(self):
        settings = self.settings()
        if self._ai_factory:
            return self._ai_factory(settings)
        from .chatgpt_ai import configured_ai
        auth = self._chatgpt() if settings.get('ai_provider') == 'chatgpt' else None
        return configured_ai(settings, auth)

    def _chatgpt(self):
        from .chatgpt_auth import ChatGPTAuth
        with self._lock:
            if self._chatgpt_auth is None:
                self._chatgpt_auth = ChatGPTAuth(self.config_dir)
            return self._chatgpt_auth

    def _chatgpt_action(self, action, payload, event, cancel):
        from .assistant_ai import AIError
        from .chatgpt_ai import list_chatgpt_models

        auth = self._chatgpt()
        try:
            profile_id = payload.get('profile_id') or None
            if profile_id is not None and (not isinstance(profile_id, str) or len(profile_id) > 128):
                raise ValueError('请选择有效的 ChatGPT 账号。')
            if action == 'chatgpt_status':
                return {'ok': True, **auth.status()}
            if action == 'chatgpt_models':
                models = list_chatgpt_models(auth, profile_id=profile_id)
                return {'ok': True, 'models': models}
            if action == 'chatgpt_select':
                return {'ok': True, **auth.select(profile_id)}
            if action == 'chatgpt_sign_out':
                return {'ok': True, **auth.sign_out(profile_id)}
            if action == 'chatgpt_sign_in':
                result = auth.sign_in(profile_id=profile_id, cancel=cancel,
                                      notify=lambda message: event({'message': str(message)[:200]}))
                if cancel.is_set():
                    raise CancelledError()
                connected = bool(result.get('connected'))
                return {**result, 'ok': connected, 'status': 'success' if connected else 'needs_input',
                        'message': ('ChatGPT 已授权，正在读取可用模型。' if connected
                                    else '已登录，但账号没有向本应用授权 ChatGPT 套餐用量。')}
        except (AIError, ValueError) as exc:
            if cancel.is_set():
                raise CancelledError() from None
            return {'ok': False, 'status': 'needs_input', 'message': str(exc)}
        raise ValueError('不支持的 ChatGPT 账号操作。')

    def _phone_controller(self):
        if self._phone is None:
            from .phone_control import PhoneController
            self._phone = PhoneController()
        return self._phone

    def status(self):
        return self.request('status')

    def request(self, action, payload=None):
        return self._clean(self._execute(action,dict(payload or {}),lambda event:None,self._closing))

    def _execute(self, action, payload, event, cancel):
        if cancel.is_set(): raise CancelledError()
        if action.startswith('chatgpt_'):
            return self._chatgpt_action(action, payload, event, cancel)
        if action=='session_sync':
            with self._backend_lock:
                return self._sync_session(event,cancel)
        if action.startswith('locations_'):
            return self._locations(action,payload)
        if action=='sign_history':
            with self._lock:return {'ok':True,'history':deepcopy(list(reversed(self._sign_history)))}
        if action=='sign' and payload.get('sign_type')=='location':
            from .location import location_record,wgs84_to_gcj02
            point=location_record(payload.get('latitude'),payload.get('longitude'),
                                  coordinate_system=payload.get('coordinate_system','gcj02'))
            if point['coordinate_system']=='wgs84':
                payload['latitude'],payload['longitude']=wgs84_to_gcj02(point['latitude'],point['longitude'])
                payload['coordinate_system']='gcj02'
        if action=='ai_test':
            result=self._ai().test_connection()
            ok=result.get('ok') is True
            return {**result,'status':'success' if ok else 'needs_input',
                    'message':'AI连接测试成功。' if ok else 'AI连接测试失败：'+str(result.get('error') or result.get('message') or '服务没有返回有效结果。')}
        if action == 'review_export':
            from .review import run_review_export
            return run_review_export(self, payload, event, cancel)
        if action=='phone_devices':return {'ok':True,'devices':self._phone_controller().devices()}
        if action.startswith('phone_'):
            return self._phone_controller().execute(action[6:],payload)
        if action=='import_session':
            source=Path(payload.get('path','')).expanduser()
            if not source.is_absolute() or not source.is_file() or source.stat().st_size>65536:
                raise ValueError('请选择有效的本地 Cookie 文本文件。')
            data=source.read_text().strip()
            if not data or not all('=' in p for p in data.split(';') if p.strip()):
                raise ValueError('Cookie 文件需要 name=value;name=value 格式。')
            # Never touch the source or phone's private application files.
            with self._backend_lock:
                if cancel.is_set():raise CancelledError()
                self._write_cookie(self.data_dir/'cookies.txt',data)
            return {'ok':True,'message':'会话已复制到独立目录。请刷新课程验证是否有效。'}
        if action=='install_backend':return self._install_backend(event,cancel)
        if action in ('work_solve','work_complete'):
            with self._backend_lock:
                work=self._backend().execute('work_load',payload,on_event=event,cancel=cancel)
                if not work.get('questions') or work.get('ok') is False:return work
                questions=work['questions']
                def progress(done,total,result):
                    event({'message':f'联网解题 {done}/{total}','progress':done/max(1,total)})
                answers=self._ai().solve_questions(questions,search=bool(payload.get('search',True)),progress=progress,cancel=cancel)
                result={'ok':True,'status':'success','url':payload.get('url'),
                        'questions':questions,'answers':answers,'context':work.get('context',{}),
                        'message':'解题完成，请检查答案与来源。'}
                unknown=[a for a in answers if a.get('status')!='ok' or a.get('answer') is None]
                unverified=[a for a in answers if payload.get('search',True) and
                            a.get('search_status',{}).get('state') not in ('disabled','not_used') and
                            (a.get('search_status',{}).get('state')!='ok' or not a.get('sources'))]
                if unknown:
                    result.update(ok=False,status='needs_input',message=f'{len(unknown)} 题答案未确定，请补全后继续。')
                elif unverified:
                    result.update(status='partial',message=f'{len(unverified)} 题搜索未取得可核验来源，已保留模型参考答案供检查。')
                if action=='work_complete':
                    if cancel.is_set():raise CancelledError()
                    if unknown or unverified:
                        result.update(ok=False,status='needs_input',message=f'{len(unknown)} 题答案未确定，{len(unverified)} 题缺少联网来源；未自动保存或提交。请检查后继续。')
                    else:
                        submitted=self._backend().execute('work_submit',{**result,'mode':'submit' if payload.get('submit') else 'save'},on_event=event,cancel=cancel)
                        result['submission']=submitted
                        result.update(ok=submitted.get('ok',False),status=submitted.get('status','error'),message=submitted.get('message','已处理提交请求。'))
                return result
        if action=='work_submit':
            answers=payload.get('answers',[])
            if payload.get('mode')=='submit' and any(isinstance(a,dict) and (a.get('status')=='unknown' or a.get('answer') is None) for a in answers):
                raise ValueError('仍有未确定答案，请补全后再正式提交。')
        if action not in {'status','login','courses','course_plan','course_run','work_list','work_load','work_submit','activities','sign'}:
            raise ValueError('不支持的操作。')
        with self._backend_lock:
            if cancel.is_set():raise CancelledError()
            result=self._backend().execute(action,payload,on_event=event,cancel=cancel)
            if action=='courses' and result.get('ok') is False and result.get('status')=='needs_input':
                return self._sync_session(event,cancel)
            return result

    def _persist(self):
        history = []
        for job in list(self._jobs.values())[-100:]:
            if job.get('action') == 'chatgpt_sign_in' and isinstance(job.get('result'), dict):
                job = {**job, 'result': {key: value for key, value in job['result'].items()
                                         if key in ('ok', 'status', 'message')}}
            history.append(job)
        atomic_json(self.data_dir/'jobs.json', history)

    def _locations(self,action,payload):
        from .location import location_record
        path=self.data_dir/'locations.json'
        with self._lock:
            try:
                records=json.loads(path.read_text())
                if not isinstance(records,list) or any(not isinstance(record,dict) for record in records):raise ValueError()
            except FileNotFoundError:
                records=[]
            except (ValueError,OSError):
                raise ValueError('常用位置记录无法读取，请先备份该文件再处理。')
            if action=='locations_list':return {'ok':True,'locations':deepcopy(records)}
            ident=str(payload.get('id',''))
            if action=='locations_delete':
                filtered=[r for r in records if r.get('id')!=ident]
                if not ident or len(filtered)==len(records):raise ValueError('没有找到这个常用位置。')
                atomic_json(path,filtered)
                return {'ok':True,'message':'已删除常用位置。'}
            if action!='locations_save':raise ValueError('不支持的位置操作。')
            name=str(payload.get('name') or payload.get('label') or '').strip()
            if not name or len(name)>80:raise ValueError('请填写80字以内的常用位置标签。')
            if any(str(r.get('name','')).casefold()==name.casefold() and r.get('id')!=ident for r in records):
                raise ValueError('常用位置标签不能重复。')
            address=str(payload.get('address','')).strip()
            if len(address)>500:raise ValueError('位置说明过长，请缩短后保存。')
            point=location_record(payload.get('latitude'),payload.get('longitude'),
                                  coordinate_system=payload.get('coordinate_system','gcj02'),
                                  name=name,address=address,source=str(payload.get('source','manual'))[:60])
            point['id']=ident or uuid.uuid4().hex[:12]
            point['updated_at']=datetime.now(timezone.utc).isoformat()
            exists=any(r.get('id')==ident for r in records)
            if ident and not exists:raise ValueError('常用位置已被删除，请重新保存。')
            if not exists and len(records)>=200:raise ValueError('最多保存200个常用位置，请先删除不需要的地点。')
            records=[point if r.get('id')==ident else r for r in records] if exists else records+[point]
            atomic_json(path,records)
            return {'ok':True,'location':deepcopy(point),'message':'常用位置已保存，仅本机可见。'}

    def start(self, action, payload=None):
        job_id=uuid.uuid4().hex[:12]
        payload=deepcopy(payload or {})
        write_key=self._write_key(action,payload)
        fingerprint=hashlib.sha256(json.dumps([action,payload],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self._lock:
            if self._closing.is_set():raise ValueError('程序正在退出，不能添加任务。')
            if write_key:
                if getattr(self,'_session_syncing',False):
                    raise ValueError('正在同步登录状态，请完成后再添加写入任务。')
                if any(j.get('_write_key')==write_key and j.get('status') in ('queued','running','cancelling') for j in self._jobs.values()):
                    raise ValueError('相同作业、课程或签到已有任务，不能重复添加。')
                recent=self._recent_writes.get(fingerprint)
                if recent and time.monotonic()-recent<30:
                    raise ValueError('相同操作刚刚完成，请先检查结果，稍后再试。')
            if sum(j.get('status') in ('running','queued','cancelling') for j in self._jobs.values())>=4:
                raise ValueError('已有四个任务，请等待完成或停止后再添加。')
            job={'id':job_id,'action':action,'status':'queued','progress':0,
                 'created_at':datetime.now(timezone.utc).isoformat(),'message':'等待执行','events':[]}
            if write_key:job['_write_key']=write_key
            job['_fingerprint']=fingerprint
            if action=='sign':
                course=payload.get('course') or {}
                job['summary']={
                    'course':str(course.get('name') or course.get('title') or ''),
                    'activity_id':str(payload.get('activity_id') or payload.get('activeId') or ''),
                    'sign_type':str(payload.get('sign_type','normal')),
                    'address':str(payload.get('address',''))[:500],
                }
            self._jobs[job_id]=job
            cancel=self._cancel[job_id]=threading.Event()
            self._persist()
        thread=threading.Thread(target=self._run,args=(job_id,action,payload,cancel),daemon=True,name='study-'+job_id)
        with self._lock:self._threads[job_id]=thread
        thread.start()
        with self._lock:return deepcopy({k:v for k,v in job.items() if not k.startswith('_')})

    @staticmethod
    def _write_key(action,payload):
        if action in ('work_submit','work_complete'):
            parsed=urlsplit(str(payload.get('url','')))
            query=[(k,v) for k,v in parse_qsl(parsed.query) if k.lower() in ('courseid','classid','clazzid','workid','answerid','cpi')]
            target=urlunsplit(('',parsed.netloc,parsed.path,urlencode(sorted(query)) if query else parsed.query,''))
            domain='work'
        elif action=='course_run':
            course=payload.get('course',payload)
            target=str(course.get('courseId') or course.get('courseid') or course.get('id'))+':'+str(course.get('clazzId') or course.get('clazzid') or course.get('classId'))
            domain='course'
        elif action=='sign':
            target=str(payload.get('activity_id') or payload.get('activeId'))
            domain='sign'
        elif action=='install_backend':
            target='study-backend';domain='install'
        else:return None
        return domain+':'+hashlib.sha256(target.encode()).hexdigest()

    def _run(self,job_id,action,payload,cancel):
        def event(value):
            with self._lock:
                job=self._jobs[job_id]
                item=self._clean(value if isinstance(value,dict) else {'message':str(value)})
                job['events']=(job['events']+[item])[-150:]
                if 'message' in item:job['message']=str(item['message'])
                if 'progress' in item:job['progress']=item['progress']
                self._persist()
        try:
            with self._lock:
                self._jobs[job_id].update(status='running',message='正在执行')
            result=self._execute(action,payload,event,cancel)
            status=result.get('status','success' if result.get('ok',True) else 'error') if isinstance(result,dict) else 'success'
            with self._lock:
                self._jobs[job_id].update(result=self._clean(result),status='cancelled' if cancel.is_set() else status,
                    message=self._clean(result.get('message','任务已完成')) if isinstance(result,dict) else '任务已完成',progress=1)
        except Exception as exc:
            with self._lock:
                self._jobs[job_id].update(status='cancelled' if cancel.is_set() or isinstance(exc,CancelledError) else 'error',message=self._clean(str(exc) or '操作已停止'))
        finally:
            with self._lock:
                self._jobs[job_id]['finished_at']=datetime.now(timezone.utc).isoformat()
                if self._jobs[job_id].get('_write_key') and self._jobs[job_id]['status'] in ('success','partial'):
                    self._recent_writes[self._jobs[job_id]['_fingerprint']]=time.monotonic()
                if action=='sign':
                    job=self._jobs[job_id]
                    record={k:deepcopy(v) for k,v in job.items() if k in ('id','action','created_at','finished_at','status','message','summary','result')}
                    self._sign_history=(self._sign_history+[record])[-80:]
                    atomic_json(self.data_dir/'sign-history.json',self._sign_history)
                self._cancel.pop(job_id,None)
                self._threads.pop(job_id,None)
                self._persist()

    def jobs(self):
        with self._lock:return deepcopy([{k:v for k,v in job.items() if not k.startswith('_')} for job in self._jobs.values()])

    def cancel(self,job_id):
        with self._lock:
            cancel=self._cancel.get(job_id)
            if cancel:
                cancel.set();self._jobs[job_id].update(status='cancelling',message='正在停止；已发送的远端操作不会撤销。');self._persist()
        return {'ok':True,'message':'已请求停止。' if cancel else '任务已经结束。'}

    def shutdown(self,timeout=5):
        import time
        self._closing.set()
        with self._lock:
            for token in self._cancel.values():token.set()
            threads=list(self._threads.values())
        deadline=time.monotonic()+timeout
        for thread in threads:
            if thread is not threading.current_thread():thread.join(max(0,deadline-time.monotonic()))

    def _install_backend(self,event,cancel):
        from .gui import installer_path
        script=installer_path('install-study-backend.py')
        import tempfile
        output_stream=tempfile.TemporaryFile(mode='w+')
        process=subprocess.Popen(['/usr/bin/python3',str(script),'--apply'],stdout=output_stream,stderr=subprocess.STDOUT,text=True,start_new_session=True)
        def terminate():
            import signal
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
        try:
            event({'message':'正在安装固定版本课程组件，请等待依赖安装完成。'})
            while process.poll() is None:
                if cancel.wait(0.2):terminate();process.wait(timeout=10);raise CancelledError()
            output_stream.seek(0)
            output=output_stream.read()[-3000:]
        finally:
            output_stream.close()
        event({'message':output})
        return {'ok':process.returncode==0,'status':'success' if process.returncode==0 else 'error','message':output}
