#!/usr/bin/python3
"""Narrow, reversible Android 13 NDK overlay installer; no root-enabling hooks."""
from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

URL = 'https://github.com/supremegamers/vendor_google_proprietary_ndk_translation-prebuilt/archive/68734c52556d3d7a6db34c603dd9276915c29f2f.zip'
ARCHIVE_SHA256 = 'a142d1586c9eafb5edf62277110f2128bc03066179a6783bcaa33ee322e1cbd0'
ARCHIVE_ROOT = 'vendor_google_proprietary_ndk_translation-prebuilt-68734c52556d3d7a6db34c603dd9276915c29f2f/'
PROPERTIES = {
    'ro.product.cpu.abilist': 'x86_64,x86,arm64-v8a,armeabi-v7a,armeabi',
    'ro.product.cpu.abilist32': 'x86,armeabi-v7a,armeabi',
    'ro.product.cpu.abilist64': 'x86_64,arm64-v8a',
    'ro.dalvik.vm.native.bridge': 'libndk_translation.so',
    'ro.enable.native.bridge.exec': '1',
    'ro.vendor.enable.native.bridge.exec': '1',
    'ro.vendor.enable.native.bridge.exec64': '1',
    'ro.ndk_translation.version': '0.2.3',
    'ro.dalvik.vm.isa.arm': 'x86',
    'ro.dalvik.vm.isa.arm64': 'x86_64',
}
# Minimal upstream binfmt registration only. No service, shell, resetprop, or su.
INIT_RC = b'''# Enable native bridge for target executables
on early-init && property:ro.enable.native.bridge.exec=1
    mount binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc

on property:ro.enable.native.bridge.exec=1 && property:ro.dalvik.vm.isa.arm=x86
    copy /system/etc/binfmt_misc/arm_exe /proc/sys/fs/binfmt_misc/register
    copy /system/etc/binfmt_misc/arm_dyn /proc/sys/fs/binfmt_misc/register

on property:ro.enable.native.bridge.exec=1 && property:ro.dalvik.vm.isa.arm64=x86_64
    copy /system/etc/binfmt_misc/arm64_exe /proc/sys/fs/binfmt_misc/register
    copy /system/etc/binfmt_misc/arm64_dyn /proc/sys/fs/binfmt_misc/register
'''
EXACT_FILES = {
    'bin/arm/app_process', 'bin/arm/linker', 'bin/arm64/app_process64', 'bin/arm64/linker64',
    'bin/ndk_translation_program_runner_binfmt_misc',
    'bin/ndk_translation_program_runner_binfmt_misc_arm64',
    'etc/binfmt_misc/arm_exe', 'etc/binfmt_misc/arm_dyn',
    'etc/binfmt_misc/arm64_exe', 'etc/binfmt_misc/arm64_dyn',
    'etc/cpuinfo.arm.txt', 'etc/cpuinfo.arm64.txt',
    'etc/ld.config.arm.txt', 'etc/ld.config.arm64.txt', 'etc/init/ndk_translation.rc',
}
GENERATED = ('waydroid_base.prop', 'waydroid.prop', 'session.cfg', 'config_nodes',
             'lxc/waydroid/config', 'lxc/waydroid/config_nodes',
             'lxc/waydroid/config_session', 'lxc/waydroid/waydroid.seccomp')


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def allowed(relative: str) -> bool:
    if relative in EXACT_FILES:
        return True
    return bool(re.fullmatch(r'(?:lib/arm|lib64/arm64)/(?:[A-Za-z0-9_.@+\-]+/)*[A-Za-z0-9_.@+\-]+\.so', relative)
                or re.fullmatch(r'(?:lib|lib64)/libndk[A-Za-z0-9_.+\-]*\.so', relative))


def read_payload(archive: Path, expected_sha: str = ARCHIVE_SHA256) -> dict[str, bytes]:
    if not archive.is_file() or archive.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('ARM 归档不是普通文件或超过大小上限。')
    with archive.open('rb') as source:
        raw = source.read(128 * 1024 * 1024 + 1)
    if len(raw) > 128 * 1024 * 1024 or sha(raw) != expected_sha:
        raise ValueError('ARM 归档 SHA256 不匹配或过大；拒绝使用。')
    payload = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zipped:
        seen = set()
        for item in zipped.infolist():
            name = item.filename
            parts = name.rstrip('/').split('/')
            if (not name or '\\' in name or '\x00' in item.orig_filename
                    or any(part in ('', '.', '..') for part in parts)
                    or PurePosixPath(name).is_absolute() or ':' in parts[0]
                    or stat.S_ISLNK(item.external_attr >> 16) or name in seen):
                raise ValueError('ARM 归档包含重复、越界或符号链接路径。')
            seen.add(name)
            if not name.startswith(ARCHIVE_ROOT):
                raise ValueError('ARM 归档顶层目录不符合固定版本。')
            if item.is_dir():
                continue
            if not name.startswith(ARCHIVE_ROOT + 'prebuilts/'):
                continue
            relative = name[len(ARCHIVE_ROOT + 'prebuilts/'):]
            if not allowed(relative):
                raise ValueError(f'ARM 归档包含未获准文件：{relative}')
            total += item.file_size
            if item.file_size > 16 * 1024 * 1024 or total > 128 * 1024 * 1024:
                raise ValueError('ARM 归档展开超过安全上限。')
            content = zipped.read(item)
            if relative == 'etc/init/ndk_translation.rc':
                if content.strip() != INIT_RC.strip():
                    raise ValueError('ARM 归档 initrc 超出已审查的最小 binfmt 配置。')
                content = INIT_RC
            payload[relative] = content
    required = EXACT_FILES | {'lib/libndk_translation.so', 'lib64/libndk_translation.so'}
    if not required.issubset(payload):
        raise ValueError('ARM 归档缺少必要的翻译库、执行器或 binfmt 配置。')
    return payload


def safe_path(base: Path, relative: str) -> Path:
    if PurePosixPath(relative).is_absolute() or any(p in ('', '.', '..') for p in relative.split('/')):
        raise ValueError('目标路径不安全。')
    target = base / relative
    # Check every existing ancestor, including base; never follow a destination symlink.
    for path in [target, *target.parents]:
        if path.is_symlink():
            raise ValueError(f'目标路径包含符号链接：{path}')
        if path != target and path.exists() and not path.is_dir():
            raise ValueError(f'目标父路径不是目录：{path}')
    return target


def atomic_write(path: Path, content: bytes, mode: int):
    descriptor, temporary = tempfile.mkstemp(prefix='.chaoxing-arm-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def config_bytes(config) -> bytes:
    out = io.StringIO()
    config.write(out)
    return out.getvalue().encode()


def load_config(base: Path):
    path = safe_path(base, 'waydroid.cfg')
    config = configparser.ConfigParser(interpolation=None)
    with path.open(encoding='utf-8') as stream:
        config.read_file(stream)
    if config.get('waydroid', 'mount_overlays', fallback='') != 'True':
        raise ValueError('需要 waydroid.cfg 的 [waydroid] mount_overlays=True；不会自动改变镜像挂载方式。')
    if config.get('waydroid', 'arch', fallback='') not in ('x86_64', 'amd64'):
        raise ValueError('仅支持 x86_64 Android 13 镜像。')
    return config


def read_build_properties(base: Path, run=subprocess.run) -> dict[str, str]:
    path = base / 'rootfs/system/build.prop'
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        # Android need not be booted just to inspect its ext4 image. debugfs is
        # read-only without -w; neither its command nor image path is user input.
        image = base / 'images/system.img'
        if not image.is_file():
            raise ValueError('无法读取 system/build.prop，系统镜像尚未就绪；不能确认 Android 13。')
        text = ''
        for command in ('cat /system/build.prop', 'cat /build.prop'):
            result = run(['/usr/sbin/debugfs', '-R', command, str(image)],
                         text=True, capture_output=True, timeout=30)
            if result.returncode == 0 and re.search(r'^ro\.(?:system\.)?build\.version\.release=',
                                                  result.stdout, flags=re.MULTILINE):
                text = result.stdout
                break
        if not text:
            raise ValueError('无法通过只读 debugfs 从 system.img 获取 system/build.prop；拒绝猜测 Android 版本。')
    props = {}
    for line in text.splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            props[key.strip()] = value.strip()
    release = props.get('ro.build.version.release', props.get('ro.system.build.version.release', ''))
    sdk = props.get('ro.build.version.sdk', props.get('ro.system.build.version.sdk', ''))
    if release != '13' or sdk != '33':
        raise ValueError(f'此翻译归档仅适用 Android 13 / SDK 33；build.prop 报告 release={release!r}, sdk={sdk!r}。')
    return props


def stopped(run=subprocess.run):
    result = run(['/usr/bin/waydroid', 'status'], text=True, capture_output=True, timeout=30)
    text = result.stdout + result.stderr
    # Never stop anyone's session. Unknown/error states fail closed.
    if result.returncode or not re.search(r'^Session:\s*STOPPED\s*$', text, flags=re.MULTILINE):
        raise ValueError('Waydroid 会话未明确报告 STOPPED。请本人关闭 Android 会话后重试；本工具不会停止其他用户的会话。')


class Installer:
    def __init__(self, base=Path('/var/lib/waydroid'), run=subprocess.run):
        self.base = Path(base)
        self.state = safe_path(self.base, 'chaoxing-linux-arm-bridge')
        self.run = run

    def manifest(self):
        path = safe_path(self.base, 'chaoxing-linux-arm-bridge/manifest.json')
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        if value.get('tool') != 'chaoxing-linux-arm-bridge-v1' or value.get('archive_sha256') != ARCHIVE_SHA256:
            raise ValueError('现有 ARM 安装记录不属于本工具/固定版本。')
        return value

    def check(self, payload):
        cfg = load_config(self.base)
        build = read_build_properties(self.base, self.run)
        previous = self.manifest()
        if previous and previous.get('status') != 'installed':
            raise ValueError('检测到未完成的 ARM 安装事务；请先使用 --rollback 恢复，不能把它当作已安装。')
        for source in (dict(cfg.items('properties')) if cfg.has_section('properties') else {}, build):
            bridge = source.get('ro.dalvik.vm.native.bridge', '')
            if bridge not in ('', '0') and (not previous or bridge != 'libndk_translation.so'):
                raise ValueError(f'检测到已有 native bridge：{bridge}，拒绝覆盖。')
        # Detect common competing bridges even when their property was not enabled.
        for tree in ('overlay/system', 'rootfs/system'):
            root = self.base / tree
            for pattern in ('lib*/libhoudini*', 'bin/houdini*', 'lib*/libndk_translation*',
                            'etc/init/*houdini*', 'etc/init/resetprop*', 'etc/resetprop*'):
                for found in root.glob(pattern):
                    relative = str(found.relative_to(self.base))
                    owned = relative if tree == 'overlay/system' else relative.replace('rootfs/system/', 'overlay/system/', 1)
                    if (not previous or owned not in previous['files'] or not found.is_file()
                            or sha(found.read_bytes()) != previous['files'][owned]['after_sha256']):
                        raise ValueError(f'检测到非本工具管理的 native bridge 文件：{found}')
        for relative in payload:
            target = safe_path(self.base, 'overlay/system/' + relative)
            if target.exists() and (not previous or 'overlay/system/' + relative not in previous['files']):
                raise ValueError(f'目标文件已存在且不属于本工具：{target}')
        if previous:
            self.verify_unchanged(previous)
        return cfg, previous

    def plan(self, payload):
        config, previous = self.check(payload)
        return {'mode': 'plan', 'android': '13', 'archive_sha256': ARCHIVE_SHA256,
                'already_installed': bool(previous),
                'files': [{'path': str(self.base / 'overlay/system' / name),
                           'sha256': sha(data), 'bytes': len(data),
                           'mode': '0755' if name.startswith('bin/') else '0644'}
                          for name, data in sorted(payload.items())],
                'properties': PROPERTIES,
                'config': str(self.base / 'waydroid.cfg'),
                'backup': str(self.state), 'offline_command': ['/usr/bin/waydroid', 'upgrade', '-o'],
                'generated_files_backed_up': list(GENERATED),
                'warning': '第三方 NDK 翻译库；未启用 root、未更改证书，也不保证所有手机功能兼容。'}

    def verify_unchanged(self, manifest):
        for relative, entry in manifest['files'].items():
            path = safe_path(self.base, relative)
            # A process interrupted during copying may leave either side of a
            # journaled atomic write. Accept only those two exact known states.
            if manifest.get('status') == 'preparing':
                current = sha(path.read_bytes()) if path.is_file() else None
                current_mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else None
                if ((current, current_mode) == (entry['before_sha256'], entry['before_mode'])
                        or (current, current_mode) == (entry['after_sha256'], entry['after_mode'])):
                    continue
            if entry['after_sha256'] is None:
                if path.exists():
                    raise ValueError(f'安装后文件已被新增，拒绝盲目还原：{path}')
            elif (not path.is_file() or sha(path.read_bytes()) != entry['after_sha256']
                  or stat.S_IMODE(path.stat().st_mode) != entry['after_mode']):
                raise ValueError(f'安装后文件已被修改，拒绝盲目还原：{path}')

    def ensure_parents(self, path, created):
        missing = []
        current = path.parent
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o755)
            os.chmod(directory, 0o755)
            created.append(str(directory.relative_to(self.base)))

    def backup(self, relative, manifest):
        target = safe_path(self.base, relative)
        entry = {'before_sha256': None, 'before_mode': None, 'after_sha256': None, 'after_mode': None}
        if target.exists():
            if not target.is_file():
                raise ValueError(f'需要备份的路径不是普通文件：{target}')
            content = target.read_bytes()
            backup = self.state / 'backup' / relative
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(backup, content, 0o600)
            entry.update(before_sha256=sha(content), before_mode=stat.S_IMODE(target.stat().st_mode))
        manifest['files'][relative] = entry

    def record_after(self, manifest):
        for relative, entry in manifest['files'].items():
            path = safe_path(self.base, relative)
            entry['after_sha256'] = sha(path.read_bytes()) if path.is_file() else None
            entry['after_mode'] = stat.S_IMODE(path.stat().st_mode) if path.is_file() else None

    def restore(self, manifest):
        # Verify *everything* before deleting/restoring anything.
        self.verify_unchanged(manifest)
        for relative, entry in manifest['files'].items():
            if entry['before_sha256'] is not None:
                backup = safe_path(self.state / 'backup', relative)
                if not backup.is_file() or sha(backup.read_bytes()) != entry['before_sha256']:
                    raise ValueError(f'备份损坏：{relative}')
        for relative, entry in manifest['files'].items():
            target = safe_path(self.base, relative)
            if entry['before_sha256'] is None:
                if target.exists():
                    target.unlink()
            else:
                self.ensure_parents(target, [])
                atomic_write(target, (self.state / 'backup' / relative).read_bytes(), entry['before_mode'])
        for relative in reversed(manifest['created_directories']):
            path = safe_path(self.base, relative)
            try:
                path.rmdir()  # Preserves user additions in a nonempty directory.
            except OSError:
                pass

    def refresh(self):
        result = self.run(['/usr/bin/waydroid', 'upgrade', '-o'], text=True, capture_output=True, timeout=180)
        if result.returncode:
            raise RuntimeError('Waydroid 离线刷新失败：' + (result.stdout + result.stderr)[-4000:])

    def apply(self, payload):
        stopped(self.run)
        if not payload or any(not allowed(relative) for relative in payload):
            raise ValueError('安装负载包含未获准文件或为空。')
        config, previous = self.check(payload)
        if previous:
            return {'already_installed': True, 'files': len(previous['files'])}
        if self.state.exists():
            raise ValueError(f'存在未完成的事务目录，请先核对/恢复：{self.state}')
        self.state.mkdir(mode=0o700)
        manifest = {'tool': 'chaoxing-linux-arm-bridge-v1', 'archive_sha256': ARCHIVE_SHA256,
                    'files': {}, 'created_directories': [], 'status': 'preparing'}
        manifest_path = self.state / 'manifest.json'
        refresh_started = False
        try:
            for relative in ['waydroid.cfg', *GENERATED, *('overlay/system/' + name for name in sorted(payload))]:
                self.backup(relative, manifest)
            # Record a recovery journal before changing any target file.
            self.record_after(manifest)
            for relative, content in payload.items():
                manifest['files']['overlay/system/' + relative].update(
                    after_sha256=sha(content), after_mode=0o755 if relative.startswith('bin/') else 0o644)
            if not config.has_section('properties'):
                config.add_section('properties')
            for key, value in PROPERTIES.items():
                config.set('properties', key, value)
            new_config = config_bytes(config)
            manifest['files']['waydroid.cfg'].update(after_sha256=sha(new_config),
                                                    after_mode=manifest['files']['waydroid.cfg']['before_mode'])
            atomic_write(manifest_path, json.dumps(manifest, indent=2).encode(), 0o600)
            for relative, content in sorted(payload.items()):
                target = safe_path(self.base, 'overlay/system/' + relative)
                self.ensure_parents(target, manifest['created_directories'])
                atomic_write(target, content, 0o755 if relative.startswith('bin/') else 0o644)
            atomic_write(self.base / 'waydroid.cfg', new_config, manifest['files']['waydroid.cfg']['before_mode'])
            self.record_after(manifest)
            manifest['status'] = 'refreshing'
            atomic_write(manifest_path, json.dumps(manifest, indent=2).encode(), 0o600)
            stopped(self.run)
            refresh_started = True
            self.refresh()
            self.record_after(manifest)
            manifest['status'] = 'installed'
            atomic_write(manifest_path, json.dumps(manifest, indent=2).encode(), 0o600)
        except BaseException:
            # The official command may have rewritten derived files before failing.
            # Overlay hashes remain the values we actually intended to write;
            # later user changes must not become "ours" just because refresh failed.
            if refresh_started:
                for relative in ('waydroid.cfg', *GENERATED):
                    entry = manifest['files'].get(relative)
                    if entry is not None:
                        path = safe_path(self.base, relative)
                        entry['after_sha256'] = sha(path.read_bytes()) if path.is_file() else None
                        entry['after_mode'] = stat.S_IMODE(path.stat().st_mode) if path.is_file() else None
            atomic_write(manifest_path, json.dumps(manifest, indent=2).encode(), 0o600)
            self.restore(manifest)
            shutil.rmtree(self.state)
            raise
        return {'installed': True, 'files': len(payload), 'backup': str(self.state)}

    def rollback(self):
        stopped(self.run)
        manifest = self.manifest()
        if not manifest:
            raise ValueError('没有本工具创建的 ARM 安装记录。')
        self.restore(manifest)
        # Restore backed-up generated config directly; no fresh upgrade that could
        # overwrite later user configuration or introduce unrelated differences.
        shutil.rmtree(self.state)
        return {'rolled_back': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Android 13 ARM 翻译层安装；默认只读计划，不运行社区安装脚本。')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--plan', action='store_true', help='只读预览，不下载、不写入（默认）')
    actions.add_argument('--apply', action='store_true', help='管理员执行已审查的 overlay 安装')
    actions.add_argument('--rollback', action='store_true', help='仅回滚本工具未被后改的文件')
    parser.add_argument('--archive', type=Path, help='复用 SHA256 匹配的本地固定版本归档')
    args = parser.parse_args(argv)
    try:
        if args.apply or args.rollback:
            if os.geteuid() != 0:
                raise ValueError('此操作需要管理员权限，请在终端通过 sudo /usr/bin/python3 运行。')
        installer = Installer()
        if args.rollback:
            print(json.dumps(installer.rollback(), ensure_ascii=False, indent=2))
            return 0
        if args.archive:
            payload = read_payload(args.archive)
        elif not args.apply:
            print(json.dumps({'mode': 'plan', 'download_url': URL, 'archive_sha256': ARCHIVE_SHA256,
                              'target': '/var/lib/waydroid/overlay/system', 'properties': PROPERTIES,
                              'file_patterns': sorted(EXACT_FILES) + ['lib/arm/**/*.so', 'lib64/arm64/**/*.so',
                                                                        'lib/libndk*.so', 'lib64/libndk*.so'],
                              'requirements': ['Android 13 / SDK 33 from system/build.prop', 'mount_overlays=True',
                                               'x86_64', 'Session STOPPED', 'no conflicting native bridge'],
                              'note': '使用 --archive 本地归档 可逐文件预览；--apply 才会下载和写入。'},
                             ensure_ascii=False, indent=2))
            return 0
        else:
            # Download only a pinned ZIP into a temporary directory, never execute it.
            with tempfile.TemporaryDirectory(prefix='chaoxing-ndk-') as temporary:
                archive = Path(temporary) / 'ndk.zip'
                with urllib.request.urlopen(URL, timeout=60) as response, archive.open('wb') as output:
                    if not response.geturl().startswith('https://'):
                        raise ValueError('下载重定向未使用 HTTPS。')
                    total = 0
                    while block := response.read(1024 * 1024):
                        total += len(block)
                        if total > 128 * 1024 * 1024:
                            raise ValueError('下载归档过大。')
                        output.write(block)
                payload = read_payload(archive)
        result = installer.apply(payload) if args.apply else installer.plan(payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, configparser.Error, zipfile.BadZipFile,
            subprocess.SubprocessError) as exc:
        print(f'ARM 支持未更改或未完成：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
