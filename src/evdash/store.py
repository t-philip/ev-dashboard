"""SQLite storage: raw readings, polling state and the daily request count.

Raw readings are kept exactly as reported (one row per vehicle, field and source timestamp),
so derived views such as charging sessions can be rebuilt when the detection logic improves.
Deleting the database file deletes everything the application has stored.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Tuple

from .parse import Reading

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    vehicle TEXT NOT NULL,
    field TEXT NOT NULL,
    value,                      -- no declared type: numbers stay numbers, text stays text
    unit TEXT,
    source_ts TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (vehicle, field, source_ts)
);
CREATE INDEX IF NOT EXISTS readings_by_field ON readings (vehicle, field, source_ts);
CREATE TABLE IF NOT EXISTS vehicles (vin TEXT PRIMARY KEY, first_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS group_state (
    vehicle TEXT NOT NULL,
    grp TEXT NOT NULL,
    next_due REAL NOT NULL,
    fails INTEGER NOT NULL DEFAULT 0,
    last_http INTEGER,
    last_ok TEXT,
    PRIMARY KEY (vehicle, grp)
);
CREATE TABLE IF NOT EXISTS api_calls (day TEXT PRIMARY KEY, calls INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class GroupState(NamedTuple):
    next_due: float
    fails: int
    last_http: Optional[int]
    last_ok: Optional[str]


class Latest(NamedTuple):
    field: str
    value: object
    unit: Optional[str]
    source_ts: str
    fetched_at: str


class Store:
    def __init__(self, path, readonly: bool = False):
        self.path = Path(path)
        if readonly:
            # Raises sqlite3.OperationalError when no database exists yet.
            self._db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, timeout=10)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- readings -----------------------------------------------------------

    def add_readings(self, vehicle: str, readings: Iterable[Reading], fetched_at: str) -> int:
        """Store readings not seen before; returns how many were new."""
        before = self._db.total_changes
        try:
            self._db.executemany(
                "INSERT OR IGNORE INTO readings (vehicle, field, value, unit, source_ts, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(vehicle, r.field, r.value, r.unit, r.ts, fetched_at) for r in readings])
        except BaseException:
            self._db.rollback()  # a failed batch must not be committed by the next, unrelated write
            raise
        self._db.commit()
        return self._db.total_changes - before

    def latest(self, vehicle: str) -> List[Latest]:
        rows = self._db.execute(
            "SELECT r.field, r.value, r.unit, r.source_ts, r.fetched_at FROM readings r "
            "JOIN (SELECT field, MAX(source_ts) AS m FROM readings WHERE vehicle = ? GROUP BY field) x "
            "ON r.field = x.field AND r.source_ts = x.m WHERE r.vehicle = ? ORDER BY r.field",
            (vehicle, vehicle)).fetchall()
        return [Latest(*row) for row in rows]

    def series(self, vehicle: str, field: str) -> List[Tuple[str, object]]:
        """Every stored (source timestamp, value) of one field, oldest first."""
        return [(ts, value) for ts, value in self._db.execute(
            "SELECT source_ts, value FROM readings WHERE vehicle = ? AND field = ? ORDER BY source_ts",
            (vehicle, field))]

    def delete_older(self, field_prefix: str, cutoff_ts: str) -> int:
        """Delete readings of fields starting with ``field_prefix`` older than ``cutoff_ts``."""
        cursor = self._db.execute(
            "DELETE FROM readings WHERE substr(field, 1, ?) = ? AND source_ts < ?",
            (len(field_prefix), field_prefix, cutoff_ts))
        if cursor.rowcount:
            # Remembered in the database itself, in the same transaction, so a restart cannot lose it.
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('compact_pending', '1')")
        self._db.commit()
        return cursor.rowcount

    def compact_pending(self) -> bool:
        """True while rows have been deleted and the file has not yet been compacted."""
        row = self._db.execute("SELECT value FROM meta WHERE key = 'compact_pending'").fetchone()
        return bool(row and row[0] == "1")

    def fingerprint(self) -> Tuple[int, int]:
        """(highest row id, row count): changes whenever readings are added or deleted."""
        row = self._db.execute("SELECT COALESCE(MAX(id), 0), COUNT(*) FROM readings").fetchone()
        return row[0], row[1]

    def compact(self) -> None:
        """Rewrite the database file so deleted rows no longer sit in free pages or the log.

        VACUUM first: in write-ahead-log mode its result goes to the log, and only the checkpoint
        that follows moves it into the main file and truncates the log.
        """
        self._db.execute("VACUUM")
        row = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row and row[0] != 0:  # busy: a reader blocked it, so the old pages may still be in the file
            raise sqlite3.OperationalError("checkpoint was blocked by another connection")
        self._db.execute("DELETE FROM meta WHERE key = 'compact_pending'")
        self._db.commit()

    # --- vehicles -----------------------------------------------------------

    def vehicles(self) -> List[str]:
        return [row[0] for row in self._db.execute("SELECT vin FROM vehicles ORDER BY first_seen, vin")]

    def add_vehicle(self, vin: str, now_iso: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO vehicles (vin, first_seen) VALUES (?, ?)", (vin, now_iso))
        self._db.commit()

    # --- polling state ------------------------------------------------------

    def state(self, vehicle: str, group: str) -> Optional[GroupState]:
        row = self._db.execute(
            "SELECT next_due, fails, last_http, last_ok FROM group_state WHERE vehicle = ? AND grp = ?",
            (vehicle, group)).fetchone()
        return GroupState(*row) if row else None

    def set_state(self, vehicle: str, group: str, next_due: float, fails: int,
                  last_http: Optional[int], last_ok: Optional[str]) -> None:
        self._db.execute(
            "INSERT INTO group_state (vehicle, grp, next_due, fails, last_http, last_ok) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (vehicle, grp) DO UPDATE SET "
            "next_due = excluded.next_due, fails = excluded.fails, last_http = excluded.last_http, "
            "last_ok = COALESCE(excluded.last_ok, group_state.last_ok)",
            (vehicle, group, next_due, fails, last_http, last_ok))
        self._db.commit()

    # --- request budget -----------------------------------------------------

    def calls_on(self, day: str) -> int:
        row = self._db.execute("SELECT calls FROM api_calls WHERE day = ?", (day,)).fetchone()
        return row[0] if row else 0

    def take_call(self, day: str, budget: int) -> bool:
        """Count one request against ``day``'s budget; False (and no count) when it is spent."""
        # One statement, so two processes cannot both take the last request of the day.
        cursor = self._db.execute(
            "INSERT INTO api_calls (day, calls) VALUES (?, 1) "
            "ON CONFLICT (day) DO UPDATE SET calls = calls + 1 WHERE calls < ?", (day, budget))
        self._db.commit()
        return cursor.rowcount == 1
