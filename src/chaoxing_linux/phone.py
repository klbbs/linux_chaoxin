"""Open the official app on one explicitly selected, authorized USB phone."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

from .runtime import PACKAGE, RuntimeFailure, proc_identity, run


def parse_devices(output):
    devices = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 2 or not any(x.startswith('usb:') for x in fields[2:]):
            continue
        if fields[0].startswith('emulator-') or ':' in fields[0]:
            continue
        metadata = dict(x.split(':', 1) for x in fields[2:] if ':' in x)
        devices.append({'serial': fields[0], 'state': fields[1],
                        'model': metadata.get('model', 'Android 手机').replace('_', ' ')})
    return devices


def select_device(devices, serial=None):
    candidates = [x for x in devices if not serial or x['serial'] == serial]
    if not candidates:
        raise RuntimeFailure('未检测到 USB 手机。请连接数据线，并在手机中开启 USB 调试。')
    if len(candidates) != 1:
        raise RuntimeFailure('检测到多台 USB 手机，请在下拉框选择要连接的手机。')
    device = candidates[0]
    if device['state'] != 'device':
        raise RuntimeFailure('手机尚未授权或未就绪。请解锁手机，并允许这台电脑进行 USB 调试。')
    return device


def scrcpy_path():
    local = Path.home() / '.local/share/chaoxing-linux/tools/scrcpy-v5.0.1/scrcpy'
    return str(local) if local.is_file() and os.access(local, os.X_OK) else shutil.which('scrcpy')


class PhoneRuntime:
    def __init__(self, notify=lambda message: None, *, runner=run):
        self.notify = notify
        self.runner = runner
        bundled = Path.home() / '.local/share/chaoxing-linux/tools/scrcpy-v5.0.1/adb'
        self.adb = shutil.which('adb') or (str(bundled) if bundled.is_file() else 'adb')

    def devices(self):
        if not shutil.which(self.adb):
            raise RuntimeFailure('尚未安装 ADB，请先点击“安装手机连接工具”。')
        return parse_devices(self.runner([self.adb, 'devices', '-l'], timeout=10).stdout)

    def launch(self, serial=None):
        device = select_device(self.devices(), serial)
        executable = scrcpy_path()
        if not executable:
            raise RuntimeFailure('请先点击“安装手机连接工具”，再连接手机。')
        version = self.runner([executable, '--version'], timeout=10).stdout
        match = re.search(r'scrcpy\s+(\d+)\.', version)
        if not match or int(match.group(1)) < 3:
            raise RuntimeFailure('现有 scrcpy 版本较旧，请安装本启动器提供的手机连接工具。')
        result = self.runner([self.adb, '-s', device['serial'], 'shell', 'pm', 'path', PACKAGE],
                             timeout=15, check=False)
        if result.returncode or not re.search(r'^package:', result.stdout, re.M):
            raise RuntimeFailure('这台手机未找到学习通。请先通过手机应用商店安装官方学习通。')
        base = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
        if not base.is_dir() or base.stat().st_uid != os.getuid():
            raise RuntimeFailure('当前桌面用户的运行目录不可用。')
        folder = base / 'chaoxing-linux'
        folder.mkdir(mode=0o700, exist_ok=True)
        state = folder / 'phone-runtime.json'
        try:
            previous = json.loads(state.read_text())
        except (OSError, ValueError):
            previous = {}
        if previous.get('pid') and previous.get('start') and proc_identity(previous['pid']) == previous['start']:
            raise RuntimeFailure('手机窗口已打开，请切换到“学习通 Linux · 手机”窗口。')
        args = [executable, '--serial', device['serial'], '--window-title=学习通 Linux · 手机',
                '--window-height=900', '--max-size=1920', '--stay-awake', '--start-app=' + PACKAGE]
        log_path = folder / 'phone.log'
        env = dict(os.environ)
        env['ADB'] = self.adb
        with log_path.open('w') as log:
            child = subprocess.Popen(args, env=env, stdout=log, stderr=log, start_new_session=True)
        record = {'pid': child.pid, 'start': proc_identity(child.pid), 'serial': device['serial']}
        temporary = state.with_suffix('.tmp')
        temporary.write_text(json.dumps(record))
        temporary.chmod(0o600)
        temporary.replace(state)
        time.sleep(1)
        if child.poll() is not None:
            raise RuntimeFailure('手机窗口启动失败：' + log_path.read_text(errors='replace')[-1800:])
        self.notify('已打开手机窗口，请确认学习通界面。相机、定位和账号仍由手机提供。')

    def stop(self):
        base = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
        state = base / 'chaoxing-linux/phone-runtime.json'
        try:
            previous = json.loads(state.read_text())
        except (OSError, ValueError):
            self.notify('没有本启动器创建的手机窗口。')
            return
        pid = previous.get('pid')
        if pid and previous.get('start') and proc_identity(pid) == previous['start']:
            os.kill(pid, signal.SIGTERM)
        state.unlink(missing_ok=True)
        self.notify('手机窗口已关闭，手机中的应用保留。')
