"""Isolated JSON bridge to the separately installed Python 3.13 study backend."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time


def process_identity(pid: int) -> str | None:
    try:
        return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


ACTIONS = frozenset({'status', 'login', 'courses', 'course_plan', 'course_run',
                     'work_list', 'work_load', 'work_submit', 'activities', 'sign', 'review_collect'})

ACTION_TIMEOUTS = {'course_run': 86400, 'course_plan': 3600, 'review_collect': 900}


def backend_paths(settings: dict) -> tuple[Path | None, Path | None]:
    installed = Path.home() / '.local/share/chaoxing-linux/study-backend'
    source = Path(__file__).resolve()
    chosen = Path(settings['backend_path']).expanduser() if settings.get('backend_path') else None
    roots = ([chosen, chosen / 'source'] if chosen else
             [installed / 'source', installed, source.parents[3] / 'chaoxing'])
    root = next((p.resolve() for p in roots if (p / 'api/base.py').is_file()), None)
    if settings.get('backend_python'):
        python = Path(settings['backend_python']).expanduser()
    else:
        candidates = [root / '.venv/bin/python', root.parent / '.venv/bin/python'] if root else []
        python = next((p for p in candidates if p.is_file()), None)
    return root, python if python and python.is_file() else None


class BackendClient:
    def __init__(self, settings: dict):
        self.settings = dict(settings)

    def execute(self, action: str, payload: dict | None = None, on_event=None,
                cancel: threading.Event | None = None) -> dict:
        if action not in ACTIONS:
            return {'ok': False, 'status': 'error', 'message': '未知后端操作'}
        root, python = backend_paths(self.settings)
        if not root or not python:
            return {'ok': False, 'status': 'needs_input', 'message': '请先安装学习后端，或设置源码目录与 Python 解释器',
                    'backend_path': str(root) if root else '', 'backend_python': str(python) if python else ''}
        if cancel and cancel.is_set():
            return {'ok': False, 'status': 'cancelled', 'message': '已取消'}
        worker = Path(__file__).with_name('study_worker.py')
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        env['PYTHONPATH'] = str(worker.parent.parent)
        # Credentials travel through the pipe, never through command arguments or environment.
        request = json.dumps({'action': action, 'payload': payload or {}, 'settings': self.settings,
                              '_parent': {'pid': os.getpid(), 'identity': process_identity(os.getpid())}}, ensure_ascii=False)
        process = None
        events: queue.Queue = queue.Queue()
        try:
            process = subprocess.Popen([str(python), str(worker), str(root)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                       encoding='utf-8', env=env, start_new_session=True)
            def read_output():
                for line in process.stdout:
                    events.put(line)
                events.put(None)
            threading.Thread(target=read_output, daemon=True).start()
            process.stdin.write(request + '\n')
            process.stdin.close()
            timeout = float(self.settings.get('backend_timeout', ACTION_TIMEOUTS.get(action, 180)))
            deadline = time.monotonic() + max(1, timeout)
            while True:
                if cancel and cancel.is_set():
                    return {'ok': False, 'status': 'cancelled', 'message': '已终止后台任务；已发送的请求无法撤回'}
                if time.monotonic() > deadline:
                    return {'ok': False, 'status': 'timeout', 'message': '后端操作超时，已停止；请核对服务器状态'}
                try:
                    line = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    return {'ok': False, 'status': 'error', 'message': '学习后端异常退出，请检查解释器及依赖'}
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if message.get('type') == 'event' and on_event:
                    on_event(message.get('event', {}))
                elif message.get('type') == 'result':
                    result = message.get('result')
                    return result if isinstance(result, dict) else {'ok': False, 'status': 'error', 'message': '后端结果格式错误'}
        except (OSError, ValueError, BrokenPipeError):
            return {'ok': False, 'status': 'error', 'message': '无法运行学习后端，请检查解释器及依赖'}
        finally:
            if process:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=2)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                if process.stdout:
                    process.stdout.close()
