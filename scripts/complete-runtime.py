#!/usr/bin/python3
"""Finish a fresh, incomplete initialization from officially verified image ZIPs."""
import argparse
import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(data)
    return result.hexdigest()


def process_start(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def verify_zip(path, kind, expected):
    if path.stat().st_size != expected['size'] or digest(path) != expected['sha256']:
        raise ValueError(f'{kind} 镜像大小或官方 SHA256 不匹配。')
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) != 1 or members[0].filename != kind + '.img':
            raise ValueError(f'{kind} 归档必须只包含 {kind}.img。')
        item = members[0]
        if item.file_size <= 0 or item.file_size > 8 * 1024**3:
            raise ValueError('镜像展开大小无效。')
        return item.file_size


def main():
    parser = argparse.ArgumentParser(description='使用官方校验的并行下载镜像完成首次初始化')
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--replace-init-pid', type=int)
    parser.add_argument('--replace-init-start')
    parser.add_argument('--apply', action='store_true', help='管理员应用；默认只读计划')
    args = parser.parse_args()
    if args.apply and os.geteuid() != 0:
        parser.error('应用需要管理员权限。')
    spec = importlib.util.spec_from_file_location('chaoxing_image_fetch', Path(__file__).with_name('fetch-images.py'))
    fetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch)
    manifest = json.loads(args.manifest.read_text())
    if manifest.get('status') != 'complete':
        raise ValueError('并行下载尚未全部完成。')
    plan = {}
    for kind, channel in fetch.official_channels().items():
        expected = fetch.fetch_metadata(channel)
        recorded = manifest['images'][kind]
        if not recorded.get('verified') or any(recorded[k] != expected[k] for k in ('filename', 'sha256', 'size', 'url')):
            raise ValueError('缓存清单与当前官方 OTA 不一致。')
        # Do not trust a manifest-supplied arbitrary extraction path.
        path = args.manifest.resolve().parent / expected['filename']
        if path.is_symlink() or not path.is_file():
            raise ValueError('缓存镜像不是普通文件。')
        length = verify_zip(path, kind, expected)
        plan[kind] = {'archive': str(path), 'bytes': length, **expected}
    cfg = configparser.ConfigParser()
    cfg.read('/var/lib/waydroid/waydroid.cfg')
    images = Path('/var/lib/waydroid/images')
    if (cfg.get('waydroid', 'images_path', fallback='') != str(images)
            or any(cfg.getint('waydroid', k + '_datetime', fallback=0) != 0 for k in ('system', 'vendor'))
            or any((images / (k + '.img')).exists() for k in ('system', 'vendor'))):
        raise ValueError('只支持本次尚未完成的默认初始化；已有镜像和已完成的环境保持原样。')
    if not args.apply:
        print(json.dumps({'mode': 'plan', 'images': plan}, indent=2, ensure_ascii=False))
        return
    status = subprocess.run(['/usr/bin/waydroid', 'status'], capture_output=True, text=True, timeout=15)
    fresh_uninitialized = ('is not initialized' in status.stdout
                           and not Path('/var/lib/waydroid/rootfs').exists())
    if status.returncode or ('Session:\tSTOPPED' not in status.stdout and not fresh_uninitialized):
        raise ValueError('Android 会话未停止，不能应用首次初始化镜像。')
    if args.replace_init_pid and process_start(args.replace_init_pid):
        if not args.replace_init_start or process_start(args.replace_init_pid) != args.replace_init_start:
            raise ValueError('原下载进程身份已变化。')
        cmd = Path(f'/proc/{args.replace_init_pid}/cmdline').read_bytes().split(b'\0')
        if cmd[:5] != [b'/usr/bin/python3', b'/usr/bin/waydroid', b'init', b'-s', b'VANILLA']:
            raise ValueError('原进程不是本次预期的 VANILLA 初始化。')
        os.kill(args.replace_init_pid, signal.SIGINT)
        for _ in range(100):
            if process_start(args.replace_init_pid) is None:
                break
            time.sleep(0.1)
        if process_start(args.replace_init_pid) is not None:
            raise ValueError('原下载进程仍未退出，未替换镜像。')
    if images.is_symlink():
        raise ValueError('默认镜像目录是符号链接。')
    images.mkdir(parents=True, exist_ok=True)
    stages = []
    try:
        for kind, entry in plan.items():
            handle, temporary = tempfile.mkstemp(prefix=f'.{kind}-', dir=images)
            stages.append(Path(temporary))
            with os.fdopen(handle, 'wb') as output, zipfile.ZipFile(entry['archive']) as archive, archive.open(kind + '.img') as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush(); os.fsync(output.fileno())
            os.chmod(temporary, 0o644)
        for kind, stage in zip(plan, stages):
            stage.replace(images / (kind + '.img'))
            cfg.set('waydroid', kind + '_datetime', str(plan[kind]['datetime']))
        cfg_path = Path('/var/lib/waydroid/waydroid.cfg')
        backup = cfg_path.with_name('waydroid.cfg.chaoxing-initial-backup')
        if not backup.exists():
            shutil.copy2(cfg_path, backup)
        with cfg_path.open('w') as output:
            cfg.write(output)
        subprocess.run(['/usr/bin/waydroid', 'init', '-s', 'VANILLA'], check=True, timeout=180)
        subprocess.run(['/usr/bin/systemctl', 'enable', '--now', 'waydroid-container'], check=True, timeout=60)
        # Host-side ADB is used only for the requested REA verification of this
        # newly created Android instance. It does not enable USB debugging or
        # change Android authentication settings.
        subprocess.run(['/usr/bin/apt-get', 'install', '-y', 'adb'], check=True, timeout=180)
        print('官方校验镜像已安装，首次初始化已完成。', flush=True)
    finally:
        for stage in stages:
            stage.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'初始化未完成：{error}', file=sys.stderr)
        sys.exit(1)
