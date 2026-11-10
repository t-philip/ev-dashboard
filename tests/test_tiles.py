import math
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path

from evdash import mapview, tiles

PNG = b"\x89PNG\r\n\x1a\n" + b"test-data-tile"
URL = "https://tiles.example.test/{z}/{x}/{y}.png"


class Valid(unittest.TestCase):
    def test_numbers_must_be_whole_and_inside_the_grid(self):
        self.assertTrue(tiles.valid(5, 0, 31))
        for bad in ((1, 0, 0), (18, 0, 0), (5, 32, 0), (5, 0, 32), (5, -1, 0), (5, 1.5, 0), (True, 1, 1)):
            self.assertFalse(tiles.valid(*bad), bad)


class Cache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000.0
        self.urls = []
        self.reply = PNG

    def make(self):
        def download(url, agent):
            self.urls.append((url, agent))
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
        return tiles.TileCache(Path(self.tmp.name) / "t", URL, "test-agent", download=download,
                               clock=lambda: self.now)

    def test_only_the_template_with_whole_numbers_is_fetched_with_the_given_agent(self):
        self.assertEqual(self.make().get(5, 16, 10), PNG)
        self.assertEqual(self.urls, [("https://tiles.example.test/5/16/10.png", "test-agent")])

    def test_cached_tiles_are_reused_until_they_are_a_month_old(self):
        cache = self.make()
        cache.get(5, 16, 10)
        cache.get(5, 16, 10)
        self.assertEqual(len(self.urls), 1)
        path = Path(self.tmp.name) / "t" / "5" / "16" / "10.png"
        import os
        os.utime(path, (self.now, self.now))
        self.now += tiles.CACHE_DAYS * 86400 + 1
        cache.get(5, 16, 10)
        self.assertEqual(len(self.urls), 2)

    def test_an_old_tile_is_still_served_when_the_service_cannot_be_reached(self):
        cache = self.make()
        cache.get(5, 16, 10)
        import os
        path = Path(self.tmp.name) / "t" / "5" / "16" / "10.png"
        os.utime(path, (self.now, self.now))
        self.now += tiles.CACHE_DAYS * 86400 + 1
        self.reply = urllib.error.URLError("test-data")
        self.assertEqual(cache.get(5, 16, 10), PNG)

    def test_a_failed_or_odd_reply_gives_nothing_and_is_not_kept(self):
        for reply in (urllib.error.URLError("test-data"), b"<html>not a png</html>", PNG + b"x" * tiles.MAX_TILE_BYTES):
            self.reply = reply
            self.assertIsNone(self.make().get(6, 1, 1))
        self.assertFalse((Path(self.tmp.name) / "t" / "6" / "1" / "1.png").exists())

    def test_a_refreshed_tile_is_not_counted_twice(self):
        import os
        cache = self.make()
        cache.get(5, 16, 10)
        first = cache._size
        os.utime(Path(self.tmp.name) / "t" / "5" / "16" / "10.png", (self.now, self.now))
        self.now += tiles.CACHE_DAYS * 86400 + 1
        cache.get(5, 16, 10)
        self.assertEqual(len(self.urls), 2)
        self.assertEqual(cache._size, first)

    def test_invalid_numbers_are_never_fetched(self):
        self.assertIsNone(self.make().get(99, 1, 1))
        self.assertEqual(self.urls, [])

    def test_uncached_fetches_are_rate_limited(self):
        cache = self.make()
        got = [cache.get(12, i, 0) for i in range(tiles.MAX_FETCHES_PER_MINUTE + 5)]
        self.assertEqual(len(self.urls), tiles.MAX_FETCHES_PER_MINUTE)
        self.assertEqual(got[-1], None)
        self.now += 61
        self.assertEqual(cache.get(12, 999, 0), PNG)

    def test_redirects_are_refused(self):
        handler = tiles._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://other.example.test/"))

    def test_the_failure_log_names_the_error_class_only(self):
        self.reply = urllib.error.URLError("secret-address.example.test")
        with self.assertLogs("evdash.tiles", level="WARNING") as logs:
            self.make().get(7, 1, 1)
        text = "\n".join(logs.output)
        self.assertIn("URLError", text)
        self.assertNotIn("secret-address", text)
        self.assertNotIn("7/1/1", text)


class MapDrawing(unittest.TestCase):
    A = (10.10, 20.10, "Test Data a", "start")
    B = (10.20, 20.25, "Test Data b", "end")
    LINE = ((10.10, 20.10), (10.20, 20.25))

    def test_plain_drawing_is_unchanged_and_fetches_nothing(self):
        svg = mapview.render([self.A, self.B], [self.LINE])
        self.assertNotIn("<image", svg)
        self.assertNotIn("openstreetmap", svg.lower())

    def test_tiles_cover_the_view_and_are_addressed_to_this_server_only(self):
        svg = mapview.render([self.A, self.B], [self.LINE], tiles=True)
        refs = re.findall(r'href="([^"]+)"', svg)
        tile_refs = [r for r in refs if r.startswith("/tiles/")]
        self.assertTrue(4 <= len(tile_refs) <= 16)
        self.assertEqual([r for r in refs if not r.startswith("/tiles/")],
                         ["https://www.openstreetmap.org/copyright"])
        for ref in tile_refs:
            z, x, y = (int(v) for v in re.fullmatch(r"/tiles/(\d+)/(\d+)/(\d+)\.png", ref).groups())
            self.assertTrue(tiles.valid(z, x, y), ref)
        self.assertIn("OpenStreetMap contributors", svg)

    def test_points_land_where_the_mercator_formula_puts_them(self):
        z = 2
        for candidate in range(mapview.TILE_ZOOM_MAX, 1, -1):
            xs = [mapview._world(p[0], p[1], candidate)[0] for p in (self.A, self.B)]
            ys = [mapview._world(p[0], p[1], candidate)[1] for p in (self.A, self.B)]
            if max(xs) - min(xs) <= mapview.WIDTH - 80 and max(ys) - min(ys) <= mapview.HEIGHT - 80:
                z = candidate
                break
        svg = mapview.render([self.A, self.B], [self.LINE], tiles=True)
        self.assertIn(f"/tiles/{z}/", svg)
        ax, ay = mapview._world(10.10, 20.10, z)
        bx, by = mapview._world(10.20, 20.25, z)
        cx = re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', svg)
        (sx, sy), (ex, ey) = [(float(a), float(b)) for a, b in cx[:2]]
        self.assertAlmostEqual(ex - sx, bx - ax, delta=0.2)
        self.assertAlmostEqual(ey - sy, by - ay, delta=0.2)

    def test_a_single_point_gets_a_close_zoom_and_the_world_edge_does_not_crash(self):
        svg = mapview.render([self.A], [], tiles=True)
        self.assertIn(f"/tiles/{mapview.TILE_ZOOM_MAX}/", svg)
        mapview.render([(85.0, 179.9, "Test Data", "stop"), (-85.0, -179.9, "Test Data", "stop")], [], tiles=True)

    def test_labels_are_escaped(self):
        svg = mapview.render([(10.1, 20.1, "<b>x</b>", "stop")], [], tiles=True)
        self.assertNotIn("<b>x", svg)


if __name__ == "__main__":
    unittest.main()
