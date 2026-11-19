"""Offline packaging checks: never install packages or start Android."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def run_command(self, *args, **kwargs):
        return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)

    def make_fixture(self, target):
        for directory in ('scripts', 'packaging'):
            shutil.copytree(PROJECT / directory, target / directory)
        package = target / 'src/chaoxing_linux'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('__version__ = "1.2.3"\n', encoding='utf-8')
        (package / '__main__.py').write_text(
            'import json, sys\nprint(json.dumps(sys.argv[1:]))\n', encoding='utf-8')
        (target / 'assets').mkdir()
        (target / 'assets/icon.svg').write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"/>', encoding='utf-8')
        # Downloads and reverse-engineering artifacts must not leak into the deb.
        (package / 'untrusted.apk').write_bytes(b'not an apk')
        (target / 'analysis').mkdir()
        (target / 'analysis/original.dex').write_bytes(b'not a dex')
        return package

    def test_shell_syntax_and_host_help_without_privilege(self):
        for script in sorted((PROJECT / 'scripts').glob('*.sh')):
            self.run_command('bash', '-n', str(script))
        output = self.run_command('bash', str(PROJECT / 'scripts/install-host.sh'), '--help').stdout
        self.assertIn('noble', output)
        host = (PROJECT / 'scripts/install-host.sh').read_text(encoding='utf-8')
        self.assertIn('https://repo.waydro.id/waydroid.gpg', host)
        self.assertIn('7CE0331F71E0A238BB1002D70E406D181DCEE19C', host)
        self.assertNotIn('init -f', host)
        self.assertNotRegex(host, r'curl[^\n]*\|\s*(?:sudo\s+)?(?:ba)?sh')

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb unavailable')
    def test_deb_contents_dependencies_and_launcher(self):
        with tempfile.TemporaryDirectory(prefix='chaoxing-package-') as temporary:
            root = Path(temporary) / '项目 with spaces'
            root.mkdir()
            self.make_fixture(root)
            self.run_command('bash', str(root / 'scripts/build-deb.sh'))
            deb = root / 'dist/chaoxing-linux_1.2.3_all.deb'
            self.assertTrue(deb.is_file())
            fields = self.run_command('dpkg-deb', '-f', str(deb)).stdout
            self.assertIn('Architecture: all', fields)
            self.assertIn('Version: 1.2.3', fields)
            self.assertIn('Depends: python3, python3-gi, gir1.2-gtk-3.0, python3-dbus, python3-cryptography, python3-gi-cairo', fields)
            archive = subprocess.run(['dpkg-deb', '--fsys-tarfile', str(deb)],
                                     check=True, capture_output=True).stdout
            tar_path = root / 'contents.tar'
            tar_path.write_bytes(archive)
            with tarfile.open(tar_path) as packed:
                for member in packed:
                    self.assertEqual((member.uid, member.gid), (0, 0))
                    self.assertNotIn('analysis', member.name)
                    self.assertFalse(member.name.endswith(('.apk', '.dex', '.pyc')))
            extracted = root / 'extracted'
            self.run_command('dpkg-deb', '-x', str(deb), str(extracted))
            wrapper = extracted / 'usr/bin/chaoxing-linux'
            self.assertTrue(os.access(wrapper, os.X_OK))
            self.assertTrue(os.access(extracted / 'usr/lib/chaoxing-linux/install-host.sh', os.X_OK))
            phone_installer = extracted / 'usr/lib/chaoxing-linux/install-phone-tools.py'
            self.assertTrue(os.access(phone_installer, os.X_OK))
            self.run_command('/usr/bin/python3', str(phone_installer), '--help')
            study_installer = extracted / 'usr/lib/chaoxing-linux/install-study-backend.py'
            self.assertTrue(os.access(study_installer, os.X_OK))
            self.assertTrue(study_installer.with_name('study-backend.lock').is_file())
            self.run_command('/usr/bin/python3', str(study_installer), '--help')
            env = os.environ.copy()
            env['PYTHONPATH'] = str(extracted / 'usr/lib/chaoxing-linux')
            actual = self.run_command(str(wrapper), '--doctor', 'argument with spaces', env=env)
            self.assertEqual(json.loads(actual.stdout), ['--doctor', 'argument with spaces'])
            desktop = extracted / 'usr/share/applications/chaoxing-linux.desktop'
            self.assertIn('Icon=chaoxing-linux\n', desktop.read_text(encoding='utf-8'))
            if shutil.which('desktop-file-validate'):
                self.run_command('desktop-file-validate', str(desktop))

    @unittest.skipIf(os.geteuid() == 0, 'local installer deliberately rejects root')
    def test_local_install_quoted_paths_and_idempotence(self):
        with tempfile.TemporaryDirectory(prefix='chaoxing-local-') as temporary:
            root = Path(temporary) / 'source'
            root.mkdir()
            self.make_fixture(root)
            home = Path(temporary) / "home with ' quote"
            home.mkdir()
            env = os.environ.copy()
            env['HOME'] = str(home)
            env['XDG_DATA_HOME'] = str(home / 'data')
            for _ in range(2):
                self.run_command('bash', str(root / 'scripts/install-local.sh'), env=env)
            wrapper = home / '.local/bin/chaoxing-linux'
            phone_installer = home / 'data/chaoxing-linux/install-phone-tools.py'
            self.assertTrue(os.access(phone_installer, os.X_OK))
            self.run_command('/usr/bin/python3', str(phone_installer), '--help')
            actual = self.run_command(str(wrapper), '--doctor', '--json', env=env)
            self.assertEqual(json.loads(actual.stdout), ['--doctor', '--json'])
            desktop = home / 'data/applications/chaoxing-linux.desktop'
            self.assertIn(str(wrapper), desktop.read_text(encoding='utf-8'))
            if shutil.which('desktop-file-validate'):
                self.run_command('desktop-file-validate', str(desktop))


if __name__ == '__main__':
    unittest.main()
