"""Turn the vehicle APIs' replies into plain readings.

Every field is optional: a vehicle reports only what it supports, and a field may come back
null, as an error object, or as "UNSPECIFIED". Those are dropped, never stored as a value.
Nothing here trusts the reply's shape: anything unexpected is skipped.
"""
from __future__ import annotations

import datetime
import math
import re
from typing import Any, List, NamedTuple, Optional

_NAME = re.compile(r"[A-Za-z0-9_]{1,80}")
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT[0-9:.]{5,20}(?:Z|[+-]\d\d:\d\d)?")
_UNIT = re.compile(r"[A-Za-z0-9_%/.\- ]{1,24}")
_NO_DATA = {"UNSPECIFIED"}


class Reading(NamedTuple):
    field: str  # "<group>.<name>", for example "energy.batteryChargeLevel"
    value: Any  # a number or a short string
    unit: Optional[str]
    ts: str  # the vehicle's own timestamp for this value


def _value(raw: Any) -> Any:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):  # stays within what SQLite can hold
        return raw if -(2 ** 63) <= raw < 2 ** 63 else None
    if isinstance(raw, float):
        return raw if math.isfinite(raw) else None
    if isinstance(raw, str) and 0 < len(raw) <= 200 and raw.isprintable() and raw not in _NO_DATA:
        return raw
    return None


def _normalise_ts(ts: Any) -> Optional[str]:
    """UTC with a fixed-width fraction, so timestamps sort correctly as text."""
    if not isinstance(ts, str) or not _TIMESTAMP.fullmatch(ts):
        return None
    try:
        when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (ValueError, OverflowError):
        return None


def _reading(group: str, name: Any, raw: Any, unit: Any, ts: Any) -> Optional[Reading]:
    value = _value(raw)
    if value is None or not isinstance(name, str) or not _NAME.fullmatch(name):
        return None
    ts = _normalise_ts(ts)
    if ts is None:
        return None
    clean_unit = unit if isinstance(unit, str) and _UNIT.fullmatch(unit) else None
    return Reading(f"{group}.{name}", value, clean_unit, ts)


def parse_connected(group: str, payload: dict) -> List[Reading]:
    """Connected Vehicle API: ``{"data": {field: {"value", "unit", "timestamp"}}}``."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    out = []
    for name, item in data.items():
        if isinstance(item, dict):
            reading = _reading(group, name, item.get("value"), item.get("unit"), item.get("timestamp"))
            if reading:
                out.append(reading)
    return out


def parse_energy(group: str, payload: dict) -> List[Reading]:
    """Energy API: ``{field: {"status", "value", "unit", "updatedAt"}}`` (an error object has a code)."""
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []
    out = []
    for name, item in data.items():
        if isinstance(item, dict) and "code" not in item:
            reading = _reading(group, name, item.get("value"), item.get("unit"), item.get("updatedAt"))
            if reading:
                out.append(reading)
    return out


def parse_location(group: str, payload: dict) -> List[Reading]:
    """Location API: a GeoJSON point (longitude, latitude[, altitude]) and a heading."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    geometry = data.get("geometry") if isinstance(data.get("geometry"), dict) else {}
    props = data.get("properties") if isinstance(data.get("properties"), dict) else {}
    coords = geometry.get("coordinates")
    ts = props.get("timestamp")
    out = []
    if isinstance(coords, list) and len(coords) >= 2:
        for name, raw, unit in (("longitude", coords[0], "deg"), ("latitude", coords[1], "deg"),
                                ("altitude", coords[2] if len(coords) > 2 else None, "m")):
            reading = _reading(group, name, raw, unit, ts)
            if reading:
                out.append(reading)
    heading = props.get("heading")
    if isinstance(heading, str) and heading.replace(".", "", 1).isdigit():
        heading = float(heading)
    reading = _reading(group, "heading", heading, "deg", ts)
    if reading:
        out.append(reading)
    return out


def parse_vehicle_list(payload: dict) -> List[str]:
    """VINs from the vehicle list. A VIN is 17 characters, never I, O or Q."""
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    vins = [item.get("vin") for item in data if isinstance(item, dict)]
    return [v for v in vins if isinstance(v, str) and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", v)]
