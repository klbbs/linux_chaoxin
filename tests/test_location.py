"""Coordinate geometry and mocked public-service contracts, no account writes."""
import io
import json
import math
import tempfile
from pathlib import Path
import unittest

from chaoxing_linux.location import (LocationError, MapViewport, PlaceSearch,
    gcj02_to_wgs84, location_record, project, unproject, validate_coordinates, wgs84_to_gcj02)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class CoordinateTests(unittest.TestCase):
    def test_validation_rejects_nonfinite_and_out_of_range(self):
        for lat, lon in ((float('nan'), 1), (1, float('inf')), (91, 0), (0, -181), ('bad', 0)):
            with self.assertRaises(LocationError):
                validate_coordinates(lat, lon)
        self.assertEqual(validate_coordinates('40.1', '116.2'), (40.1, 116.2))

    def test_beijing_known_shift_and_inverse(self):
        converted = wgs84_to_gcj02(39.908823, 116.39747)
        self.assertAlmostEqual(converted[0], 39.9102265, places=5)
        self.assertAlmostEqual(converted[1], 116.4037136, places=5)
        original = gcj02_to_wgs84(*converted)
        self.assertAlmostEqual(original[0], 39.908823, places=7)
        self.assertAlmostEqual(original[1], 116.39747, places=7)

    def test_outside_china_unmodified(self):
        self.assertEqual(wgs84_to_gcj02(51.5, -.1), (51.5, -.1))
        self.assertEqual(gcj02_to_wgs84(-33.8, 151.2), (-33.8, 151.2))

    def test_record_retains_coordinate_system_and_original_world_point(self):
        lat, lon = wgs84_to_gcj02(40.0022905, 116.320963)
        record = location_record(lat, lon, name=' 清华大学 ', coordinate_system='gcj02')
        self.assertEqual(record['name'], '清华大学')
        self.assertAlmostEqual(record['wgs84_latitude'], 40.0022905, places=7)
        self.assertAlmostEqual(record['wgs84_longitude'], 116.320963, places=7)
        self.assertEqual(record['coordinate_system'], 'gcj02')
        with self.assertRaises(LocationError):
            location_record(40, 116, coordinate_system='bd09')

    def test_projection_roundtrip_and_polar_clamp(self):
        for lat, lon in ((40, 116), (0, 0), (-33, 151), (80, -170)):
            actual = unproject(*project(lat, lon, 16), 16)
            self.assertAlmostEqual(actual[0], lat, places=7)
            self.assertAlmostEqual(actual[1], lon, places=7)
        self.assertTrue(all(math.isfinite(v) for v in project(90, 180, 16)))

    def test_click_pan_zoom_preserves_screen_anchor(self):
        viewport = MapViewport(40, 116, 12)
        self.assertAlmostEqual(viewport.coordinates_at(400, 300, 800, 600)[0], 40)
        before = viewport.coordinates_at(600, 200, 800, 600)
        viewport.zoom_at(2, 600, 200, 800, 600)
        after = viewport.coordinates_at(600, 200, 800, 600)
        self.assertAlmostEqual(before[0], after[0], places=7)
        self.assertAlmostEqual(before[1], after[1], places=7)
        viewport.pan(20, -10)
        px, py = viewport.pixel_at(*after, 800, 600)
        self.assertAlmostEqual(px, 620, places=5)
        self.assertAlmostEqual(py, 190, places=5)

    def test_tiles_only_intersect_viewport_and_wrap(self):
        viewport = MapViewport(0, 179.9, 3)
        tiles = list(viewport.tiles(800, 500))
        self.assertLessEqual(len(tiles), 20)
        for z, x, y, left, top in tiles:
            self.assertTrue(0 <= x < 2**z and 0 <= y < 2**z)
            self.assertTrue(left < 800 and left+256 > 0)
            self.assertTrue(top < 500 and top+256 > 0)


class SearchTests(unittest.TestCase):
    def test_search_parses_chinese_place_and_caches_query(self):
        calls = []
        def opener(request, timeout):
            calls.append((request, timeout))
            return Response(json.dumps({'features': [{'geometry': {'coordinates': [116.320963, 40.0022905]},
                'properties': {'name': '清华大学', 'country': '中国', 'city': '北京市'}}]}).encode())
        with tempfile.TemporaryDirectory() as directory:
            search = PlaceSearch(cache_dir=directory, opener=opener, timeout=4)
            places = search.search('清华大学')
            self.assertEqual(search.search('清华大学'), places)
            self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o700)
            self.assertEqual(next(Path(directory).glob('*.json')).stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(calls), 1)
        self.assertIn('ChaoxingLinux/', calls[0][0].get_header('User-agent'))
        self.assertEqual(calls[0][1], 4)
        self.assertEqual(places[0]['coordinate_system'], 'wgs84')
        self.assertIn('北京市', places[0]['address'])

    def test_timeout_has_actionable_error(self):
        def timeout(*args, **kwargs):
            raise TimeoutError('network timeout')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LocationError, '暂不可用'):
                PlaceSearch(cache_dir=directory, opener=timeout).search('学校')

    def test_endpoint_and_query_validation(self):
        with self.assertRaises(LocationError):
            PlaceSearch(base_url='http://example.org')
        with self.assertRaises(LocationError):
            PlaceSearch().search('')
