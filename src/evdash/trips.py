"""Trips: the moves between consecutive stops.

The Location API reports a position only when a trip ends, and keeps no history. So a *stop* is
one reported position, and a *trip* is the move from one stop to the next: it starts where the
previous stop was and ends at the new one. The route between the two is not available and is never
guessed; only the straight-line gap is known.

For each trip the distance comes from the odometer, the start time from the vehicle's own timestamp of
a change of the engine status to RUNNING (when that change was seen), and the battery change from the
energy readings. Anything that
cannot be established from the stored readings is left empty rather than estimated.
"""
from __future__ import annotations

import bisect
import datetime
import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .store import Store

MIN_DISTANCE_KM = 0.3            # a smaller odometer change is not counted as a trip
MIN_STRAIGHT_KM = 0.5            # ...unless the two stops are at least this far apart
TOLERANCE = datetime.timedelta(minutes=45)   # how far a reading may be from a stop and still match it
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


@dataclass(frozen=True)
class Stop:
    ts: str
    lat: float
    lon: float
    heading: Optional[float]


@dataclass(frozen=True)
class Trip:
    id: str                       # derived from the end timestamp; unique per vehicle
    start: Stop
    end: Stop
    started_at: Optional[str]     # the vehicle's own time of a change to RUNNING inside the interval, if seen
    distance_km: Optional[float]  # from the odometer; None when it could not be established
    battery_start: Optional[float]
    battery_end: Optional[float]
    charged: bool                 # the car was charging between the two stops
    straight_km: float            # the straight-line gap between the two stops


def parse_ts(ts: str) -> Optional[datetime.datetime]:
    try:
        when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def trip_id(end_ts: str) -> str:
    return "".join(c for c in end_ts if c.isalnum())


class _Series:
    """A time-ordered list of readings with nearest-in-time and between-two-times lookups."""

    def __init__(self, rows: Sequence[Tuple[str, object]]):
        pairs = []
        for ts, value in rows:
            when = parse_ts(ts)
            if when is not None:
                pairs.append((when, value))
        pairs.sort(key=lambda p: p[0])
        self.times = [p[0] for p in pairs]
        self.values = [p[1] for p in pairs]

    def nearest(self, when: datetime.datetime):
        if not self.times:
            return None
        i = bisect.bisect_left(self.times, when)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.times):
                gap = abs(self.times[j] - when)
                if gap <= TOLERANCE and (best is None or gap < best[0]):
                    best = (gap, self.values[j])
        return None if best is None else best[1]

    def between(self, start: datetime.datetime, end: datetime.datetime):
        """(time, value) pairs with start < time <= end."""
        lo = bisect.bisect_right(self.times, start)
        hi = bisect.bisect_right(self.times, end)
        return list(zip(self.times[lo:hi], self.values[lo:hi]))


def _number(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) else None


def stops_from(latitudes, longitudes, headings=()) -> List[Stop]:
    """Pair latitude and longitude readings that share a timestamp into stops, oldest first."""
    lon_by_ts = {ts: v for ts, v in longitudes}
    heading_by_ts = {ts: v for ts, v in headings}
    out = []
    for ts, lat in latitudes:
        lon = lon_by_ts.get(ts)
        lat_f, lon_f = _number(lat), _number(lon)
        if lat_f is None or lon_f is None or not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
            continue
        if parse_ts(ts) is None:
            continue
        out.append(Stop(ts, lat_f, lon_f, _number(heading_by_ts.get(ts))))
    out.sort(key=lambda s: parse_ts(s.ts))
    return out


def detect(stops: Sequence[Stop], odometer, battery, charging, engine) -> List[Trip]:
    """Trips between consecutive stops, oldest first. The four series are lists of (timestamp, value)."""
    odo, bat, chg, eng = _Series(odometer), _Series(battery), _Series(charging), _Series(engine)
    trips = []
    for prev, cur in zip(stops, stops[1:]):
        t_prev, t_cur = parse_ts(prev.ts), parse_ts(cur.ts)
        if t_prev is None or t_cur is None or t_cur <= t_prev:
            continue
        straight = haversine_km(prev.lat, prev.lon, cur.lat, cur.lon)
        a, b = _number(odo.nearest(t_prev)), _number(odo.nearest(t_cur))
        distance = b - a if a is not None and b is not None else None
        if distance is not None and distance < MIN_DISTANCE_KM:
            distance = None  # no movement recorded, or a stale odometer: do not show a false zero
        if distance is None and straight < MIN_STRAIGHT_KM:
            continue  # the same place reported again: not a trip
        started = [t for t, v in eng.between(t_prev, t_cur) if v == "RUNNING"]
        start_b, end_b = _number(bat.nearest(t_prev)), _number(bat.nearest(t_cur))
        charged = any(v == "CHARGING" for _, v in chg.between(t_prev, t_cur)) or (
            start_b is not None and end_b is not None and end_b > start_b + 1)
        trips.append(Trip(
            id=trip_id(cur.ts), start=prev, end=cur,
            started_at=min(started).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if started else None,
            distance_km=distance, battery_start=start_b, battery_end=end_b,
            charged=charged, straight_km=straight))
    return trips


def load(store: Store, vehicle: str) -> Tuple[List[Stop], List[Trip]]:
    """All stops and trips stored for a vehicle, oldest first."""
    stops = stops_from(store.series(vehicle, "location.latitude"),
                       store.series(vehicle, "location.longitude"),
                       store.series(vehicle, "location.heading"))
    trips = detect(stops, store.series(vehicle, "odometer.odometer"),
                   store.series(vehicle, "energy.batteryChargeLevel"),
                   store.series(vehicle, "energy.chargingStatus"),
                   store.series(vehicle, "engine_status.engineStatus"))
    return stops, trips
