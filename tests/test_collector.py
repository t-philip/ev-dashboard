import calendar
import http.client
import logging
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from evdash import __main__ as cli
from evdash import auth, collector, config, parse, probe, store

# Fictitious values only: nothing here is, or resembles, real vehicle data or a credential.
VIN = "TESTDATA0000000A1"
TS = "1999-03-04T05:06:07Z"
TSN = "1999-03-04T05:06:07.000000Z"  # the same instant, as the parser normalises it
T0 = calendar.timegm((1999, 3, 4, 5, 6, 0))  # a fixed clock, in seconds since the epoch
API_KEY = "test-data-api-key"
TOKEN = "test-data-access-token"


def connected(**fields):
    return {"data": {k: {"value": v, "unit": u, "timestamp": TS} for k, (v, u) in fields.items()}}


class Parsing(unittest.TestCase):
    def test_connected_reading_is_namespaced_by_group(self):
        out = parse.parse_connected("odometer", connected(odometer=(1234, "km")))
        self.assertEqual(out, [parse.Reading("odometer.odometer", 1234, "km", TSN)])

    def test_missing_unsupported_and_malformed_fields_are_dropped(self):
        payload = {"data": {
            "none": None,
            "unspecified": {"value": "UNSPECIFIED", "unit": None, "timestamp": TS},
            "no_value": {"unit": "km", "timestamp": TS},
            "bad_time": {"value": 1, "unit": "km", "timestamp": "yesterday"},
            "boolean": {"value": True, "unit": None, "timestamp": TS},
            "huge": {"value": "x" * 500, "unit": None, "timestamp": TS},
            "bad name!": {"value": 1, "unit": None, "timestamp": TS},
            "ok": {"value": "CLOSED", "unit": None, "timestamp": TS},
        }}
        self.assertEqual([r.field for r in parse.parse_connected("doors", payload)], ["doors.ok"])

    def test_energy_keeps_ok_fields_and_skips_error_objects(self):
        payload = {
            "batteryChargeLevel": {"status": "OK", "value": 80.5, "unit": "percentage", "updatedAt": TS},
            "chargingCurrentLimit": {"status": "ERROR", "code": "PROPERTY_NOT_SUPPORTED", "message": "x"},
        }
        out = parse.parse_energy("energy", payload)
        self.assertEqual(out, [parse.Reading("energy.batteryChargeLevel", 80.5, "percentage", TSN)])

    def test_location_point_and_heading(self):
        payload = {"data": {"geometry": {"type": "Point", "coordinates": [10.5, 20.25, 3.0]},
                            "properties": {"heading": "90", "timestamp": TS}}}
        got = {r.field: r.value for r in parse.parse_location("location", payload)}
        self.assertEqual(got, {"location.longitude": 10.5, "location.latitude": 20.25,
                               "location.altitude": 3.0, "location.heading": 90.0})

    def test_garbage_payloads_give_nothing(self):
        for payload in ({}, {"data": "x"}, {"data": []}, {"data": {"a": 5}}):
            self.assertEqual(parse.parse_connected("g", payload), [])
            self.assertEqual(parse.parse_location("g", payload), [])

    def test_timestamps_are_normalised_so_they_sort_as_text(self):
        for raw in ("1999-03-04T06:06:07+01:00", "1999-03-04T05:06:07", "1999-03-04T05:06:07.000Z"):
            got = parse.parse_connected("g", {"data": {"a": {"value": 1, "unit": None, "timestamp": raw}}})
            self.assertEqual(got[0].ts, TSN, raw)

    def test_integers_too_large_for_sqlite_are_dropped_not_fatal(self):
        payload = {"data": {"big": {"value": 10 ** 400, "unit": None, "timestamp": TS},
                            "small": {"value": 5, "unit": None, "timestamp": TS}}}
        self.assertEqual([r.field for r in parse.parse_connected("g", payload)], ["g.small"])

    def test_vehicle_list_accepts_only_well_formed_vins(self):
        payload = {"data": [{"vin": VIN}, {"vin": "short"}, {"vin": "I" * 17}, {"nope": 1}, "x"]}
        self.assertEqual(parse.parse_vehicle_list(payload), [VIN])


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "test.db"
        self.db = store.Store(self.path)
        self.addCleanup(self.db.close)

    def test_same_reading_is_stored_once(self):
        r = parse.Reading("energy.batteryChargeLevel", 80.0, "percentage", TS)
        self.assertEqual(self.db.add_readings(VIN, [r], "1999-03-04T05:07:00Z"), 1)
        self.assertEqual(self.db.add_readings(VIN, [r], "1999-03-04T05:12:00Z"), 0)

    def test_values_keep_their_type_and_latest_wins(self):
        later = "1999-03-04T06:00:00Z"
        self.db.add_readings(VIN, [parse.Reading("a.number", 7, "km", TS),
                                   parse.Reading("a.text", "LOCKED", None, TS),
                                   parse.Reading("a.number", 9, "km", later)], "1999-03-04T06:01:00Z")
        latest = {r.field: r for r in self.db.latest(VIN)}
        self.assertEqual(latest["a.number"].value, 9)
        self.assertIsInstance(latest["a.number"].value, int)
        self.assertEqual(latest["a.text"].value, "LOCKED")

    def test_budget_is_enforced_per_day(self):
        self.assertTrue(self.db.take_call("1999-03-04", 2))
        self.assertTrue(self.db.take_call("1999-03-04", 2))
        self.assertFalse(self.db.take_call("1999-03-04", 2))
        self.assertEqual(self.db.calls_on("1999-03-04"), 2)  # the refused call was not counted
        self.assertTrue(self.db.take_call("1999-03-05", 2))

    def test_state_round_trip_keeps_last_ok(self):
        self.db.set_state(VIN, "doors", 100.0, 0, 200, TS)
        self.db.set_state(VIN, "doors", 200.0, 1, 500, None)
        state = self.db.state(VIN, "doors")
        self.assertEqual((state.next_due, state.fails, state.last_http, state.last_ok),
                         (200.0, 1, 500, TS))

    def test_readonly_open_works_for_an_odd_path(self):
        odd = Path(self.tmp.name) / "a b#c%d" / "t.db"
        db = store.Store(odd)
        db.add_vehicle(VIN, TS)
        db.close()
        readonly = store.Store(odd, readonly=True)
        self.addCleanup(readonly.close)
        self.assertEqual(readonly.vehicles(), [VIN])

    def test_a_failed_batch_is_rolled_back(self):
        class Bad:
            field, unit, ts = "g.a", None, TSN

            @property
            def value(self):
                raise RuntimeError("boom")
        good = parse.Reading("g.b", 1, None, TSN)
        with self.assertRaises(RuntimeError):
            self.db.add_readings(VIN, [good, Bad()], TS)
        self.db.set_state(VIN, "g", 1.0, 0, 200, None)  # an unrelated commit
        self.assertEqual(self.db.latest(VIN), [])

    def test_latest_picks_the_newest_instant_across_offsets(self):
        older = parse.parse_connected(
            "g", {"data": {"a": {"value": 1, "unit": None, "timestamp": "1999-03-04T07:00:00+02:00"}}})
        newer = parse.parse_connected(
            "g", {"data": {"a": {"value": 2, "unit": None, "timestamp": "1999-03-04T06:00:00Z"}}})
        self.db.add_readings(VIN, older + newer, TS)
        self.assertEqual(self.db.latest(VIN)[0].value, 2)

    def test_readonly_open_of_a_missing_database_fails_cleanly(self):
        with self.assertRaises(sqlite3.OperationalError):
            store.Store(Path(self.tmp.name) / "absent.db", readonly=True)


class FakeManager:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def access_token(self):
        self.calls += 1
        if self.error:
            raise self.error
        return TOKEN


class FakeApi:
    """Replies by path suffix; records every request."""

    def __init__(self, replies=None, default=(200, {})):
        self.replies = replies or {}
        self.default = default
        self.requests = []

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        for suffix, reply in self.replies.items():
            if url.endswith(suffix):
                return reply if len(reply) == 3 else (reply[0], reply[1], {})
        return (*self.default, {})


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class CollectorBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = store.Store(Path(self.tmp.name) / "test.db")
        self.addCleanup(self.db.close)
        self.clock = Clock()
        self.slept = []

    def make(self, api, manager=None, **kwargs):
        kwargs.setdefault("vins", (VIN,))
        return collector.Collector(
            manager or FakeManager(), self.db, API_KEY, base="https://api.example.test",
            get=api, clock=self.clock, sleep=self.slept.append, **kwargs)

    def test_first_pass_polls_every_group_and_stores_readings(self):
        api = FakeApi({"/doors": (200, connected(centralLock=("LOCKED", None))),
                       "/state": (200, {"batteryChargeLevel": {"status": "OK", "value": 55.0,
                                                                  "unit": "percentage", "updatedAt": TS}})})
        summary = self.make(api).run_once()
        self.assertEqual(summary["polled"], len(collector.GROUPS) - 1)  # location is off by default
        self.assertEqual(summary["stored"], 2)
        self.assertEqual({r.field for r in self.db.latest(VIN)},
                         {"doors.centralLock", "energy.batteryChargeLevel"})

    def test_requests_carry_the_token_and_application_key(self):
        api = FakeApi()
        self.make(api).run_once()
        for url, headers in api.requests:
            self.assertTrue(url.startswith("https://api.example.test/"))
            self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
            self.assertEqual(headers["vcc-api-key"], API_KEY)
            self.assertIn("Accept", headers)

    def test_a_406_tries_the_next_accept_header_and_remembers_the_one_that_works(self):
        seen = []

        def get(url, headers):
            if url.endswith("/state"):
                seen.append(headers["Accept"])
                if headers["Accept"] != collector.ACCEPT["energy"][1]:
                    return 406, {}, {}
            return 200, {}, {}
        worker = self.make(get)
        worker.run_once()
        self.assertEqual(seen, list(collector.ACCEPT["energy"][:2]))  # second try worked, no third
        self.clock.now += 400
        worker.run_once()
        self.assertEqual(seen[-1], collector.ACCEPT["energy"][1])  # the remembered one goes first
        self.assertEqual(len(seen), 3)

    def test_a_remembered_header_that_starts_failing_falls_back_to_the_others(self):
        seen = []
        replies = iter([406, 200, 406, 200])  # v2 no, v1 yes (remembered); later v1 no, v2 yes

        def get(url, headers):
            if url.endswith("/state"):
                seen.append(headers["Accept"])
                return next(replies), {}, {}
            return 200, {}, {}
        worker = self.make(get)
        worker.run_once()
        self.clock.now += 400
        worker.run_once()
        v2, v1 = collector.ACCEPT["energy"][:2]
        self.assertEqual(seen, [v2, v1, v1, v2])

    def test_every_accept_try_is_counted_against_the_budget(self):
        calls = []

        def get(url, headers):
            calls.append(url)
            return 406, {}, {}
        worker = self.make(get, budget=2, groups=[collector.GROUPS[0]])
        worker.run_once()
        self.assertEqual(len(calls), 2)  # the first poll and one more try, then the budget stops it
        self.assertEqual(self.db.calls_on(self._day()), 2)

    def _day(self):
        return collector._iso(self.clock())[:10]

    def test_nothing_is_polled_again_before_its_interval(self):
        api = FakeApi()
        worker = self.make(api)
        worker.run_once()
        first = len(api.requests)
        self.clock.now += 60
        self.assertEqual(worker.run_once()["polled"], 0)
        self.assertEqual(len(api.requests), first)
        self.clock.now += 400  # past the 5-minute energy interval only
        self.assertEqual(worker.run_once()["polled"], 1)
        self.assertTrue(api.requests[-1][0].endswith("/state"))

    def test_schedule_survives_a_restart(self):
        api = FakeApi()
        self.make(api).run_once()
        before = len(api.requests)
        self.make(api).run_once()  # a new collector on the same database
        self.assertEqual(len(api.requests), before)

    def test_daily_budget_is_never_exceeded(self):
        api = FakeApi()
        worker = self.make(api, budget=3)
        summary = worker.run_once()
        self.assertEqual(len(api.requests), 3)
        self.assertTrue(summary["budget_spent"] and worker.budget_spent)
        self.clock.now += 3600
        worker.run_once()
        self.assertEqual(len(api.requests), 3)  # still the same UTC day
        self.clock.now += 86400
        worker.run_once()
        self.assertGreater(len(api.requests), 3)

    def test_a_failure_backs_off_and_is_not_retried_at_once(self):
        api = FakeApi({"/doors": (500, {})})
        worker = self.make(api)
        summary = worker.run_once()
        self.assertEqual(summary["failed"], 1)
        state = self.db.state(VIN, "doors")
        self.assertEqual((state.fails, state.last_http), (1, 500))
        self.assertGreaterEqual(state.next_due - T0, 600)
        before = len(api.requests)
        worker.run_once()
        self.assertEqual(len(api.requests), before)

    def test_backoff_is_capped_and_honours_retry_after(self):
        group = collector.GROUPS[0]
        self.assertLessEqual(collector.Collector._backoff(group, 50, {}), collector.MAX_BACKOFF)
        self.assertGreaterEqual(collector.Collector._backoff(group, 1, {"Retry-After": "5000"}), 5000)
        self.assertEqual(collector.Collector._backoff(group, 1, {"Retry-After": "junk"}),
                         collector.Collector._backoff(group, 1, {}))

    def test_a_200_with_no_readings_is_logged_as_a_warning(self):
        api = FakeApi({"/doors": (200, {"data": {}})})
        with self.assertLogs("evdash.collector", level=logging.WARNING) as logs:
            self.make(api).run_once()
        self.assertIn("doors returned 200 but no usable readings", "\n".join(logs.output))

    def test_retry_after_is_honoured_on_any_status(self):
        api = FakeApi({"/doors": (503, {}, {"Retry-After": "7000"})})
        self.make(api).run_once()
        self.assertGreaterEqual(self.db.state(VIN, "doors").next_due - T0, 7000)

    def test_success_clears_the_failure_count(self):
        api = FakeApi({"/doors": (500, {})})
        worker = self.make(api)
        worker.run_once()
        api.replies["/doors"] = (200, connected(centralLock=("LOCKED", None)))
        self.clock.now += 5000
        worker.run_once()
        self.assertEqual(self.db.state(VIN, "doors").fails, 0)

    def test_signed_out_makes_no_requests(self):
        api = FakeApi()
        summary = self.make(api, manager=FakeManager(auth.NotSignedIn("x"))).run_once()
        self.assertFalse(summary["signed_in"])
        self.assertEqual(api.requests, [])

    def test_location_is_collected_only_when_enabled(self):
        point = {"data": {"geometry": {"coordinates": [10.5, 20.25]}, "properties": {"timestamp": TS}}}
        api = FakeApi({"/location": (200, point)})
        self.make(api).run_once()
        self.assertFalse(any(u.endswith("/location") for u, _ in api.requests))
        self.assertEqual([r for r in self.db.latest(VIN) if r.field.startswith("location.")], [])
        self.clock.now += 100000
        self.make(api, location=True).run_once()
        self.assertTrue(any(u.endswith("/location") for u, _ in api.requests))
        self.assertIn("location.latitude", {r.field for r in self.db.latest(VIN)})

    def test_vehicles_are_discovered_once_then_remembered(self):
        api = FakeApi({"/vehicles": (200, {"data": [{"vin": VIN}]})})
        worker = self.make(api, vins=())
        worker.run_once()
        lists = [u for u, _ in api.requests if u.endswith("/connected-vehicle/v2/vehicles")]
        self.assertEqual(len(lists), 1)
        self.assertEqual(self.db.vehicles(), [VIN])
        self.clock.now += 100000
        worker.run_once()
        lists = [u for u, _ in api.requests if u.endswith("/connected-vehicle/v2/vehicles")]
        self.assertEqual(len(lists), 1)

    def test_logs_never_contain_the_vin_or_secrets(self):
        api = FakeApi({"/vehicles": (200, {"data": [{"vin": VIN}]}), "/doors": (500, {})})
        with self.assertLogs("evdash.collector", level=logging.DEBUG) as logs:
            self.make(api, vins=()).run_once()
        text = "\n".join(logs.output)
        for secret in (VIN, API_KEY, TOKEN):
            self.assertNotIn(secret, text)
        self.assertIn(VIN[-4:], text)

    def test_budget_outside_the_allowed_range_is_refused(self):
        for bad in (0, -1, collector.MAX_BUDGET + 1):
            with self.assertRaises(ValueError):
                self.make(FakeApi(), budget=bad)

    def test_vehicle_list_failure_backs_off_instead_of_repeating_every_tick(self):
        api = FakeApi({"/vehicles": (401, {})})
        worker = self.make(api, vins=())
        worker.run_once()
        self.clock.now += 30
        worker.run_once()
        self.clock.now += 30
        worker.run_once()
        self.assertEqual(len(api.requests), 1)  # one attempt, then it waits
        self.clock.now += 4000
        worker.run_once()
        self.assertEqual(len(api.requests), 2)

    def test_a_bad_reply_does_not_escape_and_is_logged_by_class_only(self):
        def boom(url, headers):
            raise ValueError("Invalid header value " + headers["Authorization"])
        with self.assertLogs("evdash.collector", level=logging.DEBUG) as logs:
            summary = self.make(boom).run_once()
        self.assertGreater(summary["failed"], 0)
        text = "\n".join(logs.output)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(API_KEY, text)
        self.assertIn("ValueError", text)

    def test_a_storage_error_in_one_poll_does_not_stop_the_rest(self):
        real = self.db.add_readings
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(*args, **kwargs)
        with mock.patch.object(self.db, "add_readings", side_effect=flaky):
            summary = self.make(FakeApi()).run_once()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["polled"], len(collector.GROUPS) - 1)

    def test_run_forever_survives_a_failing_pass(self):
        stop = threading.Event()
        worker = self.make(FakeApi())
        outcomes = [RuntimeError("boom"), {"signed_in": True}]

        def run_once():
            result = outcomes.pop(0)
            if not outcomes:
                stop.set()
            if isinstance(result, Exception):
                raise result
            return result
        with mock.patch.object(worker, "run_once", side_effect=run_once), \
                mock.patch.object(stop, "wait") as waited, self.assertLogs("evdash.collector") as logs:
            worker.run_forever(stop)
        self.assertEqual(outcomes, [])
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertEqual(waited.call_args_list[0].args[0], 60.0)

    def test_run_forever_stops_when_asked(self):
        stop = threading.Event()
        stop.set()
        worker = self.make(FakeApi())
        with mock.patch.object(worker, "run_once") as run_once:
            worker.run_forever(stop)  # returns at once
        run_once.assert_not_called()


class Probe(unittest.TestCase):
    def test_describe_shows_shape_and_status_words_but_never_values(self):
        reply = {"data": {"lock": {"value": "LOCKED", "unit": None, "timestamp": TS},
                          "level": {"value": 41.5, "unit": "percentage"},
                          "count": {"value": 7}, "vin": VIN, "name": "Test Data Car",
                          "where": {"coordinates": [10.5, 20.25]}, "flag": True,
                          "rows": [{"a": 1}, {"a": 2}], "none": None}}
        shown = repr(probe.describe(reply))
        for secret in (VIN, "Test Data Car", "10.5", "20.25", "41.5", TS):
            self.assertNotIn(secret, shown)
        self.assertIn("LOCKED", shown)  # a status word is shown
        self.assertIn("'int'", shown)
        self.assertIn("2 items", shown)

    def test_an_all_letter_17_character_identifier_is_not_shown(self):
        self.assertEqual(probe.describe("ABCDEFGHJKLMNPRST"), "str")
        self.assertEqual(probe.describe("NO_WARNING"), "NO_WARNING")

    def test_hostile_keys_and_depth_are_contained(self):
        deep = cur = {}
        for _ in range(30):
            cur["k"] = {}
            cur = cur["k"]
        self.assertIn("...", repr(probe.describe(deep)))
        self.assertIn("<key>", repr(probe.describe({"bad key!": 1})))

    def test_identifier_like_keys_are_masked_and_field_names_kept(self):
        shown = probe.describe({VIN: 1, "ABCDEFGHJKLMNPRST": 2, "x" * 41: 3, "k1234": 4,
                                "batteryChargeLevel": 5, "tyre2": 6})
        self.assertNotIn(VIN, repr(shown))
        self.assertNotIn("ABCDEFGHJKLMNPRST", repr(shown))
        self.assertNotIn("k1234", repr(shown))
        self.assertNotIn("x" * 41, repr(shown))
        self.assertIn("batteryChargeLevel", shown)
        self.assertIn("tyre2", shown)

    def test_all_capitals_text_is_never_shown_for_place_and_route_endpoints(self):
        payload = {"data": {"destination": "TESTCITY", "state": "CLOSED"}}
        for sensitive in ("/navigation/v1/vehicles/{vin}/route", "/location/v1/vehicles/{vin}/location",
                          "/locations", "/Location/x", "/geofence", "/trips", "/connected-vehicle/v2/vehicles/{vin}",
                          "/connected-vehicle/v2/vehicles/{vin}/environment", "/anything/new"):
            self.assertFalse(probe.shows_words(sensitive), sensitive)  # words only for a known-safe list
        for plain in ("/connected-vehicle/v2/vehicles/{vin}/doors", "/energy/v2/vehicles/{vin}/state"):
            self.assertTrue(probe.shows_words(plain), plain)
        masked = repr(probe.describe(payload, words=False))
        self.assertNotIn("TESTCITY", masked)
        self.assertNotIn("CLOSED", masked)
        self.assertIn("TESTCITY", repr(probe.describe(payload)))  # the filter is the flag, not luck

    def test_only_plain_paths_are_accepted(self):
        for ok in ("/a/b-c_d/{vin}/e",):
            self.assertTrue(probe.valid_path(ok))
        for bad in ("a/b", "/a?x=1", "/a/../b", "/a b", "https://evil.example.test/x", "/a#f",
                    "//evil.example.test/x", "/a/%2e%2e/b", "/a\nb"):
            self.assertFalse(probe.valid_path(bad), bad)


class Explore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = store.Store(Path(self.tmp.name) / "test.db")
        self.addCleanup(self.db.close)

    def make(self, api, **kwargs):
        return collector.Collector(FakeManager(), self.db, API_KEY, base="https://api.example.test",
                                   vins=(VIN,), get=api, clock=Clock(), sleep=lambda s: None, **kwargs)

    def test_each_path_is_requested_once_and_reported_without_values(self):
        api = FakeApi({"/doors": (200, connected(centralLock=("LOCKED", None))), "/fuel": (403, {})})
        got = self.make(api).explore(["/connected-vehicle/v2/vehicles/{vin}/doors",
                                      "/connected-vehicle/v2/vehicles/{vin}/fuel"])
        self.assertEqual([(p, s) for p, s, _ in got],
                         [("/connected-vehicle/v2/vehicles/{vin}/doors", 200),
                          ("/connected-vehicle/v2/vehicles/{vin}/fuel", 403)])
        self.assertNotIn(VIN, repr(got))  # the placeholder is reported, never the VIN
        self.assertEqual(len(api.requests), 2)
        self.assertIn(VIN, api.requests[0][0])  # but the request itself used it

    def test_a_bad_path_is_refused_without_a_request(self):
        api = FakeApi()
        got = self.make(api).explore(["https://evil.example.test/x", "/a/../b"])
        self.assertEqual(api.requests, [])
        self.assertTrue(all(s is None for _, s, _ in got))

    def test_probing_spends_and_respects_the_budget(self):
        api = FakeApi()
        got = self.make(api, budget=2).explore(["/a/{vin}", "/b/{vin}", "/c/{vin}"])
        self.assertEqual(len(api.requests), 2)
        self.assertEqual(got[-1][2], "stopped: daily budget spent")
        self.assertEqual(self.db.calls_on(collector._iso(T0)[:10]), 2)

    def test_a_406_retry_while_probing_is_counted_too(self):
        calls = []

        def get(url, headers):
            calls.append(headers["Accept"])
            return 406, {}, {}
        got = self.make(get, budget=10).explore(["/energy/v2/vehicles/{vin}/state"])
        self.assertEqual(len(calls), len(collector.ACCEPT["energy"]))
        self.assertEqual(self.db.calls_on(collector._iso(T0)[:10]), len(calls))
        self.assertEqual(got[0][1], 406)

    def test_an_exception_is_reported_by_class_only(self):
        def boom(url, headers):
            raise ValueError("Invalid header value " + headers["Authorization"])
        got = self.make(boom).explore(["/a/{vin}"])
        self.assertEqual(got[0][2], "failed: ValueError")
        self.assertNotIn(TOKEN, repr(got))


class Redirects(unittest.TestCase):
    """Credentials must never be re-sent to another address."""

    def test_a_redirect_is_refused_and_the_target_is_never_contacted(self):
        hits = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        target = HTTPServer(("127.0.0.1", 0), Target)

        class Redirector(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/x")
                self.end_headers()

            def log_message(self, *args):
                pass

        first = HTTPServer(("127.0.0.1", 0), Redirector)
        for server in (target, first):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        direct = urllib.request.build_opener(auth._NoRedirect(), urllib.request.ProxyHandler({}))
        with mock.patch.object(auth, "_OPENER", direct):  # ignore any proxy set on this machine
            status, payload, _ = collector.http_get(
                f"http://127.0.0.1:{first.server_address[1]}/", {"Authorization": "Bearer " + TOKEN})
        self.assertEqual(status, 302)
        self.assertEqual(hits, [])

    def test_malformed_http_is_an_auth_error_not_a_crash(self):
        with mock.patch("evdash.auth._open", side_effect=http.client.BadStatusLine("x")):
            with self.assertRaises(auth.AuthError):
                collector.http_get("https://api.example.test/x", {})


class Commands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        for name in ("client_id", "client_secret", "vcc_api_key"):
            (d / name).write_text("test-data-" + name, encoding="utf-8")
        self.env = {
            "EVDASH_REDIRECT_URI": "https://dashboard.example.test/auth/callback",
            "EVDASH_SCOPES": "openid", "EVDASH_TOKEN_FILE": str(d / "tokens.json"),
            "EVDASH_DB": str(d / "t.db"), "EVDASH_CLIENT_ID_FILE": str(d / "client_id"),
            "EVDASH_CLIENT_SECRET_FILE": str(d / "client_secret"),
            "EVDASH_VCC_API_KEY_FILE": str(d / "vcc_api_key"),
        }

    def run_cli(self, *args, env=None):
        with mock.patch.dict(os.environ, env if env is not None else self.env, clear=True):
            return cli.main(list(args))

    def test_poll_runs_one_pass_and_reports_counts(self):
        summary = {"polled": 1, "stored": 2, "failed": 0, "signed_in": True, "budget_spent": False}
        with mock.patch.object(collector.Collector, "run_once", return_value=summary) as run_once:
            self.assertEqual(self.run_cli("poll"), 0)
        run_once.assert_called_once()

    def test_poll_when_signed_out_exits_nonzero(self):
        with mock.patch.object(collector.Collector, "run_once", return_value={"signed_in": False}):
            self.assertEqual(self.run_cli("poll"), 1)

    def test_collect_runs_until_stopped(self):
        with mock.patch.object(collector.Collector, "run_forever") as forever, \
                mock.patch("evdash.__main__.signal.signal") as handlers:  # leave the test run's handlers alone
            self.assertEqual(self.run_cli("collect"), 0)
        forever.assert_called_once()
        self.assertEqual(handlers.call_count, 2)  # SIGTERM and SIGINT stop it cleanly

    def test_probe_prints_shapes_and_never_the_vin_or_values(self):
        import contextlib
        import io

        def get(url, headers):  # a hostile reply: the VIN and a name as values and as a key
            return 200, {"data": {"centralLock": {"value": "LOCKED"}, "owner": "Test Data Person",
                                  "vin": VIN, VIN: {"x": 1}, "odometer": {"value": 12345}}}, {}
        out = io.StringIO()
        # no network: the collector's request method is replaced, so nothing can leave this machine
        with mock.patch.object(collector.Collector, "_request_negotiating",
                               lambda self, api, path: get(path, {})), \
                contextlib.redirect_stdout(out):
            self.assertEqual(self.run_cli("probe", "/connected-vehicle/v2/vehicles/{vin}/doors",
                                          env={**self.env, "EVDASH_VIN": VIN}), 0)
        text = out.getvalue()
        self.assertIn("HTTP 200", text)
        self.assertIn("LOCKED", text)
        for secret in (VIN, "Test Data Person", "12345", TOKEN, API_KEY):
            self.assertNotIn(secret, text)

    def test_a_refused_path_prints_its_reason_not_no_reply(self):
        import contextlib
        import io
        out = io.StringIO()
        with mock.patch.object(collector.Collector, "_vehicles", return_value=(VIN,)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(self.run_cli("probe", "https://evil.example.test/x"), 0)
        self.assertIn("refused: not a plain path", out.getvalue())

    def test_probe_without_paths_uses_the_default_list(self):
        with mock.patch.object(collector.Collector, "explore", return_value=[]) as explore:
            self.assertEqual(self.run_cli("probe"), 0)
        explore.assert_called_once_with(list(probe.DEFAULT_PATHS))

    def test_unknown_command_is_a_usage_error(self):
        self.assertEqual(self.run_cli("bogus"), 2)

    def test_poll_without_the_application_key_is_a_configuration_error(self):
        env = {k: v for k, v in self.env.items() if k != "EVDASH_VCC_API_KEY_FILE"}
        self.assertEqual(self.run_cli("poll", env=env), 2)

    def test_status_does_not_need_the_application_key(self):
        env = {k: v for k, v in self.env.items() if k != "EVDASH_VCC_API_KEY_FILE"}
        self.assertEqual(self.run_cli("status", env=env), 0)

    def test_budget_and_vin_settings_are_validated(self):
        for bad in ({"EVDASH_DAILY_BUDGET": "0"}, {"EVDASH_DAILY_BUDGET": "99999"},
                    {"EVDASH_DAILY_BUDGET": "x"}, {"EVDASH_VIN": "../etc"},
                    {"EVDASH_API_BASE": "http://insecure.example.test"}):
            with self.assertRaises(config.ConfigError, msg=str(bad)):
                config.load({**self.env, **bad})
        good = config.load({**self.env, "EVDASH_VIN": VIN})
        self.assertEqual(good.vins, (VIN,))
        self.assertFalse(good.location)  # location is off unless explicitly enabled


if __name__ == "__main__":
    unittest.main()
