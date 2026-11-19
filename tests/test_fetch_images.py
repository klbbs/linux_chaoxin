"""Offline local HTTP range server; production never permits external HTTP."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/fetch-images.py'
SPEC = importlib.util.spec_from_file_location('fetch_images', SCRIPT)
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/metadata':
            entry = {'filename': 'official-test.zip', 'id': hashlib.sha256(self.server.payload).hexdigest(),
                     'size': len(self.server.payload), 'datetime': 123, 'url': self.server.url + '/data'}
            content = json.dumps({'response': [entry]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        requested = self.headers.get('Range', '')
        self.server.requests.append(requested)
        if self.path == '/no-range':
            self.send_response(200)
            self.send_header('Content-Length', str(len(self.server.payload)))
            self.end_headers()
            self.wfile.write(self.server.payload)
            return
        match = re.fullmatch(r'bytes=(\d+)-(\d+)', requested)
        if not match:
            self.send_error(416)
            return
        first, last = map(int, match.groups())
        if not 0 <= first <= last < len(self.server.payload):
            self.send_error(416)
            return
        content = self.server.payload[first:last + 1]
        self.send_response(206)
        self.send_header('Content-Range', f'bytes {first + (1 if self.path == "/wrong-range" else 0)}-{last}/{len(self.server.payload)}')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class FetchImagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.server.payload = bytes(range(256)) * 100 + b'final uneven chunk'
        cls.server.requests = []
        cls.server.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='chaoxing-fetch-test-')
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)
        self.server.requests.clear()
        self.image = {'filename': 'image.zip', 'size': len(self.server.payload),
                      'sha256': hashlib.sha256(self.server.payload).hexdigest(),
                      'url': self.server.url + '/data'}
        self.downloader = fetch.Downloader(self.cache, chunk_size=8192, workers=3,
                                           retries=1, allow_loopback=True, progress=lambda _: None)

    def test_validated_parallel_ranges_and_resume(self):
        target = self.downloader.download(self.image)
        self.assertEqual(target.read_bytes(), self.server.payload)
        expected = {f'bytes={first}-{min(first + 8192, len(self.server.payload)) - 1}'
                    for first in range(0, len(self.server.payload), 8192)}
        self.assertEqual(set(self.server.requests), expected)
        self.assertEqual(len(self.server.requests), len(expected))
        target.unlink()
        self.server.requests.clear()
        self.downloader.download(self.image)
        self.assertEqual(self.server.requests, [])

    def test_corrupted_cached_chunk_is_redownloaded(self):
        target = self.downloader.download(self.image)
        target.unlink()
        part = self.cache / 'chunks' / self.image['sha256'] / '8192/000001.part'
        part.write_bytes(b'X' * part.stat().st_size)
        self.server.requests.clear()
        self.downloader.download(self.image)
        self.assertEqual(self.server.requests, ['bytes=8192-16383'])
        self.assertEqual(target.read_bytes(), self.server.payload)

    def test_no_range_and_wrong_content_range_fail_without_final_image(self):
        for path in ('/no-range', '/wrong-range'):
            with self.subTest(path=path):
                self.image['url'] = self.server.url + path
                with self.assertRaisesRegex(ValueError, 'Chunk'):
                    self.downloader.download(self.image)
                self.assertFalse((self.cache / self.image['filename']).exists())

    def test_sha_failure_preserves_diagnostic_without_installing(self):
        self.image['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'SHA256/length mismatch'):
            self.downloader.download(self.image)
        self.assertFalse((self.cache / self.image['filename']).exists())
        invalid = self.cache / 'image.zip.merging.invalid'
        self.assertEqual(invalid.read_bytes(), self.server.payload)
        diagnostic = json.loads((self.cache / 'image.zip.merging.invalid.json').read_text())
        self.assertEqual(diagnostic['actual_sha256'], hashlib.sha256(self.server.payload).hexdigest())

    def test_metadata_and_production_url_policy(self):
        result = fetch.fetch_metadata(self.server.url + '/metadata', allow_loopback=True)
        self.assertEqual(result['size'], len(self.server.payload))
        self.assertEqual(result['sha256'], hashlib.sha256(self.server.payload).hexdigest())
        for url in ('http://sourceforge.net/a', 'https://sourceforge.net.evil.test/a',
                    'https://user:pass@sourceforge.net/a', 'http://192.168.1.1/a',
                    'https://evil.test/a'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch.validate_url(url, allow_loopback=True)
        with self.assertRaises(ValueError):
            fetch.validate_url(self.server.url + '/data')
        fetch.validate_url('https://netix.dl.sourceforge.net/a')
        fetch.validate_url('https://ota.waydro.id/system/a.json', 'metadata')
        with self.assertRaises(ValueError):
            fetch.validate_url('https://sourceforge.net/a.json', 'metadata')


if __name__ == '__main__':
    unittest.main()
