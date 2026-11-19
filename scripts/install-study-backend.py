#!/usr/bin/env python3
"""Install a pinned, separately licensed study backend into the current user's directory."""
from __future__ import annotations
import argparse
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen

COMMIT='45d2106b16764231c2de2038fb042e71b23d878f'
URL='https://codeload.github.com/Samueli924/chaoxing/tar.gz/'+COMMIT
SHA256='2ef13b516b2f4ac7b42dcd1cf83f40920f223483ffa0fbacdeb5846f04920015'


def extract_source(data,output):
    if sha256(data).hexdigest()!=SHA256:raise ValueError('后端源码归档 SHA256 不匹配。')
    prefix='chaoxing-'+COMMIT+'/'
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
        total=0
        for entry in archive:
            if not entry.name.startswith(prefix):
                if entry.name==prefix.rstrip('/'):continue
                raise ValueError('源码归档目录无效。')
            relative=Path(entry.name[len(prefix):])
            if not relative.parts:continue
            if relative.is_absolute() or '..' in relative.parts or not (entry.isfile() or entry.isdir()):
                raise ValueError('源码归档包含不安全成员。')
            total+=entry.size
            if total>30*1024**2:raise ValueError('源码归档展开大小异常。')
            target=output/relative
            if entry.isdir():target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                with archive.extractfile(entry) as source,target.open('wb') as stream:shutil.copyfileobj(source,stream)
                target.chmod(0o644)
    if not (output/'api/base.py').is_file() or not (output/'LICENSE').is_file():raise ValueError('源码归档缺少必要文件。')


def python_candidate(explicit=None):
    choices=[explicit] if explicit else [shutil.which('python3.13'),str(Path.home()/'.local/share/uv/python/cpython-3.13.18-linux-x86_64-gnu/bin/python3.13')]
    # The existing workspace environment may supply its independently installed interpreter.
    local=Path(__file__).resolve().parents[2]/'chaoxing/.venv/bin/python'
    if not explicit:choices.append(str(local))
    for candidate in choices:
        if not candidate or not Path(candidate).is_file():continue
        try:
            result=subprocess.run([candidate,'-c','import sys;print("%s.%s"%sys.version_info[:2])'],text=True,capture_output=True,timeout=15)
            if result.returncode==0 and result.stdout.strip()=='3.13':return str(Path(candidate).resolve())
        except (OSError,subprocess.TimeoutExpired):pass
    return None


def main():
    parser=argparse.ArgumentParser(description='安装固定版本课程后端（普通用户运行；默认只读计划）')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--python',help='Python 3.13 解释器绝对路径')
    parser.add_argument('--archive',type=Path,help='复用 SHA256 匹配的本地源码归档')
    args=parser.parse_args()
    target=Path.home()/'.local/share/chaoxing-linux/study-backend'
    marker=target/'installation.json'
    candidate=python_candidate(args.python)
    plan={'mode':'apply' if args.apply else 'plan','source':URL,'commit':COMMIT,'sha256':SHA256,'target':str(target),'python':candidate,'python_required':'3.13','license':'GPL-3.0 (upstream LICENSE retained)'}
    if not args.apply:print(json.dumps(plan,ensure_ascii=False,indent=2));return
    if os.geteuid()==0:parser.error('请以普通用户运行，不要使用 sudo。')
    import fcntl
    target.parent.mkdir(parents=True,exist_ok=True)
    install_lock=(target.parent/'.study-backend.install.lock').open('a')
    fcntl.flock(install_lock,fcntl.LOCK_EX)
    def interrupted(signum,frame):
        raise KeyboardInterrupt('安装已停止。')
    signal.signal(signal.SIGTERM,interrupted)
    if target.exists():
        try:installed=json.loads(marker.read_text())
        except (OSError,ValueError):raise ValueError('目标目录已存在且不属于此安装器，保持原样。')
        if installed.get('commit')==COMMIT and (target/'.venv/bin/python').is_file() and (target/'source/api/base.py').is_file():
            print('课程后端已安装，无需重复下载。');return
        raise ValueError('已有不同或不完整的课程后端，保持原样。')
    if not candidate:raise ValueError('未找到 Python 3.13。请安装 Python 3.13 后使用 --python 指定路径，或在设置中复用已有学习通后端。')
    lock=Path(__file__).with_name('study-backend.lock')
    if not lock.is_file():raise ValueError('未找到随安装器提供的依赖锁定清单。')
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.study-backend-',dir=target.parent))
    stage.chmod(0o700)
    try:
        if args.archive:
            if args.archive.stat().st_size>10*1024**2:raise ValueError('源码归档过大。')
            data=args.archive.read_bytes()
        else:
            print('下载固定版本课程源码……',flush=True)
            with urlopen(Request(URL,headers={'User-Agent':'Chaoxing-Linux/0.2'}),timeout=60) as response:data=response.read(10*1024**2+1)
        extract_source(data,stage/'source')
        print('建立独立 Python 环境……',flush=True)
        # Use the final venv path so script shebangs remain valid. Failure removes only our new directory.
        stage.rename(target)
        subprocess.run([candidate,'-m','venv',str(target/'.venv')],check=True)
        print('安装固定版本依赖，首次安装可能较大……',flush=True)
        subprocess.run([str(target/'.venv/bin/python'),'-m','pip','install','--disable-pip-version-check','-r',str(lock)],check=True)
        subprocess.run([str(target/'.venv/bin/python'),'-c','import sys;sys.path.insert(0,sys.argv[1]);import api.base',str(target/'source')],cwd=target,check=True)
        marker.write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n');marker.chmod(0o600)
        print('课程后端安装完成。原有账号、Cookie 和第三方工具保持原样。')
    except BaseException:
        if stage.exists():shutil.rmtree(stage)
        elif target.exists() and not marker.exists():shutil.rmtree(target)
        raise


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.CalledProcessError) as error:
        print('未完成：'+str(error),file=sys.stderr);raise SystemExit(1)
