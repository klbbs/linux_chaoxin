"""USB phone controls, scoped to the selected official Chaoxing foreground app."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import threading

from .phone import PhoneRuntime, select_device
from .runtime import PACKAGE, RuntimeFailure


class PhoneController:
    def __init__(self, runtime=None, *, runner=subprocess.run, directory=None):
        self.runtime=runtime or PhoneRuntime()
        self.runner=runner
        self.directory=Path(directory or Path(os.environ.get('XDG_RUNTIME_DIR',f'/run/user/{os.getuid()}'))/'chaoxing-linux')
        self._lock=threading.RLock()
        self._sizes={}

    def devices(self):return self.runtime.devices()

    def _run(self,serial,*arguments,binary=False):
        result=self.runner([self.runtime.adb,'-s',serial,*arguments],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           text=not binary,timeout=15,check=False)
        if result.returncode:
            raise RuntimeFailure('手机操作失败，请检查 USB 授权和连接。')
        return result.stdout

    def _serial(self,payload):
        return select_device(self.devices(),payload.get('serial'))['serial']

    def _foreground(self,serial):
        text=self._run(serial,'shell','dumpsys','window','windows')
        # Current focus is stronger than stale window entries. Refuse capture/input in other apps.
        matches=re.findall(r'mCurrentFocus\s*=\s*Window\{[^\n]*?\s([\w.]+)/[^\n]*\}',text)
        if not matches:
            text=self._run(serial,'shell','dumpsys','activity','activities')
            matches=re.findall(r'mResumedActivity:.*?\s([\w.]+)/',text)
        if not matches or any(package!=PACKAGE for package in matches):
            raise RuntimeFailure('请先在手机上解锁并打开学习通。当前不截取或控制其他应用。')

    def execute(self,action,payload):
        with self._lock:
            serial=self._serial(payload)
            if action=='open':
                self.runtime.launch(serial)
                return {'ok':True,'message':'已打开手机学习通窗口。'}
            if action=='stop':
                self.runtime.stop()
                return {'ok':True,'message':'手机窗口已关闭。'}
            if action=='focus':
                result=self._run(serial,'shell','cmd','package','resolve-activity','--brief',PACKAGE)
                components=[x.strip() for x in result.splitlines() if re.fullmatch(r'com\.chaoxing\.mobile/[\w.$]+',x.strip())]
                if not components:raise RuntimeFailure('手机尚未安装官方学习通。')
                self._run(serial,'shell','am','start','-n',components[-1])
                return {'ok':True,'message':'已打开手机中的学习通。'}
            if action=='screen':
                self._foreground(serial)
                data=self._run(serial,'exec-out','screencap','-p',binary=True)
                if not isinstance(data,bytes) or not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data)<24 or len(data)>32*1024**2:
                    raise RuntimeFailure('未取得有效手机画面。')
                width,height=struct.unpack('>II',data[16:24])
                if not 1<=width<=10000 or not 1<=height<=10000:raise RuntimeFailure('手机画面尺寸无效。')
                self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
                if self.directory.stat().st_uid!=os.getuid():raise RuntimeFailure('手机预览目录不属于当前用户。')
                path=self.directory/('preview-'+re.sub(r'[^a-zA-Z0-9_-]','_',serial)+'.png')
                temporary=path.with_suffix('.tmp')
                fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                with os.fdopen(fd,'wb') as stream:stream.write(data)
                temporary.replace(path);path.chmod(0o600)
                self._sizes[serial]=(width,height)
                return {'ok':True,'path':str(path),'width':width,'height':height}
            if action=='key':
                names={'BACK':'4','HOME':'3','APP_SWITCH':'187','ENTER':'66','DEL':'67'}
                key=str(payload.get('key','')).upper()
                if key not in names:raise ValueError('不支持的手机按键。')
                self._foreground(serial)
                self._run(serial,'shell','input','keyevent',names[key])
            elif action=='tap':
                self._foreground(serial)
                if serial not in self._sizes:raise ValueError('请先刷新手机画面再点击。')
                width,height=self._sizes[serial]
                x,y=int(payload.get('x',-1)),int(payload.get('y',-1))
                if not (0<=x<width and 0<=y<height):raise ValueError('点击位置超出手机画面。')
                self._run(serial,'shell','input','tap',str(x),str(y))
            elif action=='swipe':
                self._foreground(serial)
                if serial not in self._sizes:raise ValueError('请先刷新手机画面。')
                width,height=self._sizes[serial]
                values=[int(payload.get(k,-1)) for k in ('x1','y1','x2','y2')]
                if not all(0<=v<(width if i%2==0 else height) for i,v in enumerate(values)):raise ValueError('滑动位置超出画面。')
                self._run(serial,'shell','input','swipe',*(str(v) for v in values),'350')
            elif action=='text':
                self._foreground(serial)
                text=str(payload.get('text',''))
                if not text or len(text)>1000 or any(ord(c)<32 or ord(c)>126 for c in text):
                    raise ValueError('此输入方式支持英文、数字和符号；中文请使用手机键盘或独立手机窗口输入。')
                # adb shell joins arguments into an Android shell command; explicitly quote it.
                escaped=text.replace('%s','\\%s').replace(' ','%s')
                self._run(serial,'shell','input','text',shlex.quote(escaped))
            else:raise ValueError('不支持的手机操作。')
            return {'ok':True,'message':'手机操作已执行。'}
