"""Find out what an endpoint returns without printing what it says.

``describe`` reduces a reply to its shape: field names and value types. A value is shown only
when it looks like a status word (for example CLOSED or NO_WARNING: capital letters and
underscores only, short), and only for endpoints known to report plain statuses; numbers, text,
coordinates and identifiers are never shown. Field names are shown only when they look like field
names (letters, at most two digits, not identifier-length), so a reply keyed by an identifier is
masked. This is a best-effort filter, not a guarantee: read the output before pasting it anywhere.
"""
from __future__ import annotations

import re
from typing import Any

_STATUS_WORD = re.compile(r"[A-Z][A-Z_]{1,29}")  # letters only: identifiers such as a VIN hold digits
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
# String values are shown only for these endpoints, which report plain statuses. Anything else
# (places, routes, vehicle descriptions, a path nobody has looked at yet) shows types only.
_WORDS_OK = re.compile(
    r"/connected-vehicle/v2/vehicles/\{vin\}/(doors|windows|tyres|warnings|diagnostics|brakes|engine|"
    r"engine-status|odometer|statistics|battery-charge-level|fuel|command-accessibility)"
    r"|/energy/v2/vehicles/\{vin\}/(state|capabilities)")
_PATH = re.compile(r"/[A-Za-z0-9/_\-{}]{1,200}")
MAX_DEPTH = 6
MAX_KEYS = 60

# Endpoints worth knowing about for the scopes the application can hold. Not all exist for every
# vehicle; a 403 means the scope is not in the current grant, a 404 that the path does not exist.
DEFAULT_PATHS = (
    "/connected-vehicle/v2/vehicles/{vin}",
    "/connected-vehicle/v2/vehicles/{vin}/resources",
    "/connected-vehicle/v2/vehicles/{vin}/battery-charge-level",
    "/connected-vehicle/v2/vehicles/{vin}/fuel",
    "/connected-vehicle/v2/vehicles/{vin}/environment",
    "/connected-vehicle/v2/vehicles/{vin}/navigation",
    "/connected-vehicle/v2/vehicles/{vin}/command-accessibility",
    "/connected-vehicle/v2/vehicles/{vin}/engine",
    "/energy/v2/vehicles/{vin}/capabilities",
    "/location/v1/vehicles/{vin}/location",
)


def valid_path(path: str) -> bool:
    return bool(_PATH.fullmatch(path)) and ".." not in path and not path.startswith("//")


def shows_words(path: str) -> bool:
    """True only for endpoints known to report plain statuses; everything else shows types only."""
    return bool(_WORDS_OK.fullmatch(path))


def _safe_key(key: Any) -> str:
    ok = (isinstance(key, str) and _KEY.fullmatch(key) and sum(c.isdigit() for c in key) <= 2
          and len(key) != 17)
    return key if ok else "<key>"


def describe(value: Any, depth: int = 0, words: bool = True) -> Any:
    if depth > MAX_DEPTH:
        return "..."
    if isinstance(value, dict):
        return {_safe_key(k): describe(v, depth + 1, words) for k, v in list(value.items())[:MAX_KEYS]}
    if isinstance(value, list):
        return [f"{len(value)} items"] + ([describe(value[0], depth + 1, words)] if value else [])
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return None
    if isinstance(value, str):
        # 17 characters is a VIN's length: never show one, even if it were all letters
        return value if words and _STATUS_WORD.fullmatch(value) and len(value) != 17 else "str"
    return "?"
