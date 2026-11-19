"""Small, synthetic APK fixtures exercise metadata parsing without extraction."""
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from chaoxing_linux.apk import ANDROID_NS, MAX_MANIFEST, compatibility, inspect_apk


TEXT = f'''<manifest xmlns:android="{ANDROID_NS}" package="com.chaoxing.mobile"
android:versionName="6.7.8" android:versionCode="123"><application/></manifest>'''.encode()


def binary_manifest(utf8=True):
    strings = ['manifest', 'package', 'com.chaoxing.mobile', ANDROID_NS,
               'versionName', '6.7.8', 'versionCode', 'android']
    pool = b''
    offsets = []
    for item in strings:
        offsets.append(len(pool))
        if utf8:
            encoded = item.encode('utf-8')
            pool += bytes([len(item), len(encoded)]) + encoded + b'\0'
        else:
            pool += struct.pack('<H', len(item)) + item.encode('utf-16-le') + b'\0\0'
    string_start = 28 + 4 * len(strings)
    pool += b'\0' * (-(string_start + len(pool)) % 4)
    string_chunk = struct.pack('<HHI5I', 1, 28, string_start + len(pool), len(strings),
                               0, 0x100 if utf8 else 0, string_start, 0)
    string_chunk += struct.pack('<' + 'I' * len(offsets), *offsets) + pool

    def node(kind, extra):
        return struct.pack('<HHIII', kind, 16, 16 + len(extra), 1, 0xFFFFFFFF) + extra

    def attr(ns, name, raw, kind, value):
        return struct.pack('<IIIHBBI', ns, name, raw, 8, 0, kind, value)

    attrs = attr(0xFFFFFFFF, 1, 2, 3, 2)
    attrs += attr(3, 4, 5, 3, 5)
    attrs += attr(3, 6, 0xFFFFFFFF, 0x10, 123)
    ns = struct.pack('<II', 7, 3)
    start = node(0x102, struct.pack('<II6H', 0xFFFFFFFF, 0, 20, 20, 3, 0, 0, 0) + attrs)
    end = node(0x103, struct.pack('<II', 0xFFFFFFFF, 0))
    body = string_chunk + node(0x100, ns) + start + end + node(0x101, ns)
    return struct.pack('<HHI', 3, 8, 8 + len(body)) + body


class ApkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='chaoxing-apk-')
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'misleading-name.apk'

    def apk(self, manifest=TEXT, entries=()):
        with zipfile.ZipFile(self.path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            if manifest is not None:
                archive.writestr('AndroidManifest.xml', manifest)
            for name, content in entries:
                archive.writestr(name, content)
        return self.path

    def test_text_metadata_hash_and_dex_count(self):
        path = self.apk(entries=[('classes.dex', b'dex'), ('classes2.dex', b'dex'),
                                 ('classes10.dex', b'dex'), ('classes1.dex', b'ignore'),
                                 ('assets/classes.dex', b'ignore')])
        result = inspect_apk(path)
        self.assertEqual(result['package'], 'com.chaoxing.mobile')
        self.assertEqual(result['version_name'], '6.7.8')
        self.assertEqual(result['version_code'], 123)
        self.assertEqual(result['dex_count'], 3)
        self.assertEqual(result['abis'], [])
        self.assertEqual(result['bytes'], path.stat().st_size)
        self.assertEqual(result['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertTrue(compatibility(result, []))

    def test_binary_utf8_and_utf16_metadata(self):
        for utf8 in (True, False):
            with self.subTest(utf8=utf8):
                result = inspect_apk(self.apk(binary_manifest(utf8)))
                self.assertEqual((result['version_name'], result['version_code']), ('6.7.8', 123))

    def test_abi_matching_requires_actual_runtime_support(self):
        info = inspect_apk(self.apk(entries=[('lib/arm64-v8a/libcx.so', b'arm'),
                                             ('lib/armeabi-v7a/libcx.so', b'arm')]))
        self.assertEqual(info['abis'], ['arm64-v8a', 'armeabi-v7a'])
        self.assertFalse(compatibility(info, ['x86_64', 'x86']))
        self.assertFalse(compatibility(info, []))
        self.assertTrue(compatibility(info, ['x86_64', 'arm64-v8a']))
        self.assertTrue(compatibility(info, 'x86_64,arm64-v8a'))

    def test_invalid_manifests_and_wrong_package(self):
        cases = [None, b'', b'<manifest>', b'<resources/>',
                 TEXT.replace(b'com.chaoxing.mobile', b'org.other.app'),
                 TEXT.replace(b'"123"', b'"invalid"'),
                 b'<!DOCTYPE manifest [<!ENTITY x "abc">]><manifest package="&x;"/>',
                 b'x' * (MAX_MANIFEST + 1)]
        for manifest in cases:
            with self.subTest(length=None if manifest is None else len(manifest)):
                with self.assertRaises(ValueError):
                    inspect_apk(self.apk(manifest))

    def test_path_traversal_and_nonstandard_native_paths(self):
        for name in ('../escape', '/absolute', 'lib/../x.so', 'lib/arm64-v8a/nested/x.so',
                     'lib\\arm64-v8a\\x.so', 'C:/escape', 'a//file'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                inspect_apk(self.apk(entries=[(name, b'bad')]))

    def test_empty_corrupt_and_missing_file(self):
        self.path.write_bytes(b'')
        with self.assertRaises(ValueError):
            inspect_apk(self.path)
        self.path.write_bytes(b'not a zip')
        with self.assertRaises(ValueError):
            inspect_apk(self.path)
        self.path.unlink()
        with self.assertRaises(ValueError):
            inspect_apk(self.path)

    def test_binary_bounds_and_unclosed_elements(self):
        original = binary_manifest()
        variants = [original[:-1]]
        bad = bytearray(original)
        struct.pack_into('<I', bad, 8 + 8, 0xFFFFFFFF)  # string count
        variants.append(bytes(bad))
        bad = bytearray(original)
        struct.pack_into('<I', bad, 8 + 28, 0xFFFFFFFF)  # first string offset
        variants.append(bytes(bad))
        # Drop end element + namespace end, while keeping outer length consistent.
        bad = bytearray(original[:-48])
        struct.pack_into('<I', bad, 4, len(bad))
        variants.append(bytes(bad))
        for index, manifest in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(ValueError):
                inspect_apk(self.apk(manifest))


if __name__ == '__main__':
    unittest.main()
