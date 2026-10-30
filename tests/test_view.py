import datetime
import tempfile
import unittest
from pathlib import Path

from evdash import parse, store, view

# Fictitious values only.
VIN = "TESTDATA0000000A1"
NOW = datetime.datetime(1999, 3, 4, 6, 0, 0, tzinfo=datetime.timezone.utc)


class AgeText(unittest.TestCase):
    def test_ages_are_readable(self):
        cases = {"1999-03-04T05:59:30Z": "just now", "1999-03-04T05:50:00Z": "10 min ago",
                 "1999-03-04T03:00:00Z": "3 h ago", "1999-03-01T06:00:00Z": "3 d ago",
                 "1999-03-04T05:59:30.123Z": "just now", "garbage": "unknown"}
        for ts, expected in cases.items():
            self.assertEqual(view.age_text(ts, NOW), expected, ts)

    def test_a_future_timestamp_is_not_negative(self):
        self.assertEqual(view.age_text("1999-03-05T00:00:00Z", NOW), "just now")


class WhenText(unittest.TestCase):
    def test_24_hour_clock_with_the_day_and_the_zone(self):
        cases = {
            "1999-03-04T14:05:00Z": "today 14:05 UTC",          # afternoon is 14:05, never 2:05 PM
            "1999-03-04T00:07:00Z": "today 00:07 UTC",
            "1999-03-03T23:30:00Z": "yesterday 23:30 UTC",
            "1999-03-01T08:00:00Z": "Mon 01 Mar 08:00 UTC",
            "1998-12-31T08:00:00Z": "Thu 31 Dec 1998 08:00 UTC",
            "garbage": "unknown",
        }
        now = datetime.datetime(1999, 3, 4, 15, 0, tzinfo=datetime.timezone.utc)
        for ts, expected in cases.items():
            self.assertEqual(view.when_text(ts, now, datetime.timezone.utc), expected, ts)

    def test_the_day_and_hour_follow_the_time_zone(self):
        plus_2 = datetime.timezone(datetime.timedelta(hours=2), "TST")
        now = datetime.datetime(1999, 3, 4, 15, 0, tzinfo=datetime.timezone.utc)   # 17:00 local
        self.assertEqual(view.when_text("1999-03-03T10:00:00Z", now, plus_2), "yesterday 12:00 TST")
        self.assertEqual(view.when_text("1999-03-04T22:30:00Z", now, plus_2), "Fri 05 Mar 00:30 TST")  # after midnight there
        self.assertEqual(view.when_text("1999-03-03T22:30:00Z", now, plus_2), "today 00:30 TST")
        self.assertEqual(view.when_text("1999-03-04T12:00:00Z", now, plus_2), "today 14:00 TST")

    def test_a_timestamp_at_the_edge_of_the_calendar_is_unknown_not_an_error(self):
        minus_14 = datetime.timezone(datetime.timedelta(hours=-14))
        now = datetime.datetime(1999, 3, 4, 15, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(view.when_text("0001-01-01T00:00:00Z", now, minus_14), "unknown")


class Render(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.db"

    def fill(self, readings):
        db = store.Store(self.path)
        db.add_vehicle(VIN, "1999-03-04T05:00:00Z")
        db.add_readings(VIN, readings, "1999-03-04T05:55:00Z")
        db.close()

    def test_without_a_database_it_says_nothing_is_collected(self):
        self.assertEqual(view.render(self.path, NOW), view.NO_DATA)
        self.assertEqual(view.render(None, NOW), view.NO_DATA)

    def test_an_empty_database_says_nothing_is_collected(self):
        store.Store(self.path).close()
        self.assertEqual(view.render(self.path, NOW), view.NO_DATA)

    def test_latest_values_are_grouped_and_aged(self):
        self.fill([parse.Reading("energy.batteryChargeLevel", 80.04, "percentage", "1999-03-04T05:50:00Z"),
                   parse.Reading("energy.batteryChargeLevel", 70.0, "percentage", "1999-03-04T05:00:00Z"),
                   parse.Reading("doors.centralLock", "LOCKED", None, "1999-03-04T03:00:00Z")])
        page = view.render(self.path, NOW)
        self.assertIn("<h3>energy</h3>", page)
        self.assertIn("80 percentage", page)          # the newer reading, rounded
        self.assertNotIn("70 percentage", page)
        self.assertIn("10 min ago", page)
        self.assertIn("LOCKED", page)
        self.assertIn("3 h ago", page)
        self.assertIn("Last collected today 05:55 UTC (5 min ago)", page)
        self.assertIn("today 05:50 UTC", page)       # each value says when, not just how long ago
        self.assertIn("today 03:00 UTC", page)

    def test_location_and_the_vin_are_never_shown(self):
        self.fill([parse.Reading("location.latitude", 20.25, "deg", "1999-03-04T05:50:00Z"),
                   parse.Reading("odometer.odometer", 1234, "km", "1999-03-04T05:50:00Z")])
        page = view.render(self.path, NOW)
        self.assertNotIn("location", page)
        self.assertNotIn("20.25", page)
        self.assertNotIn(VIN, page)
        self.assertIn("1234 km", page)

    def test_only_location_stored_counts_as_no_data(self):
        self.fill([parse.Reading("location.latitude", 20.25, "deg", "1999-03-04T05:50:00Z")])
        self.assertEqual(view.render(self.path, NOW), view.NO_DATA)

    def test_values_are_escaped(self):
        self.fill([parse.Reading("doors.x", "<b>&", None, "1999-03-04T05:50:00Z")])
        page = view.render(self.path, NOW)
        self.assertNotIn("<b>", page)
        self.assertIn("&lt;b&gt;&amp;", page)


if __name__ == "__main__":
    unittest.main()
