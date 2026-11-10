import base64
import hashlib
import html
import json
import re
import unittest
from pathlib import Path

from evdash import mapview, tiles as tiles_mod
from evdash import trips as trips_mod

from test_trips import B, PNG, WebHarness

STATIC = Path(__file__).resolve().parent.parent / "src" / "evdash" / "static"
# SHA-256 (base64) that the Leaflet project publishes for 1.9.4: detects an edited or swapped copy.
LEAFLET = {"leaflet.js": "20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=",
           "leaflet.css": "p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="}
A = (10.10, 20.10, "Test Data a", "start")
Z = (10.20, 20.25, "Test Data <b>b</b>", "end")


class Bundle(unittest.TestCase):
    def test_the_bundled_library_is_the_unmodified_release(self):
        for name, expected in LEAFLET.items():
            digest = base64.b64encode(hashlib.sha256((STATIC / name).read_bytes()).digest()).decode()
            self.assertEqual(digest, expected, name)

    def test_our_script_contacts_nothing_but_this_server_and_never_builds_markup_from_data(self):
        script = (STATIC / "evmap.js").read_text(encoding="utf-8")
        self.assertEqual(sorted(set(re.findall(r"https?://[A-Za-z0-9./_-]+", script))),
                         ["https://www.openstreetmap.org/copyright"])
        self.assertIn('"/tiles/{z}/{x}/{y}.png"', script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("eval(", script)


class Markup(unittest.TestCase):
    def test_points_travel_as_escaped_data_and_the_static_map_stays_as_the_fallback(self):
        out = mapview.render_interactive([A, Z], [((A[0], A[1]), (Z[0], Z[1]))])
        self.assertIn('<div class="evmap"', out)
        self.assertEqual(out.count("<script"), 2)
        self.assertNotRegex(out, r"<script>")  # no inline script: both load from /static/
        self.assertIn("<svg", out)             # the fallback
        raw = re.search(r'data-points="([^"]*)"', out).group(1)
        points = json.loads(html.unescape(raw))
        self.assertEqual(points[1][2], "Test Data <b>b</b>")  # round-trips as text
        self.assertNotIn("<b>", raw)                           # but is never raw markup in the page
        lines = json.loads(html.unescape(re.search(r'data-lines="([^"]*)"', out).group(1)))
        self.assertEqual(lines, [[[10.1, 20.1], [10.2, 20.25]]])

    def test_nothing_to_show_gives_the_plain_message(self):
        self.assertIn("no positions", mapview.render_interactive([]).lower())


class Served(WebHarness):
    def cache(self):
        return tiles_mod.TileCache(Path(self.tmp.name) / "tiles", "https://tiles.example.test/{z}/{x}/{y}.png",
                                   "test-agent", download=lambda url, agent: PNG)

    def test_static_files_are_served_only_from_a_fixed_list_and_only_when_the_map_is_on(self):
        self.assertEqual(self.get("/static/evmap.js", location=True)[0], 404)  # map off
        self.assertEqual(self.get("/static/evmap.js", location=False, tiles=self.cache())[0], 404)
        status, body, headers = self.get("/static/leaflet.js", location=True, tiles=self.cache())
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("Leaflet 1.9.4", body)
        self.assertTrue(self.get("/static/leaflet.css", location=True, tiles=self.cache())[1].startswith("/*"))
        for path in ("/static/", "/static/nothing.js", "/static/../server.py", "/static/..%2fserver.py",
                     "/static/LEAFLET-LICENSE.txt", "/static/leaflet.js/x", "/static/images/layers.png"):
            self.assertEqual(self.get(path, location=True, tiles=self.cache())[0], 404, path)

    def test_pages_with_the_map_allow_scripts_and_styles_from_this_server_only(self):
        _, page, headers = self.get("/trips", location=True, tiles=self.cache())
        policy = headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", policy)
        self.assertIn("style-src 'unsafe-inline' 'self'", policy)
        self.assertIn("img-src 'self'", policy)
        self.assertTrue(policy.startswith("default-src 'none'"))
        self.assertIn("<div class=\"evmap\"", page)
        _, detail, _ = self.get("/trips/" + trips_mod.trip_id(B[0]), location=True, tiles=self.cache())
        self.assertIn("<div class=\"evmap\"", detail)

    def test_without_the_map_pages_still_allow_no_scripts(self):
        _, page, headers = self.get("/trips", location=True)
        self.assertNotIn("script", headers["Content-Security-Policy"])
        self.assertNotIn("<script", page)
        self.assertNotIn("evmap", page)


if __name__ == "__main__":
    unittest.main()
