#!/usr/bin/python3
"""Install the pinned official scrcpy static release for the current user."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

VERSION = '5.0.1'
URL = 'https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/scrcpy-linux-x86_64-v5.0.1.tar.gz'
SHA256 = '9f969d30cc574816077edecda65719c1c70b0aee1df1ba5fa00779ac07b8fd6e'
ARCHIVE_NAME = 'scrcpy-linux-x86_64-v5.0.1.tar.gz'
ARCHIVE_ROOT = 'scrcpy-linux-x86_64-v5.0.1'
RECORD = '.chaoxing-linux-scrcpy.json'
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024


def default_target():
    return Path.home() / '.local/share/chaoxing-linux/tools/scrcpy-v5.0.1'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_destination(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('安装目标必须是绝对路径。')
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise ValueError(f'拒绝符号链接安装路径：{component}')
        if component.exists() and component != path and not component.is_dir():
            raise ValueError(f'安装路径的父项不是目录：{component}')
    return path


def inspect_archive(archive, expected_sha=SHA256):
    """Hash before reading tar members; never use tar.extract or extractall."""
    archive = Path(archive)
    with archive.open('rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('归档必须是普通文件。')
        raw = source.read(MAX_ARCHIVE + 1)
    if len(raw) > MAX_ARCHIVE or digest(raw) != expected_sha:
        raise ValueError('官方 scrcpy 归档 SHA256 不匹配或超过大小上限。')
    files = {}
    directories = set()
    seen = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as packed:
        for index, member in enumerate(packed):
            if index >= 256:
                raise ValueError('归档条目超过安全上限。')
            name = member.name.rstrip('/')
            parts = name.split('/')
            if (not name or PurePosixPath(name).is_absolute() or '\\' in name or '\x00' in name
                    or any(part in ('', '.', '..') for part in parts) or ':' in parts[0]
                    or parts[0] != ARCHIVE_ROOT or name in seen):
                raise ValueError(f'归档路径重复或不安全：{member.name}')
            seen.add(name)
            if not member.isdir() and not member.isfile():
                raise ValueError(f'拒绝归档中的链接、设备或其他特殊条目：{member.name}')
            relative = '/'.join(parts[1:])
            if not relative:
                if not member.isdir():
                    raise ValueError('归档根路径必须是目录。')
                continue
            if relative == RECORD:
                raise ValueError('归档试图写入本工具安装记录。')
            if member.isdir():
                directories.add(relative)
                continue
            total += member.size
            if member.size < 0 or total > MAX_EXPANDED:
                raise ValueError('归档展开超过安全上限。')
            source = packed.extractfile(member)
            if source is None:
                raise ValueError('无法读取归档普通文件。')
            with source:
                data = source.read(member.size + 1)
            if len(data) != member.size:
                raise ValueError('归档文件长度不符。')
            files[relative] = {'data': data, 'sha256': digest(data),
                               'mode': 0o755 if member.mode & 0o111 else 0o644}
    if 'scrcpy' not in files or files['scrcpy']['mode'] != 0o755 or 'scrcpy-server' not in files:
        raise ValueError('归档缺少 scrcpy 可执行文件或 scrcpy-server。')
    for name in [*files, *directories]:
        parents = PurePosixPath(name).parents
        if any(str(parent) in files for parent in parents if str(parent) != '.'):
            raise ValueError('归档中的文件与目录路径冲突。')
    return files, directories


def verify_existing(target, files, directories, archive_sha=SHA256):
    target = safe_destination(target)
    record = target / RECORD
    if not target.is_dir() or record.is_symlink() or not record.is_file():
        raise ValueError(f'安装目录已存在且没有本工具记录，拒绝覆盖：{target}')
    saved = json.loads(record.read_text(encoding='utf-8'))
    expected_files = {name: {'sha256': entry['sha256'], 'mode': entry['mode']} for name, entry in files.items()}
    if (saved.get('tool') != 'chaoxing-linux-scrcpy-v1' or saved.get('version') != VERSION
            or saved.get('archive_sha256') != archive_sha or saved.get('files') != expected_files):
        raise ValueError('现有安装记录与固定版本不符，拒绝覆盖。')
    allowed_dirs = set(directories)
    for relative in files:
        allowed_dirs.update(str(parent) for parent in PurePosixPath(relative).parents if str(parent) != '.')
    for relative in ['', *allowed_dirs]:
        directory = target / relative
        if (not directory.is_dir() or directory.is_symlink()
                or stat.S_IMODE(directory.stat().st_mode) != 0o755):
            raise ValueError(f'现有安装目录已修改，拒绝覆盖：{directory}')
    for path in target.rglob('*'):
        relative = str(path.relative_to(target))
        if path.is_symlink():
            raise ValueError(f'现有安装中出现符号链接，拒绝覆盖：{relative}')
        if relative == RECORD:
            continue
        if path.is_dir() and relative in allowed_dirs:
            continue
        if relative not in files or not path.is_file():
            raise ValueError(f'现有安装包含陌生条目，拒绝覆盖：{relative}')
    for name, entry in files.items():
        path = target / name
        if (not path.is_file() or path.is_symlink() or digest(path.read_bytes()) != entry['sha256']
                or stat.S_IMODE(path.stat().st_mode) != entry['mode']):
            raise ValueError(f'现有安装文件已修改，拒绝覆盖：{name}')


def install(archive, target=None, expected_sha=SHA256):
    if os.geteuid() == 0:
        raise ValueError('请以普通用户运行，无需 sudo。')
    if platform.system() != 'Linux' or platform.machine().lower() not in ('x86_64', 'amd64'):
        raise ValueError('该官方静态包仅适用于 Linux x86_64。')
    target = safe_destination(target or default_target())
    files, directories = inspect_archive(archive, expected_sha)
    if target.exists():
        verify_existing(target, files, directories, expected_sha)
        return {'already_installed': True, 'scrcpy': str(target / 'scrcpy'), 'version': VERSION}
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_destination(target)
    lock_path = target.parent / '.scrcpy-v5.0.1.install.lock'
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            verify_existing(target, files, directories, expected_sha)
            return {'already_installed': True, 'scrcpy': str(target / 'scrcpy'), 'version': VERSION}
        stage = Path(tempfile.mkdtemp(prefix='.scrcpy-v5.0.1-stage-', dir=target.parent))
        try:
            for relative in sorted(directories):
                (stage / relative).mkdir(parents=True, exist_ok=True)
            for relative, entry in files.items():
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open('xb') as output:
                    output.write(entry['data'])
                    output.flush()
                    os.fsync(output.fileno())
                destination.chmod(entry['mode'])
            for directory in [stage, *(p for p in stage.rglob('*') if p.is_dir())]:
                directory.chmod(0o755)
            manifest = {'tool': 'chaoxing-linux-scrcpy-v1', 'version': VERSION, 'archive_sha256': expected_sha,
                        'source': URL, 'files': {name: {'sha256': entry['sha256'], 'mode': entry['mode']}
                                               for name, entry in files.items()}}
            (stage / RECORD).write_text(json.dumps(manifest, indent=2), encoding='utf-8')
            (stage / RECORD).chmod(0o644)
            if target.exists():
                raise ValueError('目标目录在安装期间出现，拒绝覆盖。')
            stage.rename(target)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {'installed': True, 'scrcpy': str(target / 'scrcpy'), 'version': VERSION}


def validate_download_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.hostname not in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')):
        raise ValueError('下载必须来自官方 GitHub HTTPS 发布服务器。')


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(cache):
    cache = Path(cache)
    if cache.is_file():
        try:
            inspect_archive(cache)
            return cache
        except (ValueError, tarfile.TarError, OSError):
            raise ValueError(f'已有缓存未通过校验，请先人工核对：{cache}')
    cache.parent.mkdir(parents=True, exist_ok=True)
    validate_download_url(URL)
    opener = urllib.request.build_opener(OfficialRedirect())
    with tempfile.TemporaryDirectory(prefix='.scrcpy-download-', dir=cache.parent) as temporary:
        staged = Path(temporary) / ARCHIVE_NAME
        with opener.open(URL, timeout=30) as response, staged.open('xb') as output:
            validate_download_url(response.geturl())
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > MAX_ARCHIVE:
                    raise ValueError('官方归档下载超过大小上限。')
                output.write(block)
        inspect_archive(staged)
        if cache.exists():
            raise ValueError('下载期间出现已有缓存，拒绝覆盖。')
        staged.rename(cache)
    return cache


def main(argv=None):
    parser = argparse.ArgumentParser(description='当前用户安装官方 scrcpy 5.0.1 静态包；默认仅预览，无需 sudo。')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--plan', action='store_true', help='仅显示计划，不下载、不安装（默认）')
    action.add_argument('--apply', action='store_true', help='下载、校验并安装固定版本')
    parser.add_argument('--archive', type=Path, help='复用 SHA256 匹配的本地官方归档')
    args = parser.parse_args(argv)
    try:
        target = default_target()
        if not args.apply:
            result = {'mode': 'plan', 'version': VERSION, 'url': URL, 'sha256': SHA256,
                      'target': str(target), 'scrcpy': str(target / 'scrcpy'),
                      'requires': ['Linux x86_64', 'adb'], 'requires_sudo': False}
            if args.archive:
                files, directories = inspect_archive(args.archive)
                result['files'] = {name: {'sha256': entry['sha256'], 'bytes': len(entry['data']),
                                          'mode': oct(entry['mode'])} for name, entry in files.items()}
                if target.exists():
                    verify_existing(target, files, directories)
                    result['already_installed'] = True
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if os.geteuid() == 0:
            raise ValueError('请以普通用户运行，无需 sudo。')
        archive = args.archive or download(Path.home() / '.cache/chaoxing-linux' / ARCHIVE_NAME)
        print(json.dumps(install(archive, target), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f'手机工具安装未完成：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
