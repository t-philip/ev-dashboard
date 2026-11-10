"""The current-state section of the home page: the latest value of every field, and its age.

Reads the database read-only. Location is deliberately not shown here: it is an opt-in
feature with its own phase. The vehicle identification number is never shown.
"""
from __future__ import annotations

import datetime
import html
import re
import sqlite3
from pathlib import Path
from typing import Optional

from .store import Store

NO_DATA = '<div class="card"><p>No vehicle data is collected yet.</p></div>'


def _parse(ts: str) -> Optional[datetime.datetime]:
    try:
        when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)


def age_text(ts: str, now: datetime.datetime) -> str:
    when = _parse(ts)
    if when is None:
        return "unknown"
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} d ago"


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def when_text(ts: str, now: datetime.datetime, tz: datetime.tzinfo) -> str:
    """The moment itself, 24-hour, in the owner's time zone: "today 14:05 CEST", "Thu 30 Oct 14:05 CEST".

    A relative age ("5 min ago") says nothing about *which* moment once the page has been open a while.
    """
    when = _parse(ts)
    if when is None:
        return "unknown"
    try:
        local, today = when.astimezone(tz), now.astimezone(tz).date()
    except (ValueError, OverflowError):
        return "unknown"
    if local.date() == today:
        day = "today"
    elif local.date() == today - datetime.timedelta(days=1):
        day = "yesterday"
    else:
        day = f"{_DAYS[local.weekday()]} {local.day:02d} {_MONTHS[local.month - 1]}"
        if local.year != today.year:
            day += f" {local.year}"
    return f"{day} {local:%H:%M} {local.tzname() or ''}".strip()


def _shown(value, unit) -> str:
    if isinstance(value, float):
        value = round(value, 1)
        value = int(value) if value == int(value) else value
    return f"{value} {unit}" if unit else str(value)


def render(db_path: Optional[Path], now: Optional[datetime.datetime] = None,
           tz: datetime.tzinfo = datetime.timezone.utc) -> str:
    """HTML for the current-state section; a plain notice when nothing is stored yet."""
    if db_path is None:
        return NO_DATA
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        db = Store(db_path, readonly=True)
    except sqlite3.OperationalError:
        return NO_DATA
    try:
        vins = db.vehicles()
        rows = [r for r in db.latest(vins[0]) if not r.field.startswith("location.")] if vins else []
        polled = db.last_poll(vins[0]) if vins else None
    except sqlite3.Error:
        return NO_DATA
    finally:
        db.close()
    if not rows:
        return NO_DATA

    if polled is None or _parse(polled) is None:  # no usable poll record: the newest stored value stands in
        stored = max((_parse(r.fetched_at) for r in rows if _parse(r.fetched_at)), default=None)
        polled = stored.isoformat() if stored else None
    return dashboard(rows, polled, now, tz)


# -- the dashboard: what matters first, the raw readings last ------------------------------------

_GOOD_WORDS = {"NO_WARNING", "CLOSED", "LOCKED", "UNSPECIFIED"}


def _words(name: str) -> str:
    """frontLeftDoor -> front left door."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name).lower()


def _plain(value) -> str:
    return str(value).lower().replace("_", " ")


def _number(value, places: int = 0) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return html.escape(str(value))
    return f"{number:,.{places}f}".replace(",", " ")


def _tile(label: str, value: str, unit: str = "", note: str = "", kind: str = "", extra: str = "",
          small: bool = False) -> str:
    unit_html = f'<span class="unit">{html.escape(unit)}</span>' if unit else ""
    note_html = f'<div class="note">{note}</div>' if note else ""
    size = " small" if small else ""
    return (f'<div class="tile {kind}"><div class="label">{html.escape(label)}</div>'
            f'<div class="value{size}">{value}{unit_html}</div>{extra}{note_html}</div>')


def _status_tile(label: str, kind: str, text: str, note: str = "") -> str:
    note_html = f'<div class="note">{note}</div>' if note else ""
    text = text[:1].upper() + text[1:]
    return (f'<div class="tile {kind}"><div class="label">{html.escape(label)}</div>'
            f'<div class="state"><span class="dot {kind}"></span><span>{html.escape(text)}</span></div>'
            f'{note_html}</div>')


def _missing(label: str) -> str:
    return _tile(label, '<span class="muted">&mdash;</span>', note="not reported yet")  # ui-text


def _group_status(latest: dict, group: str, names, label: str, ok_text: str) -> str:
    found = [(n, latest[f"{group}.{n}"].value) for n in names if f"{group}.{n}" in latest]
    if not found:
        return _missing(label)
    off = [(n, v) for n, v in found if str(v).upper() not in _GOOD_WORDS]
    if not off:
        return _status_tile(label, "good", ok_text)
    listed = ", ".join(f"{_words(n)}: {_plain(v)}" for n, v in off)
    return _status_tile(label, "warn", f"{len(off)} to check", html.escape(listed))


def _single_status(latest: dict, key: str, label: str, ok_text: str, note: str = "") -> str:
    row = latest.get(key)
    if row is None:
        return _missing(label)
    if str(row.value).upper() in _GOOD_WORDS:
        return _status_tile(label, "good", ok_text, note)
    return _status_tile(label, "warn", _plain(row.value), note)


def _float(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _val(latest: dict, key: str):
    row = latest.get(key)
    return None if row is None else row.value


def _numeric_tile(latest: dict, key: str, label: str, unit: str, places: int = 0, note: str = "") -> str:
    value = _val(latest, key)
    if value is None:
        return _missing(label)
    return _tile(label, _number(value, places), unit, note)


def dashboard(rows, polled: Optional[str], now: datetime.datetime, tz: datetime.tzinfo) -> str:
    rows = [r for r in rows if not r.field.startswith("location.")]  # positions never appear on this page
    latest = {r.field: r for r in rows}
    out = []
    # The manufacturer stamps this one field with the time of each request, not of the car's report,
    # so it would make a parked car look freshly heard from on every poll.
    reported = max((r.source_ts for r in rows
                    if _parse(r.source_ts) and r.field != "statistics.averageEnergyConsumptionAutomatic"),
                   key=_parse, default=None)
    fresh = []
    if polled:
        fresh.append(f"Last poll {html.escape(when_text(polled, now, tz))} ({html.escape(age_text(polled, now))}).")
    if reported:
        fresh.append(f" Newest report from the car {html.escape(when_text(reported, now, tz))} "
                     f"({html.escape(age_text(reported, now))}).")
        when = _parse(reported)
        if when is not None and (now - when).total_seconds() > 6 * 3600:
            fresh.append(' <span class="muted">The car sends updates when it is used or charging; '
                         "while it is parked the values stay as last reported.</span>")
    if fresh:
        out.append(f'<p class="fresh">{"".join(fresh)}</p>')

    # -- charge and range
    pct = _float(_val(latest, "energy.batteryChargeLevel"))
    target = _float(_val(latest, "energy.targetBatteryChargeLevel"))
    if pct is None:
        battery = _missing("Battery")
    else:
        level = max(0.0, min(100.0, float(pct)))
        colour = "var(--good)" if level >= 25 else "var(--warn)" if level >= 12 else "var(--bad)"
        mark = ""
        note = ""
        if target is not None:
            mark = f'<b style="left:{max(0.0, min(100.0, float(target))):.0f}%" title="Charge target"></b>'  # ui-text
            note = f"Charge target {_number(target)} %"
        battery = _tile("Battery", _number(pct), "%", note,
                        extra=f'<div class="bar"><i style="width:{level:.0f}%;background:{colour}"></i>{mark}</div>')
    reach = "energy.electricRange" if "energy.electricRange" in latest else "statistics.distanceToEmptyBattery"
    charging = _val(latest, "energy.chargingStatus")
    plug = _val(latest, "energy.chargerConnectionStatus")
    if charging is None and plug is None:
        charge_tile = _missing("Charging")
    else:
        names = {"CHARGING": "Charging", "IDLE": "Not charging", "DONE": "Charging done"}  # ui-text
        text = names.get(str(charging).upper(), _plain(charging) if charging else "unknown")
        if plug is None:
            plugged = ""
        elif str(plug).upper() == "DISCONNECTED":
            plugged = "Unplugged"
        elif "CONNECTED" in str(plug).upper():
            plugged = "Plugged in"  # ui-text
        else:
            plugged = _plain(plug)
        kind = "good" if str(charging).upper() == "CHARGING" else ""
        eta = _val(latest, "energy.estimatedChargingTimeToTargetBatteryChargeLevel")
        note = html.escape(plugged)
        if str(charging).upper() == "CHARGING" and eta is not None:
            note += f" &middot; about {_number(eta)} min to target"
        charge_tile = _tile("Charging", html.escape(text), note=note, kind=kind, small=True)
    out.append('<h2 class="sec">Charge and range</h2><div class="tiles">'
               + battery
               + _numeric_tile(latest, reach, "Range", "km")
               + charge_tile
               + _numeric_tile(latest, "odometer.odometer", "Odometer", "km")
               + "</div>")

    # -- vehicle status
    door_names = ("frontLeftDoor", "frontRightDoor", "rearLeftDoor", "rearRightDoor", "hood", "tailgate", "tankLid")
    window_names = ("frontLeftWindow", "frontRightWindow", "rearLeftWindow", "rearRightWindow", "sunroof")
    tyre_names = ("frontLeft", "frontRight", "rearLeft", "rearRight")
    lock = latest.get("doors.centralLock")
    if lock is None:
        lock_tile = _missing("Lock")
    elif str(lock.value).upper() == "LOCKED":
        lock_tile = _status_tile("Lock", "good", "Locked")
    else:
        lock_tile = _status_tile("Lock", "warn", _plain(lock.value))
    left = []
    if "diagnostics.distanceToService" in latest:
        left.append(f"{_number(latest['diagnostics.distanceToService'].value)} km")
    if "diagnostics.timeToService" in latest:
        left.append(f"{_number(latest['diagnostics.timeToService'].value)} months")
    service_note = ("Next service in " + " or ".join(left)) if left else ""
    service = _single_status(latest, "diagnostics.serviceWarning", "Service", "OK", service_note)
    power = _val(latest, "engine_status.engineStatus")
    if power is None:
        power_tile = _missing("Power")
    else:
        power_tile = _status_tile("Power", "", {"STOPPED": "Off", "RUNNING": "On"}.get(
            str(power).upper(), _plain(power)))
    out.append('<h2 class="sec">Vehicle status</h2><div class="tiles">'
               + lock_tile
               + _group_status(latest, "doors", door_names, "Doors and hatches", "All closed")  # ui-text
               + _group_status(latest, "windows", window_names, "Windows", "All closed")  # ui-text
               + _group_status(latest, "tyres", tyre_names, "Tyre pressure", "All OK")  # ui-text
               + _single_status(latest, "brakes.brakeFluidLevelWarning", "Brake fluid", "OK")  # ui-text
               + _single_status(latest, "diagnostics.washerFluidLevelWarning", "Washer fluid", "OK")  # ui-text
               + service
               + power_tile
               + "</div>")

    # -- driving
    out.append('<h2 class="sec">Driving</h2><div class="tiles">'
               + _numeric_tile(latest, "statistics.averageEnergyConsumptionAutomatic", "Average consumption",  # ui-text
                               "kWh/100 km", 1)
               + _numeric_tile(latest, "statistics.averageSpeedAutomatic", "Average speed", "km/h")  # ui-text
               + _numeric_tile(latest, "statistics.tripMeterAutomatic", "Trip meter A", "km", 1,
                               "resets automatically")
               + _numeric_tile(latest, "statistics.tripMeterManual", "Trip meter B", "km", 1, "reset by hand")
               + "</div>")

    # -- the raw readings, for completeness
    groups = {}
    for row in rows:
        group, _, name = row.field.partition(".")
        groups.setdefault(group, []).append((name, row))
    raw = []
    for group in sorted(groups):
        raw.append(f"<h3>{html.escape(group)}</h3><table>")
        for name, row in groups[group]:
            raw.append(
                f"<tr><td><code>{html.escape(name)}</code></td>"
                f"<td>{html.escape(_shown(row.value, row.unit))}</td>"
                f"<td title='{html.escape(row.source_ts, quote=True)}'>"
                f"{html.escape(when_text(row.source_ts, now, tz))} "
                f"<span class=muted>({html.escape(age_text(row.source_ts, now))})</span></td></tr>")
        raw.append("</table>")
    out.append(f'<details><summary>All readings ({len(rows)})</summary><div class="inner">'
               + "".join(raw) + "</div></details>")
    return "".join(out)
