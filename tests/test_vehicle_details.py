import datetime
import json
import tempfile
import unittest
from pathlib import Path

from evdash import collector, parse, store, view

# Fictitious values only: nothing here is, or resembles, a real vehicle, place or person.
VIN = "TESTDATA0000000A1"
UTC = datetime.timezone.utc


def reply(**over):
    data = {"vin": VIN, "modelYear": 1999, "externalColour": "Test Data Blue", "fuelType": "ELECTRIC",
            "gearbox": "AUTOMATIC", "batteryCapacityKWH": 50.5,
            "descriptions": {"model": "Test Model", "steering": "LEFT", "upholstery": "T1"},
            "images": {"exteriorImageUrl": "https://images.example.test/a.png"},
            "specifications": {"driveType": "AWD", "enginePowerKw": 200.0, "firstRegistrationDate": "1999-03-04T00:00:00",
                               "grossWeightKg": 2000, "numberOfSeats": 5}}
    data.update(over)
    return {"data": data}


class Parse(unittest.TestCase):
    def test_only_the_descriptive_facts_are_taken(self):
        got = parse.parse_vehicle(reply())
        self.assertEqual(got, {"model": "Test Model", "steering": "LEFT", "colour": "Test Data Blue",
                               "fuel": "ELECTRIC", "gearbox": "AUTOMATIC", "drive": "AWD", "year": 1999,
                               "power_kw": 200.0, "battery_kwh": 50.5, "seats": 5})
        flat = json.dumps(got)
        for never in (VIN, "images.example", "1999-03-04", "2000", "T1"):
            self.assertNotIn(never, flat)

    def test_odd_values_are_dropped_not_stored(self):
        got = parse.parse_vehicle(reply(modelYear="soon", externalColour="<b>x</b>", batteryCapacityKWH=float("nan"),
                                        descriptions={"model": "x" * 80}))
        self.assertEqual(sorted(got), ["drive", "fuel", "gearbox", "power_kw", "seats"])

    def test_a_reply_of_the_wrong_shape_gives_nothing(self):
        for bad in ({}, {"data": []}, {"data": "x"}, {"data": {"specifications": 5, "descriptions": []}}):
            self.assertEqual(parse.parse_vehicle(bad), {})


class Collect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = store.Store(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.db.close)

    def test_the_details_are_kept_once_as_one_small_record(self):
        def get(url, headers):
            if url.endswith("/vehicles/" + VIN):
                return 200, reply(), {}
            return 200, {}, {}
        manager = type("M", (), {"access_token": lambda self: "test-data-token",
                                 "status": lambda self: {"signed_in": True, "granted_scopes": ["openid"]}})()
        worker = collector.Collector(manager, self.db, "test-data-key", vins=(VIN,), get=get,
                                     clock=lambda: 1_000_000_000.0, sleep=lambda s: None)
        worker.run_once()
        saved = json.loads(self.db.get_meta("vehicle_info"))
        self.assertEqual(saved["model"], "Test Model")
        self.assertNotIn(VIN, self.db.get_meta("vehicle_info"))
        self.assertEqual(self.db.state(VIN, "vehicle").next_due, 1_000_000_000.0 + 86400)  # once a day


class Show(unittest.TestCase):
    INFO = parse.parse_vehicle(reply())

    def test_the_vin_hides_the_check_digit_and_the_serial_number_only(self):
        self.assertEqual(view.masked_vin(VIN), "TESTDATA•00••••••")
        self.assertEqual(len(view.masked_vin(VIN)), 17)
        shown = view.masked_vin("ABCDEFGH1JK234567")
        self.assertEqual(shown[:8] + shown[9:11], "ABCDEFGHJK")  # maker, model, year and plant stay
        for hidden in ("1", "234567"):
            self.assertNotIn(hidden, shown)

    def test_a_vin_of_the_wrong_length_shows_nothing(self):
        for bad in (None, "", "SHORT", VIN + "X", 12345):
            self.assertEqual(view.masked_vin(bad), "")

    def test_the_card_names_the_car_and_never_shows_the_full_vin(self):
        card = view.vehicle_card(self.INFO, VIN)
        for text in ("Volvo Test Model", "1999", "Test Data Blue", "AWD", "200 kW", "50.5 kWh battery", "5 seats"):
            self.assertIn(text, card)
        self.assertIn("TESTDATA", card)
        self.assertNotIn(VIN, card)
        self.assertNotIn("A1<", card)

    def test_without_details_the_card_still_shows_the_masked_vin_or_nothing(self):
        self.assertIn("VIN TESTDATA", view.vehicle_card({}, VIN))
        self.assertEqual(view.vehicle_card({}, None), "")

    def test_the_card_escapes_what_it_shows(self):
        card = view.vehicle_card({"model": "<b>x</b>", "colour": "<i>y</i>"}, VIN)
        self.assertNotIn("<b>x", card)
        self.assertNotIn("<i>y", card)

    def test_the_card_comes_first_on_the_overview(self):
        rows = [store.Latest("odometer.odometer", 100, "km", "1999-03-04T11:00:00.000000Z", "1999-03-04T11:00:00.000000Z")]
        out = view.dashboard(rows, "1999-03-04T11:00:00Z", datetime.datetime(1999, 3, 4, 12, tzinfo=UTC), UTC,
                             (self.INFO, VIN))
        self.assertLess(out.index("Volvo Test Model"), out.index("Charge and range"))
        self.assertNotIn(VIN, out)


if __name__ == "__main__":
    unittest.main()
