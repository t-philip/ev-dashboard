"""Pages for browsing past trips: a month at a time, and one trip in detail.

Read-only. Maps are drawn by ``mapview`` with no external service; the one link that leaves the
machine is an explicit "open in OpenStreetMap" link on the detail page, which does nothing until
the owner clicks it.
"""
from __future__ import annotations

import datetime
import html
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import mapview
from . import trips as trips_mod
from .store import Store

STYLE = (
    ":root{color-scheme:light dark}body{font:16px/1.6 system-ui,sans-serif;max-width:46rem;"
    "margin:2rem auto;padding:0 16px}code{font-size:.9em}table{border-collapse:collapse;width:100%}"
    "th,td{padding:.2rem .8rem .2rem 0;text-align:left;vertical-align:top}h2{margin-top:1.6rem}"
    ".muted{opacity:.7}nav a{margin-right:1rem}")
_MONTH = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")
_TRIP_ID = re.compile(r"[0-9A-Za-z]{8,40}")
OFF = "<p>Location is turned off, so there are no trips to show.</p>"
EMPTY = ("<p>No trips yet. A trip appears once the car's position has been reported at least twice, "
         "so location collection must be on and the location scope granted. History starts the day "
         "collection starts.</p>")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def shell(title: str, body: str) -> str:
    return ("<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style>"
            "<nav><a href=/>Home</a><a href=/trips>Trips</a></nav>"
            f"<h1>{html.escape(title)}</h1>{body}</html>")


def local(ts: str, tz: datetime.tzinfo) -> Optional[datetime.datetime]:
    when = trips_mod.parse_ts(ts)
    if when is None:
        return None
    try:
        return when.astimezone(tz)
    except (ValueError, OverflowError):  # a timestamp at the edge of the calendar
        return None


def _when(ts: Optional[str], tz) -> str:
    when = local(ts, tz) if ts else None
    if not when:
        return "not recorded"
    return f"{_DAYS[when.weekday()]} {when.day:02d} {_MONTHS[when.month - 1]} {when.year} {when:%H:%M}"


def _km(value: Optional[float]) -> str:
    return "unknown" if value is None else f"{value:.1f} km"


def _battery(trip: trips_mod.Trip) -> str:
    if trip.battery_start is None or trip.battery_end is None:
        return "unknown"
    text = f"{trip.battery_start:g}% to {trip.battery_end:g}%"
    return text + (" (charged in between)" if trip.charged else "")


def _coords(lat: float, lon: float, places: int) -> str:
    return f"{lat:.{places}f}, {lon:.{places}f}"


def _month_of(trip: trips_mod.Trip, tz) -> str:
    when = local(trip.end.ts, tz)
    return when.strftime("%Y-%m") if when else ""


_CACHE: Dict[str, object] = {"key": None, "value": None}
_LOCK = threading.Lock()


def _load(db_path: Optional[Path]) -> Optional[Tuple[List[trips_mod.Stop], List[trips_mod.Trip]]]:
    """Stops and trips, recomputed only when the stored readings have changed.

    Every request is unauthenticated, so the computation must not be repeated per request. The cache
    key comes from the data itself (highest row id and row count), not from file timestamps, which a
    read-only connection to a write-ahead-log database can change.
    """
    if db_path is None:
        return None
    try:
        db = Store(db_path, readonly=True)
    except sqlite3.OperationalError:
        return None
    try:
        try:
            st = os.stat(db_path)
            identity = (st.st_ino, st.st_dev)  # a replaced file is a different database
        except OSError:
            identity = None
        key = (str(db_path), identity, db.fingerprint())
        with _LOCK:
            if _CACHE["key"] != key:
                vins = db.vehicles()
                # the first vehicle only: one owner, one car
                _CACHE["value"] = trips_mod.load(db, vins[0]) if vins else None
                _CACHE["key"] = key
            return _CACHE["value"]
    except sqlite3.Error:
        return None
    finally:
        db.close()


def render_list(db_path: Optional[Path], query: Dict[str, List[str]], tz: datetime.tzinfo) -> Tuple[int, str]:
    loaded = _load(db_path)
    trips = loaded[1] if loaded else []
    if not trips:
        return 200, shell("Trips", EMPTY)
    months = sorted({m for m in (_month_of(t, tz) for t in trips) if m})
    asked = (query.get("month") or [""])[0]
    month = asked if _MONTH.fullmatch(asked) and asked in months else months[-1]
    shown = [t for t in trips if _month_of(t, tz) == month]
    idx = months.index(month)

    nav = []
    if idx > 0:
        nav.append(f"<a href='/trips?month={months[idx - 1]}'>&larr; {months[idx - 1]}</a>")
    nav.append(f"<strong>{month}</strong>")
    if idx < len(months) - 1:
        nav.append(f"<a href='/trips?month={months[idx + 1]}'>{months[idx + 1]} &rarr;</a>")
    others = " ".join(f"<a href='/trips?month={m}'>{m}</a>" for m in reversed(months[-24:]) if m != month)

    known = [t.distance_km for t in shown if t.distance_km is not None]
    points: List[mapview.Point] = []
    seen = set()
    lines = []
    for t in shown:
        for stop in (t.start, t.end):
            if stop.ts not in seen:
                seen.add(stop.ts)
                points.append((stop.lat, stop.lon, _when(stop.ts, tz), "stop"))
        lines.append(((t.start.lat, t.start.lon), (t.end.lat, t.end.lon)))
    rows = []
    for t in reversed(shown):
        rows.append(
            f"<tr><td><a href='/trips/{html.escape(t.id)}'>{html.escape(_when(t.end.ts, tz))}</a></td>"
            f"<td>{html.escape(_km(t.distance_km))}</td>"
            f"<td>{html.escape(_coords(t.start.lat, t.start.lon, 3))}</td>"
            f"<td>{html.escape(_coords(t.end.lat, t.end.lon, 3))}</td>"
            f"<td>{html.escape(_battery(t))}</td></tr>")
    body = (
        f"<p>{' '.join(nav)}</p>"
        f"<p>{len(shown)} {'trip' if len(shown) == 1 else 'trips'}, {sum(known):.0f} km where the distance is known.</p>"
        f"{mapview.render(points, lines)}"
        "<p class=muted>Stops drawn to scale on a plain background. Dashed lines join each trip's two "
        "stops in a straight line: they are <strong>not</strong> the routes driven.</p>"
        "<table><tr><th>Ended</th><th>Distance</th><th>From</th><th>To</th><th>Battery</th></tr>"
        f"{''.join(rows)}</table>"
        + (f"<p class=muted>Other months: {others}</p>" if others else ""))
    return 200, shell("Trips", body)


def render_detail(db_path: Optional[Path], trip_id: str, tz: datetime.tzinfo) -> Tuple[int, str]:
    if not _TRIP_ID.fullmatch(trip_id or ""):
        return 404, shell("Trip not found", "<p>No such trip.</p>")
    loaded = _load(db_path)
    trips = loaded[1] if loaded else []
    pos = next((i for i, t in enumerate(trips) if t.id == trip_id), None)
    if pos is None:
        return 404, shell("Trip not found", "<p>No such trip.</p>")
    t = trips[pos]
    started = trips_mod.parse_ts(t.started_at) if t.started_at else None
    ended = trips_mod.parse_ts(t.end.ts)
    duration = (f"{round((ended - started).total_seconds() / 60)} min"
                if started and ended and ended > started else "not recorded")
    rows = [
        ("Ended", _when(t.end.ts, tz)),
        ("Started", _when(t.started_at, tz) if t.started_at else "start time not recorded"),
        ("Time spent driving", duration),
        ("Distance (odometer)", _km(t.distance_km)),
        ("Straight line between the stops", f"{t.straight_km:.1f} km"),
        ("Battery", _battery(t)),
        ("From", _coords(t.start.lat, t.start.lon, 4)),
        ("To", _coords(t.end.lat, t.end.lon, 4)),
    ]
    table = "<table>" + "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in rows) + "</table>"
    pts = [(t.start.lat, t.start.lon, "Start: " + _when(t.start.ts, tz), "start"),
           (t.end.lat, t.end.lon, "End: " + _when(t.end.ts, tz), "end")]
    osm = lambda s: (f"https://www.openstreetmap.org/?mlat={s.lat:.5f}&amp;mlon={s.lon:.5f}"
                     f"#map=16/{s.lat:.5f}/{s.lon:.5f}")
    nav = []
    if pos > 0:
        nav.append(f"<a href='/trips/{html.escape(trips[pos - 1].id)}'>&larr; earlier trip</a>")
    month = _month_of(t, tz)
    nav.append(f"<a href='/trips?month={month}'>back to {month}</a>")
    if pos < len(trips) - 1:
        nav.append(f"<a href='/trips/{html.escape(trips[pos + 1].id)}'>later trip &rarr;</a>")
    body = (
        f"<p>{' '.join(nav)}</p>{table}"
        f"{mapview.render(pts, [((t.start.lat, t.start.lon), (t.end.lat, t.end.lon))])}"
        "<p class=muted>Hollow circle: start. Filled circle: end. The dashed line is a straight line "
        "between the two stops: it is <strong>not</strong> the route driven, which the manufacturer "
        "does not provide.</p>"
        f"<p>Show on a map: <a href='{osm(t.start)}' rel='noreferrer noopener'>start</a> &middot; "
        f"<a href='{osm(t.end)}' rel='noreferrer noopener'>end</a></p>"
        "<p class=muted>Those links open openstreetmap.org in your browser, which then receives the "
        "coordinates. Nothing is sent unless you click.</p>")
    return 200, shell(f"Trip, {_when(t.end.ts, tz)}", body)
