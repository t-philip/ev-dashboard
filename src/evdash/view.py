"""The current-state section of the home page: the latest value of every field, and its age.

Reads the database read-only. Location is deliberately not shown here: it is an opt-in
feature with its own phase. The vehicle identification number is never shown.
"""
from __future__ import annotations

import datetime
import html
import sqlite3
from pathlib import Path
from typing import Optional

from .store import Store

NO_DATA = "<p>No vehicle data is collected yet.</p>"


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
    except sqlite3.Error:
        return NO_DATA
    finally:
        db.close()
    if not rows:
        return NO_DATA

    groups = {}
    for row in rows:
        group, _, name = row.field.partition(".")
        groups.setdefault(group, []).append((name, row))
    newest = max((_parse(r.fetched_at) for r in rows if _parse(r.fetched_at)), default=None)
    out = ["<h2>Vehicle state</h2>"]
    if newest:
        stamp = newest.isoformat()
        out.append(f"<p>Last collected {html.escape(when_text(stamp, now, tz))} "
                   f"({html.escape(age_text(stamp, now))}).</p>")
    for group in sorted(groups):
        out.append(f"<h3>{html.escape(group)}</h3><table>")
        for name, row in groups[group]:
            out.append(
                f"<tr><td><code>{html.escape(name)}</code></td>"
                f"<td>{html.escape(_shown(row.value, row.unit))}</td>"
                f"<td title='{html.escape(row.source_ts, quote=True)}'>"
                f"{html.escape(when_text(row.source_ts, now, tz))} "
                f"<span class=muted>({html.escape(age_text(row.source_ts, now))})</span></td></tr>")
        out.append("</table>")
    return "".join(out)
