import datetime
import unittest

from evdash import theme, view
from evdash.store import Latest

UTC = datetime.timezone.utc
NOW = datetime.datetime(1999, 3, 4, 12, 0, tzinfo=UTC)
WHEN = "1999-03-04T11:00:00.000000Z"
OLD = "1999-03-01T11:00:00.000000Z"


def row(field, value, unit=None, ts=WHEN):
    return Latest(field, value, unit, ts, ts)


def good():
    rows = [row("energy.batteryChargeLevel", 46.0, "percentage"), row("energy.targetBatteryChargeLevel", 90, "percentage"),
            row("energy.electricRange", 157.0, "km"), row("energy.chargingStatus", "IDLE"),
            row("energy.chargerConnectionStatus", "DISCONNECTED"), row("odometer.odometer", 11907, "km"),
            row("doors.centralLock", "LOCKED"), row("engine_status.engineStatus", "STOPPED"),
            row("diagnostics.serviceWarning", "NO_WARNING"), row("diagnostics.distanceToService", 18107, "km"),
            row("diagnostics.timeToService", 4, "months"), row("diagnostics.washerFluidLevelWarning", "NO_WARNING"),
            row("brakes.brakeFluidLevelWarning", "NO_WARNING"),
            row("statistics.averageEnergyConsumptionAutomatic", 24.1, "kWh/100km"),
            row("statistics.averageSpeedAutomatic", 19, "km/h"), row("statistics.tripMeterAutomatic", 109.5, "km"),
            row("statistics.tripMeterManual", 1906.6, "km")]
    for name in ("frontLeftDoor", "frontRightDoor", "rearLeftDoor", "rearRightDoor", "hood", "tailgate", "tankLid"):
        rows.append(row(f"doors.{name}", "CLOSED"))
    for name in ("frontLeftWindow", "frontRightWindow", "rearLeftWindow", "rearRightWindow", "sunroof"):
        rows.append(row(f"windows.{name}", "CLOSED"))
    for name in ("frontLeft", "frontRight", "rearLeft", "rearRight"):
        rows.append(row(f"tyres.{name}", "NO_WARNING"))
    return rows


def page(rows, polled=WHEN):
    return view.dashboard(rows, polled, NOW, UTC)


class Tiles(unittest.TestCase):
    def test_a_healthy_car_reads_at_a_glance(self):
        out = page(good())
        for text in ("Charge and range", "Vehicle status", "Driving", "46", "Charge target 90 %", "157", "11 907",  # ui-text
                     "Not charging", "Unplugged", "Locked", "All closed", "All OK", "Next service in 18 107 km or 4 months",  # ui-text
                     "24.1", "1 906.6", "Off"):
            self.assertIn(text, out)
        self.assertNotIn('class="tile warn"', out)
        self.assertNotIn('class="tile bad"', out)

    def test_something_open_or_low_is_flagged_in_words_not_colour_alone(self):
        rows = [r for r in good() if r.field not in ("doors.tailgate", "tyres.rearLeft", "doors.centralLock")]
        rows += [row("doors.tailgate", "OPEN"), row("tyres.rearLeft", "LOW_PRESSURE"), row("doors.centralLock", "UNLOCKED")]
        out = page(rows)
        self.assertEqual(out.count('class="tile warn"'), 3)
        self.assertIn("1 to check", out)
        self.assertIn("tailgate: open", out)
        self.assertIn("rear left: low pressure", out)
        self.assertIn("Unlocked", out)

    def test_charging_shows_the_time_to_target(self):
        rows = [r for r in good() if not r.field.startswith("energy.charg")]
        rows += [row("energy.chargingStatus", "CHARGING"), row("energy.chargerConnectionStatus", "CONNECTED_AC"),
                 row("energy.estimatedChargingTimeToTargetBatteryChargeLevel", 95, "minutes")]
        out = page(rows)
        self.assertIn("Charging", out)
        self.assertIn("Plugged in", out)  # ui-text
        self.assertIn("about 95 min to target", out)

    def test_missing_fields_say_so_instead_of_vanishing(self):
        out = page([row("odometer.odometer", 100, "km")])
        self.assertIn("100", out)
        self.assertGreaterEqual(out.count("not reported yet"), 8)

    def test_low_battery_changes_the_bar_colour(self):
        low = page([row("energy.batteryChargeLevel", 8.0, "percentage")])
        self.assertIn("var(--bad)", low)
        self.assertIn("var(--good)", page([row("energy.batteryChargeLevel", 80.0, "percentage")]))

    def test_unknown_words_are_escaped(self):
        out = page([row("doors.centralLock", "<b>x</b>&")])
        self.assertNotIn("<b>x", out)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;&amp;", out)

    def test_a_stale_report_explains_why_nothing_is_new(self):
        out = page([row("odometer.odometer", 100, "km", OLD)], polled=WHEN)
        self.assertIn("while it is parked the values stay as last reported", out)
        self.assertNotIn("while it is parked", page([row("odometer.odometer", 100, "km")]))

    def test_the_raw_readings_stay_available_but_collapsed(self):
        out = page(good())
        self.assertIn("<details><summary>All readings (", out)
        self.assertNotIn("<details open", out)

    def test_location_and_the_vin_never_appear(self):
        out = page(good() + [row("location.latitude", 10.1, "deg")])
        self.assertNotIn("10.1", out)


class Frame(unittest.TestCase):
    def test_the_current_tab_is_marked_and_the_title_is_escaped(self):
        out = theme.shell("A <b>x</b>", "<p>body</p>", active="trips")
        self.assertIn("aria-current=page>Trips<", out)
        self.assertNotIn("aria-current=page>Overview<", out)
        self.assertNotIn("<b>x", out)
        self.assertIn("<p>body</p>", out)

    def test_the_trips_tab_is_hidden_when_location_is_off(self):
        self.assertNotIn(">Trips<", theme.shell("T", "", show_trips=False))

    def test_nothing_is_loaded_from_outside_and_no_script_is_used(self):
        out = theme.shell("T", "")
        self.assertNotIn("<script", out)
        self.assertNotIn("http://", out.replace("<!doctype", ""))
        self.assertNotIn("https://", out)
        self.assertNotIn("@import", out)

    def test_dark_mode_follows_the_system_setting(self):
        self.assertIn("prefers-color-scheme:dark", theme.CSS)


if __name__ == "__main__":
    unittest.main()
