"""Offline ARM installer transactions, restricted to temporary synthetic trees."""
import configparser
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install-arm-bridge.py'
SPEC = importlib.util.spec_from_file_location('arm_bridge', SCRIPT)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class FakeWaydroid:
    def __init__(self, base):
        self.base = base
        self.session = 'STOPPED'
        self.fail_upgrade = False
        self.calls = []
        self.image_props = 'ro.build.version.release=13\nro.build.version.sdk=33\n'

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ['/usr/bin/waydroid', 'status']:
            return subprocess.CompletedProcess(args, 0, 'Session:\t' + self.session + '\n', '')
        if args[:3] == ['/usr/bin/waydroid', 'upgrade', '-o']:
            (self.base / 'waydroid_base.prop').write_text('generated bridge properties\n')
            return subprocess.CompletedProcess(args, 1 if self.fail_upgrade else 0, '', 'simulated failure' if self.fail_upgrade else '')
        if args[:2] == ['/usr/sbin/debugfs', '-R']:
            self.assert_read_only(args)
            return subprocess.CompletedProcess(args, 0, self.image_props, 'debugfs read-only')
        raise AssertionError(f'Unexpected command: {args}')

    @staticmethod
    def assert_read_only(args):
        assert '-w' not in args
        assert args[2] in ('cat /system/build.prop', 'cat /build.prop')


class ArmBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='chaoxing-arm-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / 'waydroid'
        self.base.mkdir()
        self.original_config = b'[waydroid]\narch=x86_64\nmount_overlays=True\n[properties]\ncustom.keep=yes\n'
        (self.base / 'waydroid.cfg').write_bytes(self.original_config)
        props = self.base / 'rootfs/system/build.prop'
        props.parent.mkdir(parents=True)
        props.write_text('ro.build.version.release=13\nro.build.version.sdk=33\n')
        self.runner = FakeWaydroid(self.base)
        self.installer = bridge.Installer(self.base, self.runner)
        self.payload = {name: b'fixture' for name in bridge.EXACT_FILES}
        self.payload['etc/init/ndk_translation.rc'] = bridge.INIT_RC
        self.payload['lib/libndk_translation.so'] = b'x86 ELF fixture'
        self.payload['lib64/libndk_translation.so'] = b'x86_64 ELF fixture'

    def archive(self, extra=(), symlink=None):
        path = Path(self.temporary.name) / 'fixture.zip'
        with zipfile.ZipFile(path, 'w') as zipped:
            for name, content in self.payload.items():
                zipped.writestr(bridge.ARCHIVE_ROOT + 'prebuilts/' + name, content)
            for name, content in extra:
                zipped.writestr(name, content)
            if symlink:
                item = zipfile.ZipInfo(bridge.ARCHIVE_ROOT + 'prebuilts/' + symlink)
                item.create_system = 3
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
                zipped.writestr(item, '/etc/passwd')
        return path

    def test_hash_pin_and_restricted_zip_paths(self):
        archive = self.archive()
        with self.assertRaises(ValueError):
            bridge.read_payload(archive)  # Synthetic ZIP does not match the pin.
        self.assertEqual(bridge.read_payload(archive, bridge.sha(archive.read_bytes())), self.payload)
        for path in ('../escape', bridge.ARCHIVE_ROOT + 'prebuilts/etc/security/cacerts/x.0',
                     bridge.ARCHIVE_ROOT + 'prebuilts/bin/su',
                     bridge.ARCHIVE_ROOT + 'prebuilts/etc/resetprop',
                     bridge.ARCHIVE_ROOT + 'prebuilts/etc/init/magisk.rc'):
            with self.subTest(path=path):
                archive = self.archive(extra=[(path, b'bad')])
                with self.assertRaises(ValueError):
                    bridge.read_payload(archive, bridge.sha(archive.read_bytes()))
        archive = self.archive(symlink='lib/arm/link.so')
        with self.assertRaises(ValueError):
            bridge.read_payload(archive, bridge.sha(archive.read_bytes()))

    def test_plan_is_read_only(self):
        before = sorted(str(p.relative_to(self.base)) for p in self.base.rglob('*'))
        plan = self.installer.plan(self.payload)
        self.assertEqual(plan['android'], '13')
        self.assertEqual(len(plan['files']), len(self.payload))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(before, sorted(str(p.relative_to(self.base)) for p in self.base.rglob('*')))
        self.assertEqual((self.base / 'waydroid.cfg').read_bytes(), self.original_config)

    def test_apply_idempotence_permissions_and_rollback(self):
        generated = self.base / 'waydroid_base.prop'
        generated.write_bytes(b'original base properties\n')
        result = self.installer.apply(self.payload)
        self.assertTrue(result['installed'])
        cfg = configparser.ConfigParser()
        cfg.read(self.base / 'waydroid.cfg')
        self.assertEqual(cfg['properties']['custom.keep'], 'yes')
        for key, value in bridge.PROPERTIES.items():
            self.assertEqual(cfg['properties'][key], value)
        for relative in self.payload:
            path = self.base / 'overlay/system' / relative
            self.assertEqual(path.read_bytes(), self.payload[relative])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755 if relative.startswith('bin/') else 0o644)
        self.assertTrue(self.installer.apply(self.payload)['already_installed'])
        self.assertEqual(sum(call[1:3] == ['upgrade', '-o'] for call in self.runner.calls), 1)
        journal = json.loads((self.installer.state / 'manifest.json').read_text())
        self.assertEqual(journal['status'], 'installed')
        self.installer.rollback()
        self.assertEqual((self.base / 'waydroid.cfg').read_bytes(), self.original_config)
        self.assertEqual(generated.read_bytes(), b'original base properties\n')
        self.assertFalse((self.base / 'overlay').exists())
        self.assertFalse(self.installer.state.exists())

    def test_offline_refresh_failure_restores_transaction(self):
        self.runner.fail_upgrade = True
        with self.assertRaises(RuntimeError):
            self.installer.apply(self.payload)
        self.assertEqual((self.base / 'waydroid.cfg').read_bytes(), self.original_config)
        self.assertFalse((self.base / 'waydroid_base.prop').exists())
        self.assertFalse((self.base / 'overlay').exists())
        self.assertFalse(self.installer.state.exists())

    def test_user_modification_refuses_rollback_before_any_change(self):
        self.installer.apply(self.payload)
        path = self.base / 'overlay/system/lib64/libndk_translation.so'
        path.write_bytes(b'user changed')
        config = (self.base / 'waydroid.cfg').read_bytes()
        with self.assertRaises(ValueError):
            self.installer.rollback()
        self.assertEqual(path.read_bytes(), b'user changed')
        self.assertEqual((self.base / 'waydroid.cfg').read_bytes(), config)
        self.assertTrue(self.installer.state.exists())

    def test_conflicts_disabled_overlays_wrong_android_and_running_session(self):
        self.runner.session = 'RUNNING'
        with self.assertRaises(ValueError):
            self.installer.apply(self.payload)
        self.runner.session = 'STOPPED'
        props = self.base / 'rootfs/system/build.prop'
        props.write_text('ro.build.version.release=11\nro.build.version.sdk=30\n')
        with self.assertRaises(ValueError):
            self.installer.apply(self.payload)
        props.write_text('ro.build.version.release=13\nro.build.version.sdk=33\n')
        (self.base / 'waydroid.cfg').write_bytes(self.original_config.replace(b'True', b'False'))
        with self.assertRaises(ValueError):
            self.installer.apply(self.payload)
        (self.base / 'waydroid.cfg').write_bytes(self.original_config)
        target = self.base / 'overlay/system/lib64/libndk_translation.so'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'existing third party bridge')
        with self.assertRaises(ValueError):
            self.installer.apply(self.payload)
        self.assertEqual(target.read_bytes(), b'existing third party bridge')

    def test_readonly_image_version_without_booting_android(self):
        (self.base / 'rootfs/system/build.prop').unlink()
        image = self.base / 'images/system.img'
        image.parent.mkdir()
        image.write_bytes(b'fake ext4 image; debugfs is mocked')
        plan = self.installer.plan(self.payload)
        self.assertEqual(plan['android'], '13')
        self.assertEqual(self.runner.calls[0][:3], ['/usr/sbin/debugfs', '-R', 'cat /system/build.prop'])
        self.runner.image_props = ''
        with self.assertRaises(ValueError):
            self.installer.plan(self.payload)

    def test_destination_symlink_refused(self):
        (self.base / 'overlay').symlink_to(Path(self.temporary.name))
        with self.assertRaises(ValueError):
            self.installer.apply(self.payload)
        self.assertFalse(self.installer.state.exists())


if __name__ == '__main__':
    unittest.main()
