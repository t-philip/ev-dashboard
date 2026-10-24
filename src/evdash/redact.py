"""Log redaction: a last line of defence behind "never log a secret".

Applied as a *formatter*, so it scrubs the final text including tracebacks, whatever
code produced it.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

PLACEHOLDER = "[redacted]"

# key=value / "key": "value" pairs that carry credentials
_PAIR = re.compile(
    r"""(?ix)
    (?P<key>\b(?:access_token|refresh_token|id_token|client_secret|code_verifier|code|state|
               vcc-api-key|api[_-]?key|password|secret)\b["']?\s*[:=]\s*["']?)
    (?P<val>[^\s"'&,;}]+)
    """)
_AUTH_HEADER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_QUERY = re.compile(r"\?[^\s'\"]+")
# Anything long and token-shaped (JWTs, opaque tokens, base64 blobs).
_TOKENISH = re.compile(r"(?<![A-Za-z0-9._~+/=-])[A-Za-z0-9._~+/=-]{32,}(?![A-Za-z0-9._~+/=-])")
# A 17-character vehicle identification number.
_VIN = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]{17}\b)(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-HJ-NPR-Z0-9]{17}\b")


def scrub(text: str, known: Iterable[str] = ()) -> str:
    """Remove credential-shaped content from ``text``."""
    for value in known:
        if value:
            text = text.replace(value, PLACEHOLDER)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)} {PLACEHOLDER}", text)
    text = _PAIR.sub(lambda m: m.group("key") + PLACEHOLDER, text)
    text = _QUERY.sub("?" + PLACEHOLDER, text)
    text = _TOKENISH.sub(PLACEHOLDER, text)
    return _VIN.sub(PLACEHOLDER, text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, known: Iterable[str] = ()):
        super().__init__(fmt, datefmt)
        self._known = tuple(v for v in known if v)

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record), self._known)


def install(known: Iterable[str] = (), level: int = logging.INFO) -> None:
    """Route all logging through the redacting formatter and quiet HTTP libraries."""
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", known=known))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("urllib3", "httpx", "httpcore", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
