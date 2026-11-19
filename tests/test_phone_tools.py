"""Offline fixtures for the pinned, ordinary-user scrcpy installer."""
import importlib.util
import io
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install-phone-tools.py'
SPEC = importlib.util.spec_from_file_location('phone_tools', SCRIPT)
phone = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(phone)


class PhoneToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='chaoxing-phone-tools-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / 'fixture.tar.gz'
        self.target = self.root / 'tools/scrcpy-v5.0.1'

    def fixture(self, extra=(), omit_scrcpy=False):
        with tarfile.open(self.archive, 'w:gz') as packed:
            for name, data, mode in [('scrcpy', b'fake ELF executable', 0o4755),
                                     ('scrcpy-server', b'fake Android server', 0o666),
                                     ('LICENSE', b'license fixture', 0o644)]:
                if omit_scrcpy and name == 'scrcpy':
                    continue
                member = tarfile.TarInfo(phone.ARCHIVE_ROOT + '/' + name)
                member.mode = mode
                member.size = len(data)
                packed.addfile(member, io.BytesIO(data))
            for member, data in extra:
                packed.addfile(member, io.BytesIO(data) if data is not None else None)
        return phone.digest(self.archive.read_bytes())

    def test_default_plan_writes_nothing(self):
        home = self.root / 'home'
        home.mkdir()
        result = subprocess.run(['/usr/bin/python3', str(SCRIPT), '--plan'],
                                check=True, text=True, capture_output=True,
                                env={'HOME': str(home), 'PATH': '/usr/bin:/bin'})
        self.assertIn(phone.SHA256, result.stdout)
        self.assertFalse(list(home.iterdir()))

    def test_pinned_hash_and_required_members(self):
        self.fixture()
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            phone.inspect_archive(self.archive)
        expected = self.fixture(omit_scrcpy=True)
        with self.assertRaisesRegex(ValueError, 'scrcpy'):
            phone.inspect_archive(self.archive, expected)

    def test_safe_tar_rejects_traversal_links_special_and_duplicate_paths(self):
        cases = [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                 (phone.ARCHIVE_ROOT + '/../../escape', tarfile.REGTYPE),
                 (phone.ARCHIVE_ROOT + '/linked', tarfile.SYMTYPE),
                 (phone.ARCHIVE_ROOT + '/hard', tarfile.LNKTYPE),
                 (phone.ARCHIVE_ROOT + '/device', tarfile.CHRTYPE),
                 (phone.ARCHIVE_ROOT + '/pipe', tarfile.FIFOTYPE),
                 (phone.ARCHIVE_ROOT + '/scrcpy', tarfile.REGTYPE)]
        for name, kind in cases:
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/etc/passwd'
                member.size = 0
                expected = self.fixture(extra=[(member, b'' if kind == tarfile.REGTYPE else None)])
                with self.assertRaises(ValueError):
                    phone.inspect_archive(self.archive, expected)
        self.assertFalse((self.root / 'escape').exists())

    @mock.patch.object(phone.platform, 'system', return_value='Linux')
    @mock.patch.object(phone.platform, 'machine', return_value='x86_64')
    @mock.patch.object(phone.os, 'geteuid', return_value=1000)
    def test_install_permissions_and_idempotence(self, *_):
        expected = self.fixture()
        result = phone.install(self.archive, self.target, expected)
        self.assertTrue(result['installed'])
        self.assertEqual(result['scrcpy'], str(self.target / 'scrcpy'))
        self.assertEqual(stat.S_IMODE((self.target / 'scrcpy').stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.target / 'scrcpy-server').stat().st_mode), 0o644)
        self.assertTrue(phone.install(self.archive, self.target, expected)['already_installed'])
        (self.target / 'scrcpy').write_bytes(b'user changed')
        with self.assertRaises(ValueError):
            phone.install(self.archive, self.target, expected)
        self.assertEqual((self.target / 'scrcpy').read_bytes(), b'user changed')

    @mock.patch.object(phone.platform, 'system', return_value='Linux')
    @mock.patch.object(phone.platform, 'machine', return_value='x86_64')
    @mock.patch.object(phone.os, 'geteuid', return_value=1000)
    def test_unknown_destination_and_symlink_are_never_overwritten(self, *_):
        expected = self.fixture()
        self.target.mkdir(parents=True)
        marker = self.target / 'personal.txt'
        marker.write_text('preserve me')
        with self.assertRaises(ValueError):
            phone.install(self.archive, self.target, expected)
        self.assertEqual(marker.read_text(), 'preserve me')
        linked = self.root / 'linked'
        linked.symlink_to(self.target)
        with self.assertRaises(ValueError):
            phone.install(self.archive, linked, expected)

    @mock.patch.object(phone.platform, 'system', return_value='Linux')
    @mock.patch.object(phone.platform, 'machine', return_value='x86_64')
    @mock.patch.object(phone.os, 'geteuid', return_value=1000)
    def test_commit_failure_cleans_staging_without_partial_install(self, *_):
        expected = self.fixture()
        with mock.patch.object(Path, 'rename', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                phone.install(self.archive, self.target, expected)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.target.parent.glob('.scrcpy-v5.0.1-stage-*')))

    def test_external_or_insecure_download_redirect_is_rejected(self):
        for url in ('http://github.com/file', 'https://github.com.evil.test/file',
                    'https://user:pass@github.com/file', 'https://example.com/file'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                phone.validate_download_url(url)
        phone.validate_download_url(phone.URL)
        phone.validate_download_url('https://release-assets.githubusercontent.com/github-production-release-asset/file')


if __name__ == '__main__':
    unittest.main()
