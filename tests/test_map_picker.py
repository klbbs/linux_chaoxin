"""Tile cache policy checks and optional GTK interaction without network access."""
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
import urllib.error
from unittest.mock import patch

from chaoxing_linux.map_picker import CACHE_AGE, MapPicker, TileCache, _gtk
from chaoxing_linux.location import LocationError

PNG = b'\x89PNG\r\n\x1a\nfixture'


class Response(io.BytesIO):
    headers = {'ETag': 'fixture-etag', 'Last-Modified': 'Sun, 01 Nov 2026 00:00:00 GMT'}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


class TileCacheTests(unittest.TestCase):
    def test_cache_at_least_seven_days_and_identifying_agent(self):
        calls = []
        def opener(req, timeout):
            calls.append(req)
            return Response(PNG)
        with tempfile.TemporaryDirectory() as directory:
            cache = TileCache(cache_dir=directory, opener=opener)
            self.assertEqual(cache.get(3, 4, 3), PNG)
            image = next(Path(directory).rglob('*.png'))
            os.utime(image, (time.time()-6*86400,)*2)
            self.assertEqual(cache.get(3, 4, 3), PNG)
            self.assertEqual(len(calls), 1)
            self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o700)
            self.assertEqual(image.stat().st_mode & 0o777, 0o600)
        self.assertGreaterEqual(CACHE_AGE, 7*86400)
        self.assertEqual(calls[0].full_url, 'https://tile.openstreetmap.org/3/4/3.png')
        self.assertIn('ChaoxingLinux/', calls[0].get_header('User-agent'))
        self.assertIsNone(calls[0].get_header('Cache-control'))

    def test_expired_cache_conditional_revalidation(self):
        calls = []
        def opener(req, timeout):
            calls.append(req)
            if len(calls) == 2:
                raise urllib.error.HTTPError(req.full_url, 304, 'Not Modified', {}, None)
            return Response(PNG)
        with tempfile.TemporaryDirectory() as directory:
            cache = TileCache(cache_dir=directory, opener=opener)
            cache.get(3, 4, 3)
            image = next(Path(directory).rglob('*.png'))
            os.utime(image, (time.time()-8*86400,)*2)
            self.assertEqual(cache.get(3, 4, 3), PNG)
            self.assertEqual(calls[1].get_header('If-none-match'), 'fixture-etag')
            self.assertLess(time.time()-image.stat().st_mtime, 10)

    def test_timeout_and_invalid_images_are_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            def timeout(*args, **kwargs):
                raise TimeoutError('timeout')
            with self.assertRaisesRegex(LocationError, '暂不可用'):
                TileCache(cache_dir=directory, opener=timeout).get(2, 1, 1)
            with self.assertRaisesRegex(LocationError, '有效 PNG'):
                TileCache(cache_dir=directory, opener=lambda *a, **k: Response(b'bad')).get(2, 1, 1)
            self.assertEqual(list(Path(directory).rglob('*.png')), [])

    def test_provider_switch_uses_separate_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            first = TileCache(cache_dir=directory)
            second = TileCache(cache_dir=directory, template='https://example.org/{z}/{x}/{y}.png')
            self.assertNotEqual(first.cache_dir, second.cache_dir)
        with self.assertRaises(LocationError):
            TileCache(template='http://example.org/{z}/{x}/{y}')

    def test_inflight_network_worker_does_not_hold_process_exit(self):
        program = """from chaoxing_linux.map_picker import _DaemonPool
import threading, time
pool = _DaemonPool(max_workers=1)
started = threading.Event()
def blocked():
    started.set()
    threading.Event().wait(30)
pool.submit(blocked)
assert started.wait(1)
assert all(worker.daemon for worker in pool.workers)
pool.shutdown(wait=False, cancel_futures=True)
"""
        result = subprocess.run([sys.executable, '-c', program], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


try:
    Gtk, Gdk, _, GLib = _gtk()
    GTK_AVAILABLE = bool(os.environ.get('DISPLAY') and Gdk.Display.get_default())
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'Requires GTK3 and display')
class MapPickerUITests(unittest.TestCase):
    def setUp(self):
        self.picker = MapPicker()
        self.addCleanup(self.picker.dialog.destroy)

    def test_confirmation_disabled_until_location_selected_and_manual_validated(self):
        self.assertFalse(self.picker.confirm.get_sensitive())
        self.picker.latitude.set_text('39.9')
        self.picker.longitude.set_text('116.4')
        self.assertTrue(self.picker._apply_manual())
        self.assertTrue(self.picker.confirm.get_sensitive())
        self.assertEqual(self.picker.selected['coordinate_system'], 'gcj02')
        self.picker.latitude.set_text('nan')
        self.assertFalse(self.picker._apply_manual())

    def test_cancel_returns_none_without_requesting_any_tiles(self):
        GLib.idle_add(lambda: self.picker.dialog.response(Gtk.ResponseType.CANCEL))
        with patch.object(self.picker.tiles, 'get', side_effect=AssertionError('unexpected tile request')):
            self.assertIsNone(self.picker.run_location())
        self.assertTrue(self.picker.closed)

    def test_confirmation_preserves_explicit_system_and_search_provenance(self):
        from chaoxing_linux.location import location_record, wgs84_to_gcj02
        self.picker._select(location_record(40.0022905, 116.320963,
            coordinate_system='wgs84', name='清华大学', source='photon'))
        GLib.idle_add(lambda: self.picker.dialog.response(Gtk.ResponseType.OK))
        result = self.picker.run_location()
        expected = wgs84_to_gcj02(40.0022905, 116.320963)
        self.assertEqual(result['coordinate_system'], 'gcj02')
        self.assertEqual(result['source'], 'photon')
        self.assertAlmostEqual(result['latitude'], expected[0], places=6)
        self.assertAlmostEqual(result['wgs84_longitude'], 116.320963, places=6)

    def test_switch_coordinate_system_preserves_pending_manual_edit(self):
        from chaoxing_linux.location import location_record, gcj02_to_wgs84
        self.picker._select(location_record(40.0, 116.3, coordinate_system='gcj02'))
        self.picker.latitude.set_text('40.1')
        self.picker.longitude.set_text('116.5')
        self.picker.system.set_active_id('wgs84')
        expected = gcj02_to_wgs84(40.1, 116.5)
        self.assertAlmostEqual(float(self.picker.latitude.get_text()), expected[0], places=6)
        self.assertAlmostEqual(float(self.picker.longitude.get_text()), expected[1], places=6)
        self.assertEqual(self.picker.selected['source'], 'manual')

    def test_invalid_manual_coordinate_is_not_silently_overwritten_on_switch(self):
        from chaoxing_linux.location import location_record
        self.picker._select(location_record(40.0, 116.3))
        self.picker.latitude.set_text('invalid')
        self.picker.system.set_active_id('wgs84')
        self.assertEqual(self.picker.system.get_active_id(), 'gcj02')
        self.assertEqual(self.picker.latitude.get_text(), 'invalid')
        self.assertIn('修正手动坐标', self.picker.status.get_text())
