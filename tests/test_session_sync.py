import contextlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from chaoxing_linux.session_sync import SessionCandidate, discover_sessions


class SessionSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / '.mozilla/firefox'

    def profile(self, name='one', rows=(), *, root=None, attributes=True):
        profile = (root or self.root) / name
        profile.mkdir(parents=True, exist_ok=True)
        path = profile / 'cookies.sqlite'
        connection = sqlite3.connect(path)
        schema = 'name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER'
        if attributes:
            schema += ', originAttributes TEXT'
        connection.execute('CREATE TABLE moz_cookies (' + schema + ')')
        for row in rows:
            values = (row[0], row[1], row[2], row[3] if len(row) > 3 else '/',
                      row[4] if len(row) > 4 else int(time.time()) + 3600)
            if attributes:
                values += (row[5] if len(row) > 5 else '',)
            connection.execute('INSERT INTO moz_cookies VALUES (' + ','.join('?' for _ in values) + ')', values)
        connection.commit()
        connection.close()
        return profile

    def ini(self, profiles, *, root=None):
        root = root or self.root
        root.mkdir(parents=True, exist_ok=True)
        text = ''
        for index, (name, default) in enumerate(profiles):
            text += f'[Profile{index}]\nName=fixture\nIsRelative=1\nPath={name}\nDefault={int(default)}\n\n'
        (root / 'profiles.ini').write_text(text)

    def cookies(self, candidate):
        return dict(item.split('=', 1) for item in candidate.cookies.split('; '))

    def test_strict_domains_expiry_and_required_identity(self):
        now = int(time.time())
        self.profile(rows=[('_uid', 'identity', '.chaoxing.com'), ('valid', 'yes', 'mooc1.chaoxing.com'),
                           ('hostonly', 'yes', 'chaoxing.com'), ('session', 'keep', '.chaoxing.com', '/', 0),
                           ('expired', 'no', '.chaoxing.com', '/', now-1),
                           ('boundary', 'no', 'evilchaoxing.com'), ('suffix', 'no', 'chaoxing.com.evil.test'),
                           ('dots', 'no', '..chaoxing.com'), ('unrelated', 'no', 'example.com')])
        self.ini([('one', True)])
        candidates = discover_sessions(home=self.home)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(self.cookies(candidates[0]), {'_uid': 'identity', 'valid': 'yes', 'hostonly': 'yes', 'session': 'keep'})
        no_uid = self.home/'no_uid'
        self.profile(root=no_uid/'.mozilla/firefox', rows=[('session','no-uid','.chaoxing.com')])
        self.assertEqual(discover_sessions(home=no_uid), [])

    def test_profiles_are_never_merged_and_defaults_are_first(self):
        self.profile('one', [('_uid', 'person-one', '.chaoxing.com'), ('only-one', '1', '.chaoxing.com')])
        self.profile('two', [('UID', 'person-two', '.chaoxing.com'), ('only-two', '2', '.chaoxing.com')])
        self.ini([('one', False), ('two', True)])
        candidates = discover_sessions(home=self.home)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(self.cookies(candidates[0])['UID'], 'person-two')
        self.assertNotIn('only-one', self.cookies(candidates[0]))
        self.assertNotIn('only-two', self.cookies(candidates[1]))
        self.assertEqual(candidates, discover_sessions(home=self.home))

    def test_container_private_and_partitioned_origins_are_excluded(self):
        self.profile(rows=[('_uid', 'normal', '.chaoxing.com'),
                           ('_uid', 'container', '.chaoxing.com', '/', 0, '^userContextId=2'),
                           ('private', 'secret', '.chaoxing.com', '/', 0, '^privateBrowsingId=1'),
                           ('partitioned', 'secret', '.chaoxing.com', '/', 0, '^partitionKey=%28https%2Cother.test%29')])
        self.ini([('one', True)])
        candidates = discover_sessions(home=self.home)
        self.assertEqual(self.cookies(candidates[0]), {'_uid': 'normal'})
        isolated = self.home/'isolated'
        self.profile(root=isolated/'.mozilla/firefox', rows=[('_uid','hidden','.chaoxing.com','/',0,'^userContextId=1')])
        self.assertEqual(discover_sessions(home=isolated), [])

    def test_same_cookie_name_prefers_parent_domain_then_root_path(self):
        self.profile(rows=[('_uid','subaccount','.mooc1.chaoxing.com'), ('_uid','parent','.chaoxing.com'),
                           ('auth','sub','.mooc1.chaoxing.com'), ('auth','child-path','.chaoxing.com','/login'),
                           ('auth','root-host','chaoxing.com'), ('auth','root-domain','.chaoxing.com')])
        self.ini([('one', True)])
        values = self.cookies(discover_sessions(home=self.home)[0])
        self.assertEqual(values['_uid'], 'parent')
        self.assertEqual(values['auth'], 'root-domain')

    def test_standard_snap_flatpak_backend_have_stable_order(self):
        locations = [self.root, self.home/'snap/firefox/common/.mozilla/firefox',
                     self.home/'.var/app/org.mozilla.firefox/.mozilla/firefox']
        for index, root in enumerate(locations):
            self.profile(root=root, rows=[('_uid',str(index),'.chaoxing.com')])
            self.ini([('one',True)], root=root)
        backend = self.home/'backend'
        backend.mkdir()
        (backend/'cookies.txt').write_text('UID=backend; other=a=b;')
        candidates = discover_sessions(home=self.home, backend_root=backend)
        self.assertEqual([c.source for c in candidates], ['Firefox 标准 · 配置 1','Firefox Snap · 配置 1',
                                                        'Firefox Flatpak · 配置 1','原命令行项目'])
        self.assertEqual(self.cookies(candidates[-1]), {'UID':'backend','other':'a=b'})

    def test_install_default_absolute_profiles_and_duplicate_paths(self):
        external = self.home/'separate'
        self.profile('profile', [('_uid','external','.chaoxing.com')], root=external)
        self.root.mkdir(parents=True)
        (self.root/'profiles.ini').write_text('[Profile0]\nIsRelative=0\nPath='+str(external/'profile')+
            '\n[InstallFixture]\nDefault='+str(external/'profile')+'\n')
        candidates = discover_sessions(home=self.home)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(self.cookies(candidates[0])['_uid'], 'external')

    def test_readonly_snapshots_keep_original_db_wal_shm_bytes_and_metadata(self):
        profile = self.profile(rows=[])
        self.ini([('one',True)])
        database = profile/'cookies.sqlite'
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.execute("INSERT INTO moz_cookies VALUES ('_uid','wal-only','.chaoxing.com','/',0,'')")
        connection.commit()
        files = [database, Path(str(database)+'-wal'), Path(str(database)+'-shm')]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in files}
        candidates = discover_sessions(home=self.home)
        self.assertEqual(self.cookies(candidates[0])['_uid'], 'wal-only')
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in files}
        self.assertEqual(before, after)
        self.assertEqual(set(profile.iterdir()), set(files))

    def test_wal_without_shm_and_readonly_original_directory_is_preserved(self):
        profile = self.profile(rows=[])
        self.ini([('one', True)])
        database = profile/'cookies.sqlite'
        wal = Path(str(database)+'-wal')
        shm = Path(str(database)+'-shm')
        connection = sqlite3.connect(database)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.execute("INSERT INTO moz_cookies VALUES ('UID','wal-no-shm','.chaoxing.com','/',0,'')")
        connection.commit()
        db_bytes, wal_bytes = database.read_bytes(), wal.read_bytes()
        connection.close()
        # Reconstruct a valid committed WAL snapshot whose SHM has not been built.
        database.write_bytes(db_bytes)
        wal.write_bytes(wal_bytes)
        shm.unlink(missing_ok=True)
        before = {p: p.read_bytes() for p in (database, wal)}
        for path in (database, wal):
            path.chmod(0o400)
        profile.chmod(0o500)
        self.addCleanup(profile.chmod, 0o700)
        candidates = discover_sessions(home=self.home)
        self.assertEqual(self.cookies(candidates[0])['UID'], 'wal-no-shm')
        self.assertFalse(shm.exists())
        self.assertEqual(before, {p: p.read_bytes() for p in (database, wal)})
        self.assertEqual(set(profile.iterdir()), {database, wal})

    def test_rollback_lock_corrupt_missing_schema_and_bad_ini_are_silent(self):
        profile = self.profile(rows=[('_uid','do-not-log','.chaoxing.com')])
        self.ini([('one',True)])
        connection = sqlite3.connect(profile/'cookies.sqlite')
        self.addCleanup(connection.close)
        connection.execute('BEGIN EXCLUSIVE')
        connection.execute("UPDATE moz_cookies SET value='private-uncommitted'")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            self.assertEqual(discover_sessions(home=self.home), [])
        self.assertEqual(captured.getvalue(), '')
        connection.rollback()
        connection.close()
        (profile/'cookies.sqlite').write_bytes(b'corrupted-secret-database')
        self.assertEqual(discover_sessions(home=self.home), [])
        (self.root/'profiles.ini').write_text('[broken')
        self.assertEqual(discover_sessions(home=self.home), [])

    def test_backend_validation_repr_and_source_preservation(self):
        backend = self.home/'backend'
        backend.mkdir()
        path = backend/'cookies.txt'
        text = '_uid=private-session-value; token=private-token-value; bad=unsafe\nvalue'
        path.write_text(text)
        before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns)
        candidates = discover_sessions(home=self.home, backend_root=backend)
        self.assertEqual(self.cookies(candidates[0]), {'_uid':'private-session-value','token':'private-token-value'})
        self.assertNotIn('private-session-value', repr(candidates[0]))
        self.assertNotIn('private-token-value', repr(candidates[0]))
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns))
        for text in ('session=no-identity', 'UID=; session=empty', 'UID=identity; malformed', 'UID=x\r\ninjected:yes'):
            path.write_text(text)
            self.assertEqual(discover_sessions(home=self.home, backend_root=backend), [])

    def test_no_unrelated_roots_or_profiles_are_scanned(self):
        self.profile('listed', [('_uid','listed','.chaoxing.com')])
        self.profile('unlisted', [('_uid','unlisted','.chaoxing.com')])
        self.profile('ignored', [('_uid','unrelated-browser','.chaoxing.com')], root=self.home/'.config/other-browser')
        self.ini([('listed',True)])
        candidates = discover_sessions(home=self.home)
        self.assertEqual(len(candidates),1)
        self.assertEqual(self.cookies(candidates[0])['_uid'],'listed')

    def test_legacy_schema_without_origin_attributes_and_missing_home(self):
        self.profile(rows=[('UID','legacy','.chaoxing.com')], attributes=False)
        self.ini([('one',True)])
        self.assertEqual(self.cookies(discover_sessions(home=self.home)[0])['UID'],'legacy')
        self.assertEqual(discover_sessions(home=self.home/'missing'), [])

    def test_permission_errors_do_not_escape_with_paths_or_cookie_values(self):
        self.profile(rows=[('_uid','do-not-leak','.chaoxing.com')])
        self.ini([('one',True)])
        with patch.object(Path, 'exists', side_effect=PermissionError('private-path/private-cookie-value')):
            self.assertEqual(discover_sessions(home=self.home), [])

    def test_unstable_snapshot_is_skipped_without_touching_database(self):
        self.profile(rows=[('_uid','unstable','.chaoxing.com')])
        self.ini([('one',True)])
        # Emulate Firefox changing DB/WAL between snapshots; no partial candidate escapes.
        import chaoxing_linux.session_sync as module
        original = module._fingerprint
        counter = 0
        def changing(path):
            nonlocal counter
            result = original(path)
            counter += 1
            return (*result[:-1], counter) if result else result
        with patch.object(module, '_fingerprint', side_effect=changing):
            self.assertEqual(discover_sessions(home=self.home), [])


if __name__ == '__main__':
    unittest.main()
