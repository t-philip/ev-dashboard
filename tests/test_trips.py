import datetime
import logging
import re
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock
import urllib.request
from pathlib import Path

from evdash import auth, collector, config, mapview, parse, server, store, tripviews
from evdash import trips as trips_mod

# Fictitious values only: nothing here is, or resembles, a real place, vehicle or person.
VIN = "TESTDATA0000000A1"
UTC = datetime.timezone.utc


def ts(day, hour, minute=0):
    return f"1999-03-{day:02d}T{hour:02d}:{minute:02d}:00.000000Z"


def stops(*items):
    """items: (ts, lat, lon)"""
    return trips_mod.stops_from([(t, la) for t, la, _ in items], [(t, lo) for t, _, lo in items])


A = (ts(4, 8), 10.000, 20.000)
B = (ts(4, 9), 10.100, 20.100)      # about 15.6 km from A
C = (ts(4, 12), 10.300, 20.300)     # about 31.2 km from B
NEAR = (ts(4, 12, 30), 10.1005, 20.1005)  # about 70 m from B


class Detection(unittest.TestCase):
    def detect(self, items, odo=(), battery=(), charging=(), engine=()):
        return trips_mod.detect(stops(*items), list(odo), list(battery), list(charging), list(engine))

    def test_trips_are_the_moves_between_consecutive_stops(self):
        got = self.detect([A, B, C], odo=[(A[0], 100.0), (B[0], 112.5), (C[0], 140.0)])
        self.assertEqual(len(got), 2)
        self.assertEqual([t.distance_km for t in got], [12.5, 27.5])
        self.assertEqual(got[0].start.lat, A[1])
        self.assertEqual(got[0].end.ts, B[0])
        self.assertEqual(got[1].start.ts, B[0])  # the end of one trip is the start of the next
        self.assertEqual(len({t.id for t in got}), 2)

    def test_the_same_place_reported_again_is_not_a_trip(self):
        self.assertEqual(self.detect([B, NEAR], odo=[(B[0], 112.5), (NEAR[0], 112.5)]), [])

    def test_a_single_stop_makes_no_trip(self):
        self.assertEqual(self.detect([A]), [])
        self.assertEqual(self.detect([]), [])

    def test_without_an_odometer_far_stops_are_a_trip_with_unknown_distance(self):
        got = self.detect([A, B])
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0].distance_km)
        self.assertGreater(got[0].straight_km, 15)

    def test_a_stale_odometer_does_not_show_a_false_zero(self):
        got = self.detect([A, B], odo=[(A[0], 100.0), (B[0], 100.0)])
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0].distance_km)

    def test_readings_far_from_a_stop_are_not_matched_to_it(self):
        got = self.detect([A, B], odo=[(ts(3, 1), 90.0), (ts(5, 1), 200.0)])
        self.assertIsNone(got[0].distance_km)

    def test_an_observed_engine_start_gives_the_start_time(self):
        got = self.detect([A, B], engine=[(ts(4, 8, 40), "RUNNING"), (ts(4, 9), "STOPPED")])
        self.assertEqual(got[0].started_at, ts(4, 8, 40))
        before = self.detect([A, B], engine=[(ts(4, 7), "RUNNING")])  # before the previous stop
        self.assertIsNone(before[0].started_at)

    def test_charging_between_the_stops_is_flagged(self):
        by_status = self.detect([A, B], charging=[(ts(4, 8, 30), "CHARGING")])
        self.assertTrue(by_status[0].charged)
        by_level = self.detect([A, B], battery=[(A[0], 40.0), (B[0], 80.0)])
        self.assertTrue(by_level[0].charged)
        plain = self.detect([A, B], battery=[(A[0], 80.0), (B[0], 70.0)])
        self.assertFalse(plain[0].charged)
        self.assertEqual((plain[0].battery_start, plain[0].battery_end), (80.0, 70.0))

    def test_stops_with_bad_coordinates_are_dropped(self):
        got = trips_mod.stops_from(
            [(ts(4, 8), 10.0), (ts(4, 9), 95.0), (ts(4, 10), "x"), (ts(4, 11), 10.0), ("garbage", 10.0)],
            [(ts(4, 8), 20.0), (ts(4, 9), 20.0), (ts(4, 10), 20.0), ("garbage", 20.0)])
        self.assertEqual([s.ts for s in got], [ts(4, 8)])  # 95 is out of range; the 11:00 stop has no longitude

    def test_haversine_one_degree_of_latitude(self):
        self.assertAlmostEqual(trips_mod.haversine_km(10, 20, 11, 20), 111.2, delta=0.3)

    def test_stops_are_sorted_by_time(self):
        got = stops(B, A)
        self.assertEqual([s.ts for s in got], [A[0], B[0]])


class Maps(unittest.TestCase):
    def test_a_single_point_and_identical_points_still_render(self):
        for points in ([(10.0, 20.0, "one", "stop")], [(10.0, 20.0, "a", "start"), (10.0, 20.0, "b", "end")]):
            svg = mapview.render(points)
            self.assertIn("<circle", svg)
            self.assertNotIn("nan", svg.lower())

    def test_nothing_valid_gives_a_notice_not_an_error(self):
        self.assertIn("No positions to show", mapview.render([]))
        self.assertIn("No positions to show", mapview.render([(95.0, 20.0, "x", "stop"), (float("nan"), 1.0, "y", "stop")]))

    def test_the_picture_has_a_scale_bar_and_loads_nothing(self):
        svg = mapview.render([(10.0, 20.0, "a", "start"), (10.1, 20.1, "b", "end")],
                             [((10.0, 20.0), (10.1, 20.1))])
        self.assertRegex(svg, r"\d+(\.\d+)? km")
        for forbidden in ("href", "src=", "<script", "<image", "url(", "@import"):
            self.assertNotIn(forbidden, svg)
        # the XML namespace is the only URL-like text in the picture, and it is never fetched
        self.assertEqual(re.findall(r"https?://[^\"' ]+", svg), ["http://www.w3.org/2000/svg"])

    def test_labels_are_escaped(self):
        svg = mapview.render([(10.0, 20.0, "<script>alert(1)</script>", "stop")])
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)

    def test_points_are_drawn_to_scale(self):
        svg = mapview.render([(10.0, 20.0, "a", "start"), (10.0, 20.2, "b", "end")])
        xs = [float(x) for x in re.findall(r'cx="([\d.]+)"', svg)]
        self.assertGreater(abs(xs[1] - xs[0]), 100)  # far apart horizontally, so well separated


def make_db(tmp, trip_rows=True):
    db = store.Store(Path(tmp) / "t.db")
    db.add_vehicle(VIN, ts(1, 0))
    if trip_rows:
        readings = []
        for (t, lat, lon), odo in zip([A, B, C], [100.0, 112.5, 140.0]):
            readings += [parse.Reading("location.latitude", lat, "deg", t),
                         parse.Reading("location.longitude", lon, "deg", t),
                         parse.Reading("odometer.odometer", odo, "km", t)]
        db.add_readings(VIN, readings, ts(5, 0))
    return db


class Pages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "t.db"

    def fill(self, **kwargs):
        db = make_db(self.tmp.name, **kwargs)
        db.close()

    def test_no_database_or_no_trips_explains_why(self):
        for path in (None, self.path):
            status, page = tripviews.render_list(path, {}, UTC)
            self.assertEqual(status, 200)
            self.assertIn("No trips yet", page)
        self.fill(trip_rows=False)
        self.assertIn("No trips yet", tripviews.render_list(self.path, {}, UTC)[1])

    def test_the_list_shows_trips_newest_first_with_a_map_and_the_straight_line_warning(self):
        self.fill()
        status, page = tripviews.render_list(self.path, {}, UTC)
        self.assertEqual(status, 200)
        self.assertIn("1999-03", page)
        self.assertIn("2 trips, 40 km", page)
        table = page[page.index("<table>"):]
        self.assertLess(table.index("12:00"), table.index("09:00"))   # newest first
        self.assertIn("<svg", page)
        self.assertIn("not</strong> the routes driven", page)
        self.assertNotIn("openstreetmap", page)  # no third-party link on the list

    def test_a_bad_or_unknown_month_falls_back_to_the_latest(self):
        self.fill()
        for month in ("garbage", "2099-01", "1999-13", "<script>"):
            status, page = tripviews.render_list(self.path, {"month": [month]}, UTC)
            self.assertEqual(status, 200, month)
            self.assertIn("2 trips", page)
            self.assertNotIn("<script>", page)

    def test_months_have_previous_and_next_links(self):
        db = make_db(self.tmp.name)
        db.add_readings(VIN, [parse.Reading("location.latitude", 10.5, "deg", "1999-04-04T08:00:00.000000Z"),
                              parse.Reading("location.longitude", 20.5, "deg", "1999-04-04T08:00:00.000000Z")], ts(5, 0))
        db.close()
        _, april = tripviews.render_list(self.path, {}, UTC)       # the latest month holds one trip
        self.assertIn("month=1999-03", april)
        self.assertIn("1 trip,", april)
        _, march = tripviews.render_list(self.path, {"month": ["1999-03"]}, UTC)
        self.assertIn("month=1999-04", march)
        self.assertIn("2 trips", march)

    def test_times_are_shown_in_the_chosen_time_zone_and_months_follow_it(self):
        self.fill()
        plus_14 = datetime.timezone(datetime.timedelta(hours=14))
        _, page = tripviews.render_list(self.path, {}, plus_14)
        self.assertIn("23:00", page)        # 09:00 UTC is 23:00 at +14
        self.assertIn("Thu 04 Mar 1999", page)

    def test_the_detail_page_has_the_facts_a_map_and_an_explicit_click_out_link(self):
        self.fill()
        trip_id = trips_mod.trip_id(B[0])
        status, page = tripviews.render_detail(self.path, trip_id, UTC)
        self.assertEqual(status, 200)
        for text in ("12.5 km", "10.0000, 20.0000", "10.1000, 20.1000", "<svg", "not</strong> the route driven",
                     "Nothing is sent unless you click"):
            self.assertIn(text, page)
        self.assertEqual(page.count("openstreetmap.org"), 3)  # two links and the sentence that explains them
        self.assertIn("rel='noreferrer noopener'", page)
        self.assertIn("later trip", page)
        self.assertNotIn("earlier trip", page)  # it is the first trip

    def test_unknown_or_hostile_trip_ids_are_a_404(self):
        self.fill()
        for bad in ("nope12345", "../etc/passwd", "<script>alert(1)</script>", "", "x" * 200, "1999%00"):
            status, page = tripviews.render_detail(self.path, bad, UTC)
            self.assertEqual(status, 404, bad)
            self.assertNotIn("<script>", page)

    def test_non_numeric_stored_values_are_ignored_and_never_rendered(self):
        db = make_db(self.tmp.name)
        db.add_readings(VIN, [parse.Reading("odometer.odometer", "<b>x</b>", None, ts(4, 8, 5)),
                              parse.Reading("location.latitude", "<i>y</i>", None, ts(4, 8, 6)),
                              parse.Reading("location.longitude", "<i>y</i>", None, ts(4, 8, 6))], ts(5, 0))
        db.close()
        status, page = tripviews.render_list(self.path, {}, UTC)
        self.assertEqual(status, 200)
        self.assertIn("2 trips", page)  # the garbage rows changed nothing
        for markup in ("<b>x</b>", "<i>y</i>"):
            self.assertNotIn(markup, page)

    def test_months_follow_the_time_zone_across_a_month_boundary(self):
        db = make_db(self.tmp.name, trip_rows=False)
        late = ("1999-03-31T21:00:00.000000Z", "1999-03-31T23:30:00.000000Z")  # 01:30 on 1 April at +02:00
        for t, lat in zip(late, (10.0, 10.2)):
            db.add_readings(VIN, [parse.Reading("location.latitude", lat, "deg", t),
                                  parse.Reading("location.longitude", 20.0, "deg", t)], t)
        db.close()
        plus_2 = datetime.timezone(datetime.timedelta(hours=2))
        self.assertIn("<strong>1999-04</strong>", tripviews.render_list(self.path, {}, plus_2)[1])
        self.assertIn("<strong>1999-03</strong>", tripviews.render_list(self.path, {}, UTC)[1])

    def test_a_timestamp_at_the_edge_of_the_calendar_does_not_crash_a_page(self):
        minus_14 = datetime.timezone(datetime.timedelta(hours=-14))
        self.assertIsNone(tripviews.local("0001-01-01T00:00:00.000000Z", minus_14))
        self.assertIsNotNone(tripviews.local(ts(4, 8), minus_14))

    def test_the_computed_trips_are_cached_until_the_database_changes(self):
        self.fill()
        with mock.patch.object(tripviews.trips_mod, "load", wraps=trips_mod.load) as load:
            tripviews.render_list(self.path, {}, UTC)
            tripviews.render_list(self.path, {"month": ["1999-03"]}, UTC)
            tripviews.render_detail(self.path, trips_mod.trip_id(B[0]), UTC)
            self.assertEqual(load.call_count, 1)  # three requests, one computation
            db = store.Store(self.path)
            db.add_readings(VIN, [parse.Reading("location.latitude", 11.0, "deg", ts(6, 8)),
                                  parse.Reading("location.longitude", 21.0, "deg", ts(6, 8))], ts(6, 9))
            db.close()
            tripviews.render_list(self.path, {}, UTC)
            self.assertEqual(load.call_count, 2)  # a write invalidates it


class FakeManager:
    def status(self):
        return {"signed_in": True, "granted_scopes": ["openid"]}


class Web(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        make_db(self.tmp.name).close()
        self.db_path = Path(self.tmp.name) / "t.db"
        self.cfg = auth.OAuthConfig("test-data-id", "test-data-secret", "https://x.example.test/cb",
                                    "https://x.example.test/a", "https://x.example.test/t", ("openid",))

    def get(self, path, location, tz=UTC):
        app = server.App(self.cfg, FakeManager(), auth.PendingSignIns(), self.db_path, location=location, tz=tz)
        srv = server.make_server(app, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{srv.server_address[1]}{path}", timeout=10) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode(), dict(err.headers)

    def test_the_trip_pages_do_not_exist_unless_location_is_on(self):
        for path in ("/trips", "/trips/" + trips_mod.trip_id(B[0])):
            status, body, _ = self.get(path, location=False)
            self.assertEqual(status, 404)
            self.assertIn("Location is turned off", body)
            self.assertNotIn("<svg", body)

    def test_the_trip_pages_work_when_location_is_on(self):
        status, body, _ = self.get("/trips", location=True)
        self.assertEqual(status, 200)
        self.assertIn("<svg", body)
        self.assertIn("2 trips", body)
        status, body, _ = self.get("/trips/" + trips_mod.trip_id(B[0]), location=True)
        self.assertEqual(status, 200)
        self.assertIn("12.5 km", body)
        self.assertEqual(self.get("/trips/doesnotexist", location=True)[0], 404)
        self.assertEqual(self.get("/trips/a/b", location=True)[0], 404)

    def test_a_failing_page_is_a_plain_500_and_logs_only_the_error_class(self):
        with mock.patch.object(tripviews, "render_list", side_effect=RuntimeError("secret 10.1 20.1")), \
                self.assertLogs("evdash.server", level=logging.WARNING) as logs:
            status, body, _ = self.get("/trips", location=True)
        self.assertEqual(status, 500)
        self.assertIn("Something went wrong", body)
        self.assertNotIn("Traceback", body)
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("10.1", text)

    def test_pages_are_sent_with_no_scripts_no_external_loads_and_no_caching(self):
        _, _, headers = self.get("/trips", location=True)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_the_home_page_links_to_trips_only_when_on_and_never_shows_coordinates(self):
        _, off, _ = self.get("/", location=False)
        _, on, _ = self.get("/", location=True)
        self.assertNotIn("/trips", off)
        self.assertIn("href=/trips", on)
        for page in (off, on):
            for coordinate in ("10.3", "20.3", "10.1000", "20.1"):  # the latest and an earlier stop
                self.assertNotIn(coordinate, page)

    def test_the_access_log_never_records_a_query_string_or_a_position(self):
        with self.assertLogs("evdash.server", level=logging.INFO) as logs:
            self.get("/trips?month=1999-03", location=True)
            self.get("/trips/" + trips_mod.trip_id(B[0]) + "?lat=10.1", location=True)
        text = "\n".join(logs.output)
        self.assertIn("/trips/", text)       # the requests were logged...
        self.assertNotIn("month=", text)     # ...without their query strings
        self.assertNotIn("lat=", text)
        self.assertNotIn("10.1", text)


class Collection(unittest.TestCase):
    """Location collection: opt-in, scope-checked, with a retention limit."""

    T0 = 920524800.0  # 1999-03-04T05:20:00Z

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = store.Store(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.db.close)
        self.now = self.T0
        self.requests = []

    def get(self, url, headers):
        self.requests.append(url)
        if url.endswith("/location"):
            return 200, {"data": {"geometry": {"coordinates": [20.0, 10.0]},
                                  "properties": {"timestamp": "1999-03-04T05:00:00Z", "heading": "90"}}}, {}
        return 200, {}, {}

    def make(self, granted, **kwargs):
        class M:
            def access_token(self_):
                return "test-data-token"

            def status(self_):
                return {"signed_in": True, "granted_scopes": granted}
        return collector.Collector(M(), self.db, "test-data-api-key", base="https://api.example.test", vins=(VIN,),
                                   get=self.get, clock=lambda: self.now, sleep=lambda s: None, **kwargs)

    def polled_location(self):
        return any(u.endswith("/location") for u in self.requests)

    def test_location_needs_both_the_setting_and_the_granted_scope(self):
        self.make(["openid", "location:read"], location=False).run_once()
        self.assertFalse(self.polled_location())               # not enabled
        self.make(["openid"], location=True).run_once()
        self.assertFalse(self.polled_location())               # enabled, scope not granted
        self.make(["openid", "location:read"], location=True).run_once()
        self.assertTrue(self.polled_location())

    def test_unknown_scopes_fail_closed(self):
        for status in (lambda: {"signed_in": True, "granted_scopes": None}, mock.Mock(side_effect=RuntimeError("x"))):
            class M:
                def access_token(self_):
                    return "test-data-token"
            M.status = lambda self_, _s=status: _s()
            self.requests.clear()
            collector.Collector(M(), self.db, "test-data-api-key", base="https://api.example.test", vins=(VIN,),
                                get=self.get, clock=lambda: self.now, sleep=lambda s: None,
                                location=True).run_once()
            self.assertFalse(self.polled_location())

    def test_a_manager_that_cannot_report_scopes_fails_closed(self):
        class NoStatus:
            def access_token(self_):
                return "test-data-token"
        collector.Collector(NoStatus(), self.db, "test-data-api-key", base="https://api.example.test", vins=(VIN,),
                            get=self.get, clock=lambda: self.now, sleep=lambda s: None, location=True).run_once()
        self.assertFalse(self.polled_location())

    def test_probe_refuses_the_location_endpoint_unless_location_is_on_and_granted(self):
        path = "/location/v1/vehicles/{vin}/location"
        for granted, enabled in ((["openid", "location:read"], False), (["openid"], True)):
            self.requests.clear()
            got = self.make(granted, location=enabled).explore([path])
            self.assertEqual(self.requests, [])
            self.assertIn("refused", got[0][2])
        self.requests.clear()
        got = self.make(["openid", "location:read"], location=True).explore([path])
        self.assertEqual(got[0][1], 200)
        self.assertEqual(len(self.requests), 1)

    def test_a_failed_compaction_is_retried_on_the_next_pass_without_new_deletions(self):
        old = "1997-01-01T00:00:00.000000Z"
        self.db.add_readings(VIN, [parse.Reading("location.latitude", 1.0, "deg", old)], old)
        worker = self.make(["openid"], location=False, location_retention_days=30)
        real = self.db.compact
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            real()
        with mock.patch.object(self.db, "compact", side_effect=flaky), \
                self.assertLogs("evdash.collector", level=logging.WARNING) as logs:
            worker.run_once()          # the delete works, the compaction fails
            self.assertIn("postponed", "\n".join(logs.output))
            worker.run_once()          # same day, nothing left to delete: compaction is still retried
        self.assertEqual(len(attempts), 2)
        worker.run_once()
        self.assertEqual(len(attempts), 2)  # and not again once it has succeeded

    def test_a_pending_compaction_survives_a_restart(self):
        old = "1997-01-01T00:00:00.000000Z"
        self.db.add_readings(VIN, [parse.Reading("location.latitude", 1.0, "deg", old)], old)
        first = self.make(["openid"], location=False, location_retention_days=30)
        with mock.patch.object(self.db, "compact", side_effect=sqlite3.OperationalError("database is locked")):
            first.run_once()                     # deleted, but the compaction failed; then "the process ends"
        self.assertTrue(self.db.compact_pending())
        second = self.make(["openid"], location=False, location_retention_days=30)  # a fresh process
        second.run_once()                        # removes nothing, yet finishes the compaction
        self.assertFalse(self.db.compact_pending())

    def test_a_checkpoint_blocked_by_a_reader_is_not_reported_as_done(self):
        class Proxy:
            """A connection whose checkpoint reports 'busy', as when another connection is reading."""
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args):
                if "wal_checkpoint" in sql:
                    return self._real.execute("SELECT 1, 0, 0")
                return self._real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._real, name)
        old = "1997-01-01T00:00:00.000000Z"
        self.db.add_readings(VIN, [parse.Reading("location.latitude", 1.0, "deg", old)], old)
        self.db.delete_older("location.", "1999-01-01T00:00:00.000000Z")
        self.assertTrue(self.db.compact_pending())
        real = self.db._db
        self.db._db = Proxy(real)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.db.compact()
        finally:
            self.db._db = real
        self.assertTrue(self.db.compact_pending())  # still pending: it will be retried
        self.db.compact()
        self.assertFalse(self.db.compact_pending())

    def test_scopes_that_are_not_a_collection_do_not_grant_location(self):
        class M:
            def access_token(self_):
                return "test-data-token"

            def status(self_):
                return {"granted_scopes": "openid location:read"}  # a string: a substring match would say yes
        collector.Collector(M(), self.db, "test-data-api-key", base="https://api.example.test", vins=(VIN,),
                            get=self.get, clock=lambda: self.now, sleep=lambda s: None, location=True).run_once()
        self.assertFalse(self.polled_location())

    def test_a_retention_failure_does_not_stop_the_polling_pass(self):
        worker = self.make(["openid"], location=False)
        with mock.patch.object(self.db, "delete_older", side_effect=sqlite3.OperationalError("database is locked")), \
                self.assertLogs("evdash.collector", level=logging.WARNING) as logs:
            summary = worker.run_once()
        self.assertGreater(summary["polled"], 0)
        self.assertIn("OperationalError", "\n".join(logs.output))

    def test_deleted_positions_do_not_linger_in_the_database_files(self):
        old = "1997-01-01T00:00:00.000000Z"
        marker = "ZZ-TEST-MARKER-ZZ"
        self.db.add_readings(VIN, [parse.Reading("location.note", marker, None, old)], old)

        def files():  # read while the collector's connection is still open, as in production
            return b"".join(f.read_bytes() for f in Path(self.tmp.name).glob("t.db*"))
        self.assertIn(marker.encode(), files())  # positive control: it really is on disk first
        worker = self.make(["openid"], location=False, location_retention_days=30)
        worker.run_once()
        self.assertEqual(self.db.series(VIN, "location.note"), [])
        self.assertNotIn(marker.encode(), files())

    def test_a_missing_scope_is_reported_once_not_every_pass(self):
        worker = self.make(["openid"], location=True)
        with self.assertLogs("evdash.collector", level=logging.WARNING) as logs:
            worker.run_once()
            self.now += 100000
            worker.run_once()
        self.assertEqual(sum("location:read" in line for line in logs.output), 1)

    def test_old_location_readings_are_deleted_once_a_day_and_nothing_else(self):
        old, recent = "1997-01-01T00:00:00.000000Z", "1999-03-01T00:00:00.000000Z"
        self.db.add_readings(VIN, [parse.Reading("location.latitude", 1.0, "deg", old),
                                   parse.Reading("location.latitude", 2.0, "deg", recent),
                                   parse.Reading("odometer.odometer", 5, "km", old),
                                   parse.Reading("locationx.other", 7, None, old)], old)
        worker = self.make(["openid"], location=False, location_retention_days=365)
        worker.run_once()
        self.assertEqual([v for _, v in self.db.series(VIN, "location.latitude")], [2.0])  # the old row is gone
        left = {(r.field, r.value) for r in self.db.latest(VIN)}
        self.assertIn(("odometer.odometer", 5), left)           # other fields are untouched
        self.assertIn(("locationx.other", 7), left)             # the prefix is exact
        self.db.add_readings(VIN, [parse.Reading("location.longitude", 3.0, "deg", old)], old)
        worker.run_once()
        self.assertEqual(len(self.db.series(VIN, "location.longitude")), 1)  # same day: not run again
        self.now += 86400
        worker.run_once()
        self.assertEqual(self.db.series(VIN, "location.longitude"), [])

    def test_a_position_already_past_retention_is_not_stored_again(self):
        # a car parked for longer than the retention period keeps reporting the same old timestamp
        asked = []

        def parked(url, headers):
            if url.endswith("/location"):
                asked.append(url)
                return 200, {"data": {"geometry": {"coordinates": [20.0, 10.0]},
                                      "properties": {"timestamp": "1997-01-01T00:00:00Z"}}}, {}
            return 200, {}, {}
        worker = self.make(["openid", "location:read"], location=True, location_retention_days=30)
        worker._get = parked
        worker.run_once()
        self.assertEqual(len(asked), 1)   # the position really was requested, then not stored
        self.assertEqual(self.db.series(VIN, "location.latitude"), [])
        worker._get = self.get   # a position inside the window is stored as usual
        self.now += 100000
        worker.run_once()
        self.assertEqual(len(self.db.series(VIN, "location.latitude")), 1)

    def test_the_probe_gate_does_not_depend_on_letter_case(self):
        for path in ("/Location/v1/vehicles/{vin}/location", "/LOCATION/v1/vehicles/{vin}/location",
                     "/location"):
            self.requests.clear()
            got = self.make(["openid"], location=True).explore([path])
            self.assertEqual(self.requests, [], path)
            self.assertIn("refused", got[0][2])

    def test_retention_limits_are_enforced(self):
        for bad in (0, -1, 3651):
            with self.assertRaises(ValueError):
                self.make(["openid"], location_retention_days=bad)

    def test_store_series_is_ordered_and_delete_is_prefix_exact(self):
        self.db.add_readings(VIN, [parse.Reading("a.x", 2, None, ts(4, 9)), parse.Reading("a.x", 1, None, ts(4, 8))], ts(5, 0))
        self.assertEqual([v for _, v in self.db.series(VIN, "a.x")], [1, 2])
        self.assertEqual(self.db.delete_older("a.", ts(4, 9)), 1)
        self.assertEqual([v for _, v in self.db.series(VIN, "a.x")], [2])


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        for name in ("client_id", "client_secret"):
            (d / name).write_text("test-data-" + name, encoding="utf-8")
        self.env = {"EVDASH_REDIRECT_URI": "https://dashboard.example.test/auth/callback", "EVDASH_SCOPES": "openid",
                    "EVDASH_CLIENT_ID_FILE": str(d / "client_id"), "EVDASH_CLIENT_SECRET_FILE": str(d / "client_secret")}

    def test_defaults_and_validation(self):
        s = config.load(self.env)
        self.assertEqual((s.location, s.location_retention_days, s.tz), (False, 365, UTC))
        for bad in ({"EVDASH_LOCATION_RETENTION_DAYS": "0"}, {"EVDASH_LOCATION_RETENTION_DAYS": "x"},
                    {"EVDASH_LOCATION_RETENTION_DAYS": "99999"}, {"EVDASH_TIMEZONE": "Not/AZone"}):
            with self.assertRaises(config.ConfigError, msg=str(bad)):
                config.load({**self.env, **bad})
        self.assertEqual(config.load({**self.env, "EVDASH_TIMEZONE": "utc"}).tz, UTC)

    def test_a_named_time_zone_is_accepted_when_the_system_has_its_data(self):
        try:
            import zoneinfo
            zoneinfo.ZoneInfo("Pacific/Auckland")
        except Exception:
            self.skipTest("no time zone database on this system")
        tz = config.load({**self.env, "EVDASH_TIMEZONE": "Pacific/Auckland"}).tz
        self.assertEqual(tz.utcoffset(datetime.datetime(1999, 7, 1)), datetime.timedelta(hours=12))  # winter there


if __name__ == "__main__":
    unittest.main()
