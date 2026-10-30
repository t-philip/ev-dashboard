"""The collector: polls the vehicle APIs on a schedule and records what they report.

The APIs return only the latest value of each field, so history exists only because this
process writes it down. It never exceeds a configured daily request budget, spaces its requests
out, and backs off after errors instead of retrying in a tight loop.

The endpoint paths and Accept headers below follow the manufacturer's published API layout and
have not yet been confirmed against a live account (see docs/DESIGN_SPEC.md section 10). A wrong
one normally shows as a non-200 status in the log; a 200 reply that yields no readings is logged
as a warning too. Only 200 replies are parsed.
"""
from __future__ import annotations

import datetime
import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from . import auth, parse
from .store import Store

log = logging.getLogger("evdash.collector")

DEFAULT_BASE = "https://api.volvocars.com"
DEFAULT_BUDGET = 2000          # requests per UTC day (the provider's documented limit is higher;
                               # its reset time is assumed to be UTC midnight, which is unconfirmed)
MAX_BUDGET = 9000              # a configured budget above this is refused
MIN_GAP = 1.0                  # seconds between two requests
TICK = 30.0                    # how often the loop looks for due work
SIGNED_OUT_WAIT = 300.0
MAX_BACKOFF = 6 * 3600.0
LIST_STATE = "_vehicle_list"  # group name under which the vehicle-list backoff is kept

ACCEPT = {
    "connected": "application/json",
    "energy": "application/vnd.volvocars.api.energy.vehicledata.v1+json",
    "location": "application/json",
}
VEHICLE_LIST_PATH = "/connected-vehicle/v2/vehicles"

HttpGet = Callable[[str, Dict[str, str]], Tuple[int, dict, Mapping[str, str]]]


@dataclass(frozen=True)
class Group:
    name: str
    api: str
    path: str          # "{vin}" is replaced by the vehicle's VIN
    interval: float    # seconds between polls
    parser: Callable[[str, dict], list]
    location: bool = False


_CV = "/connected-vehicle/v2/vehicles/{vin}/"
GROUPS: Tuple[Group, ...] = (
    Group("energy", "energy", "/energy/v2/vehicles/{vin}/state", 300, parse.parse_energy),
    Group("engine_status", "connected", _CV + "engine-status", 600, parse.parse_connected),
    Group("doors", "connected", _CV + "doors", 600, parse.parse_connected),
    Group("windows", "connected", _CV + "windows", 1800, parse.parse_connected),
    Group("odometer", "connected", _CV + "odometer", 1800, parse.parse_connected),
    Group("statistics", "connected", _CV + "statistics", 1800, parse.parse_connected),
    Group("tyres", "connected", _CV + "tyres", 21600, parse.parse_connected),
    Group("warnings", "connected", _CV + "warnings", 21600, parse.parse_connected),
    Group("diagnostics", "connected", _CV + "diagnostics", 21600, parse.parse_connected),
    Group("brakes", "connected", _CV + "brakes", 21600, parse.parse_connected),
    Group("engine", "connected", _CV + "engine", 21600, parse.parse_connected),
    Group("location", "location", "/location/v1/vehicles/{vin}/location", 900,
          parse.parse_location, location=True),
)


def http_get(url: str, headers: Dict[str, str], timeout: float = 15.0):
    request = urllib.request.Request(url, headers=headers, method="GET")
    status, raw, reply_headers = auth.fetch(request, timeout, "vehicle API")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        payload = {}
    return status, payload if isinstance(payload, dict) else {}, reply_headers


def mask_vin(vin: str) -> str:
    """A VIN is sensitive: logs show only its last four characters."""
    return "..." + vin[-4:]


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Collector:
    def __init__(self, manager: auth.TokenManager, store: Store, api_key: str, *,
                 base: str = DEFAULT_BASE, budget: int = DEFAULT_BUDGET,
                 vins: Sequence[str] = (), location: bool = False,
                 get: HttpGet = http_get, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 groups: Sequence[Group] = GROUPS):
        if not 1 <= budget <= MAX_BUDGET:
            raise ValueError(f"daily budget must be between 1 and {MAX_BUDGET}")
        self._manager = manager
        self._store = store
        self._api_key = api_key
        self._base = base.rstrip("/")
        self._budget = budget
        self._vins = tuple(vins)
        self._location = location
        self._get = get
        self._clock = clock
        self._sleep = sleep
        self._groups = tuple(groups)
        self.budget_spent = False  # True once today's budget stopped a poll

    # --- one pass -----------------------------------------------------------

    def run_once(self) -> dict:
        """Poll everything that is due. Returns counts only: never a value or a token."""
        summary = {"polled": 0, "stored": 0, "failed": 0, "signed_in": True, "budget_spent": False}
        try:
            self._manager.access_token()
        except (auth.NotSignedIn, auth.TokenInvalid):
            log.warning("not signed in: sign in again from the dashboard's login link")
            summary["signed_in"] = False
            return summary
        except auth.AuthError as err:
            log.warning("could not check the sign-in: %s", err)
            summary["failed"] += 1
            return summary

        for vin in self._vehicles(summary):
            for group in self._groups:
                if group.location and not self._location:
                    continue
                state = self._store.state(vin, group.name)
                if state and state.next_due > self._clock():
                    continue
                if not self._store.take_call(self._day(), self._budget):
                    self.budget_spent = summary["budget_spent"] = True
                    log.warning("daily request budget (%d) is spent; collection resumes tomorrow",
                                self._budget)
                    return summary
                self._poll(vin, group, summary)
                self._sleep(MIN_GAP)
        return summary

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            wait = TICK
            try:
                if not self.run_once()["signed_in"]:
                    wait = SIGNED_OUT_WAIT
            except Exception as err:  # keep collecting: the next pass tries again
                log.warning("collection pass failed: %s", type(err).__name__)
                wait = 60.0
            stop.wait(wait)

    # --- internals ----------------------------------------------------------

    def _day(self) -> str:
        return _iso(self._clock())[:10]

    def _request(self, api: str, path: str):
        """One GET with the access token and the application key. Never logs the URL (it holds the VIN)."""
        headers = {"Authorization": "Bearer " + self._manager.access_token(),
                   "vcc-api-key": self._api_key, "Accept": ACCEPT[api]}
        return self._get(self._base + path, headers)

    def _vehicles(self, summary: dict):
        if self._vins:
            return self._vins
        known = self._store.vehicles()
        if known:
            return known
        state = self._store.state("", LIST_STATE)
        if state and state.next_due > self._clock():
            return ()
        if not self._store.take_call(self._day(), self._budget):
            self.budget_spent = summary["budget_spent"] = True
            return ()
        status, vins = None, []
        try:
            status, payload, _ = self._request("connected", VEHICLE_LIST_PATH)
            if status == 200:
                vins = parse.parse_vehicle_list(payload)
        except auth.AuthError as err:
            log.warning("vehicle list failed: %s", err)
        except Exception as err:  # a bad reply must not stop collection
            log.warning("vehicle list failed: %s", type(err).__name__)
        self._sleep(MIN_GAP)
        now = self._clock()
        if vins:
            for vin in vins:
                self._store.add_vehicle(vin, _iso(now))
                log.info("vehicle found: %s", mask_vin(vin))
            return tuple(vins)
        # Failed or empty: ask again later, never on the next tick.
        summary["failed"] += 1
        fails = (state.fails if state else 0) + 1
        self._store.set_state("", LIST_STATE, now + min(60.0 * 2 ** fails, 3600.0), fails, status, None)
        log.warning("vehicle list unavailable (HTTP %s); trying again later", status)
        return ()

    def _poll(self, vin: str, group: Group, summary: dict) -> None:
        now = self._clock()
        prior = self._store.state(vin, group.name)
        fails = prior.fails if prior else 0
        summary["polled"] += 1
        status, headers = None, {}
        try:
            status, payload, headers = self._request(group.api, group.path.format(vin=vin))
            if status == 200:
                readings = group.parser(group.name, payload)
                stored = self._store.add_readings(vin, readings, _iso(now))
                summary["stored"] += stored
                self._store.set_state(vin, group.name, now + group.interval, 0, 200, _iso(now))
                if readings:
                    log.info("poll %s ok: %d readings, %d new", group.name, len(readings), stored)
                else:  # a wrong path or header can still answer 200 with a body that holds nothing
                    log.warning("poll %s returned 200 but no readings: the endpoint, its headers "
                                "or this vehicle's support for it may be wrong", group.name)
                return
        except auth.AuthError as err:  # its messages are fixed text: a status and an error code
            status = None
            log.warning("poll %s failed: %s", group.name, err)
        except Exception as err:  # a bad reply or a storage fault must not stop collection;
            status = None         # only the exception's class is logged (its text may hold a header)
            log.warning("poll %s failed: %s", group.name, type(err).__name__)

        summary["failed"] += 1
        fails += 1
        wait = self._backoff(group, fails, headers or {})
        self._store.set_state(vin, group.name, now + wait, fails, status, None)
        if status is not None:
            log.warning("poll %s failed: HTTP %s (retry in %d s)", group.name, status, wait)

    @staticmethod
    def _backoff(group: Group, fails: int, headers: Mapping[str, str]) -> float:
        wait = min(group.interval * (2 ** min(fails, 10)), max(group.interval, 3600.0))
        try:
            retry_after = float(headers.get("Retry-After", 0))
        except (TypeError, ValueError):
            retry_after = 0.0
        return min(max(wait, retry_after, 60.0), MAX_BACKOFF)
