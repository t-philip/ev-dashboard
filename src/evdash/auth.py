"""OAuth 2.0 authorisation-code flow with PKCE, and the protected token cache.

Nothing in this module logs, prints or formats a token, a code or a client secret.
Error messages carry the HTTP status and the short ``error`` code the server sent,
never a response body.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

try:  # POSIX only; on other platforms the in-process lock still applies
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

# An access token is refreshed this many seconds before it expires.
REFRESH_MARGIN = 60
# A sign-in link is valid for this many seconds.
PENDING_TTL = 600
DNS_ATTEMPTS = 3  # tries for a failed name lookup (a lossy resolver should not fail a sign-in)
DNS_RETRY_PAUSE = 1.0

PostFn = Callable[[str, Dict[str, str], Dict[str, str]], Tuple[int, dict]]


class AuthError(Exception):
    """A sign-in or token problem. The message never contains a secret.

    ``http_status`` and ``error_code`` carry what the server answered (the status number and
    its short ``error`` word), so a log line can say *why* without ever holding a body.
    """

    def __init__(self, message: str = "", *, http_status: Optional[int] = None,
                 error_code: Optional[str] = None):
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code


class NotSignedIn(AuthError):
    """No token file exists yet: the owner has to sign in."""


class TokenInvalid(AuthError):
    """The server refused the refresh token: the owner has to sign in again."""


class EndpointUnreachable(AuthError):
    """The token endpoint could not be reached (network or server down).

    Nothing was decided about the sign-in: it is neither accepted nor refused, so the right
    response is to retry, not to ask the owner to sign in.
    """


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    redirect_uri: str
    authorize_url: str
    token_url: str
    scopes: Tuple[str, ...]


# --- PKCE -----------------------------------------------------------------

def new_verifier() -> str:
    """A PKCE code verifier: 43 to 128 URL-safe characters (RFC 7636)."""
    return secrets.token_urlsafe(64)


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorize_url(cfg: OAuthConfig, state: str, challenge: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": " ".join(cfg.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{cfg.authorize_url}?{query}"


class PendingSignIns:
    """Sign-ins that have been started but not completed.

    Each entry is single-use and expires, so a replayed or stale callback is refused.
    Kept in memory: a restart between the link and the callback just means starting again.
    """

    def __init__(self, ttl: int = PENDING_TTL, limit: int = 16, clock=time.time):
        self._ttl = ttl
        self._limit = limit
        self._clock = clock
        self._items: Dict[str, Tuple[str, float]] = {}
        self._lock = threading.Lock()

    def begin(self) -> Tuple[str, str]:
        """Return (state, challenge) for a new sign-in."""
        state = secrets.token_urlsafe(32)
        verifier = new_verifier()
        with self._lock:
            self._expire()
            while len(self._items) >= self._limit:
                self._items.pop(next(iter(self._items)))
            self._items[state] = (verifier, self._clock() + self._ttl)
        return state, challenge_for(verifier)

    def take(self, state: str) -> Optional[str]:
        """Return the verifier for ``state`` and forget it, or None."""
        with self._lock:
            self._expire()
            entry = self._items.pop(state, None)
        return entry[0] if entry else None

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, (_, exp) in self._items.items() if exp <= now]:
            del self._items[key]


# --- HTTP ---------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: credentials must never be re-sent to another address."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _open(request: urllib.request.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


def fetch(request: urllib.request.Request, timeout: float = 15.0,
          what: str = "token endpoint") -> Tuple[int, bytes, Mapping[str, str]]:
    """Open ``request``; returns (status, body, headers). Retries only a failed name lookup.

    A failed lookup means the request has not left this host, so repeating it cannot spend a
    rotating refresh token twice. (A redirect to another host that then fails to resolve is the
    one theoretical exception; redirects are refused outright.) Any later failure is
    not retried. Failures raise ``AuthError`` naming only ``what``.
    """
    attempts = max(1, DNS_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        try:
            with _open(request, timeout) as response:
                return response.status, response.read(1 << 20), getattr(response, "headers", {})
        except urllib.error.HTTPError as err:
            return err.code, err.read(1 << 20), err.headers
        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
            reason = getattr(err, "reason", err)
            lookup_failed = isinstance(reason, socket.gaierror)
            if lookup_failed and attempt < attempts:
                time.sleep(DNS_RETRY_PAUSE)
                continue
            raise EndpointUnreachable(f"{what} unreachable",
                                      error_code=unreachable_reason(reason)) from None
    raise EndpointUnreachable(f"{what} unreachable", error_code="other")  # never reached


def unreachable_reason(reason: object) -> str:
    """A short, fixed word for why a request never got an answer (never the exception text).

    The word matters for diagnosis: a failed name lookup means the request never left this host,
    while a timeout or reset may mean the server processed it and the reply was lost.
    """
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError,
                           http.client.RemoteDisconnected, http.client.IncompleteRead)):
        return "reset"
    if isinstance(reason, ssl.SSLError):
        return "tls"
    if isinstance(reason, OSError):
        return "network"
    return "other"


def post_form(url: str, data: Dict[str, str], headers: Dict[str, str],
              timeout: float = 15.0) -> Tuple[int, dict]:
    body = urllib.parse.urlencode(data).encode("ascii")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json", **headers})
    status, raw, _ = fetch(request, timeout, "token endpoint")
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


_SAFE_SCOPE = re.compile(r"[A-Za-z0-9_.:/@+%=\- ]{1,1024}")  # no control characters
_SAFE_ERROR = re.compile(r"[A-Za-z0-9_.\- ]{1,64}")


def _error_code(payload: dict) -> str:
    value = payload.get("error")
    if isinstance(value, str) and _SAFE_ERROR.fullmatch(value):
        return value
    return "unspecified"


def _token_request(cfg: OAuthConfig, form: Dict[str, str], post: PostFn,
                   clock: Callable[[], float]) -> dict:
    """POST to the token endpoint with the client credentials in an HTTP Basic header."""
    # RFC 6749 section 2.3.1: form-urlencode both parts before joining them.
    userpass = (urllib.parse.quote_plus(cfg.client_id) + ":"
                + urllib.parse.quote_plus(cfg.client_secret))
    basic = base64.b64encode(userpass.encode("utf-8"))
    status, payload = post(cfg.token_url, form,
                           {"Authorization": "Basic " + basic.decode("ascii")})
    if status != 200:
        error = _error_code(payload)
        message = f"token endpoint refused the request (HTTP {status}, error={error})"
        if form.get("grant_type") == "refresh_token" and error == "invalid_grant":
            raise TokenInvalid(message, http_status=status, error_code=error)
        raise AuthError(message, http_status=status, error_code=error)
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthError("token endpoint reply had no access token")
    refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        expires_in = 300
    tokens = {
        "access_token": access,
        "refresh_token": refresh if isinstance(refresh, str) and refresh else None,
        "expires_at": clock() + float(expires_in),
    }
    scope = payload.get("scope")
    if not isinstance(scope, str):
        scope = _scope_claim(access)  # some servers only put it inside the token
    if isinstance(scope, str) and _SAFE_SCOPE.fullmatch(scope):
        tokens["scope"] = scope  # the scopes the server actually granted; not a secret
    lifetime = _refresh_lifetime(payload, tokens["refresh_token"])
    if lifetime:
        tokens["refresh_lifetime_s"] = lifetime  # how long the server lets a refresh token live
    return tokens


MAX_LIFETIME_S = 10 * 365 * 86400


def _plausible(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if 0 < value <= MAX_LIFETIME_S else None


def _refresh_lifetime(payload: dict, refresh: Optional[str]) -> Optional[int]:
    """Seconds a refresh token lives, if the server says so: the ``refresh_expires_in`` field of
    the reply, else ``exp - iat`` of a JWT-shaped refresh token. A number only, never the token.
    None when the server tells us nothing (the lifetime is then unknown, not infinite)."""
    stated = _plausible(payload.get("refresh_expires_in"))
    if stated:
        return stated
    claims = _claims(refresh) if isinstance(refresh, str) else {}
    exp, iat = claims.get("exp"), claims.get("iat")
    if (isinstance(exp, (int, float)) and isinstance(iat, (int, float))
            and not isinstance(exp, bool) and not isinstance(iat, bool)):
        return _plausible(exp - iat)
    return None


def _claims(token: str) -> dict:
    """The claims of a JWT, read without verifying it. Informational only: nothing is trusted or
    decided from them. An opaque token or anything unexpected gives an empty dict."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _scope_claim(token: str) -> Optional[str]:
    """The ``scope`` claim of a JWT access token (what to show the owner), or None."""
    scope = _claims(token).get("scope")
    return scope if isinstance(scope, str) else None


# --- token cache ------------------------------------------------------------

class TokenStore:
    """The token file: owner-only, written atomically, one writer at a time."""

    def __init__(self, path):
        self.path = Path(path)
        self._thread_lock = threading.RLock()

    def load(self) -> Optional[dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            raise AuthError("token file is not valid JSON") from None
        refresh = data.get("refresh_token") if isinstance(data, dict) else None
        if (not isinstance(data, dict)
                or not isinstance(data.get("access_token"), str)
                or not (refresh is None or isinstance(refresh, str))
                or isinstance(data.get("expires_at"), bool)
                or not isinstance(data.get("expires_at"), (int, float))):
            raise AuthError("token file has an unexpected shape")
        return data

    def save(self, tokens: dict) -> None:
        """Write to a temp file, fsync, then rename over the old file.

        A crash at any point leaves either the old complete file or the new complete
        file, never a partial one. This matters because the refresh token rotates: the
        old one is already dead once the server has replied.
        """
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tokens-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:  # mkstemp makes it mode 600
                json.dump(tokens, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace(tmp)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        if os.name == "posix":
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def _replace(self, tmp: str) -> None:
        # On Windows the rename fails while another reader holds the file open: retry briefly.
        for attempt in range(6):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05)

    @contextlib.contextmanager
    def exclusive(self):
        """Hold the single-writer lock (across threads and, on POSIX, processes)."""
        with self._thread_lock:
            if fcntl is None:
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(self.path.name + ".lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)  # closing releases the flock


class TokenManager:
    def __init__(self, cfg: OAuthConfig, store: TokenStore,
                 post: PostFn = post_form, clock: Callable[[], float] = time.time):
        self._cfg = cfg
        self._store = store
        self._post = post
        self._clock = clock

    def complete_sign_in(self, code: str, verifier: str) -> None:
        """Exchange the authorisation code for tokens and persist them."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._cfg.redirect_uri,
            "code_verifier": verifier,
        }
        tokens = _token_request(self._cfg, form, self._post, self._clock)
        if not tokens["refresh_token"]:
            raise AuthError("token endpoint reply had no refresh token")
        with self._store.exclusive():
            self._store.save(tokens)

    def access_token(self) -> str:
        """A valid access token, refreshing (and saving the rotated pair) if needed."""
        with self._store.exclusive():
            tokens = self._store.load()
            if tokens is None:
                raise NotSignedIn("not signed in")
            if tokens.get("expires_at", 0) - REFRESH_MARGIN > self._clock():
                return tokens["access_token"]
            refresh = tokens.get("refresh_token")
            if not refresh:
                raise TokenInvalid("no refresh token on file")
            form = {"grant_type": "refresh_token", "refresh_token": refresh}
            new = _token_request(self._cfg, form, self._post, self._clock)
            if not new["refresh_token"]:
                new["refresh_token"] = refresh  # server did not rotate: keep the one we have
            if "scope" not in new and isinstance(tokens.get("scope"), str):
                new["scope"] = tokens["scope"]
            if "refresh_lifetime_s" not in new and _plausible(tokens.get("refresh_lifetime_s")):
                new["refresh_lifetime_s"] = tokens["refresh_lifetime_s"]  # keep what was learned
            self._store.save(new)
            return new["access_token"]

    def status(self) -> dict:
        """Facts about the cache that are safe to show: never a token value."""
        tokens = self._store.load()
        if tokens is None:
            return {"signed_in": False}
        scope = tokens.get("scope")
        granted = sorted(scope.split()) if isinstance(scope, str) else None
        try:
            saved_at = self._store.path.stat().st_mtime
        except OSError:
            saved_at = None
        # "signed_in" here means only that a token file is on disk. Whether the server still
        # accepts it is a separate fact, recorded by the collector and read by ``describe``.
        return {"signed_in": True, "granted_scopes": granted, "saved_at": saved_at,
                "refresh_lifetime_s": _plausible(tokens.get("refresh_lifetime_s")),
                "access_token_valid_for": max(0, int(tokens.get("expires_at", 0) - self._clock()))}


def _stamp(epoch) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(epoch)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "an unknown time"


def format_utc(epoch) -> str:
    """A UTC timestamp to the second, for the history listing."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(float(epoch)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown time"


def format_duration(seconds) -> str:
    """'45 s', '12 min', '3.2 h' or '4.1 days': short and honest about being rounded."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "an unknown time"
    if s < 90:
        return f"{int(s)} s"
    if s < 5400:
        return f"{int(round(s / 60))} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


def describe(info: Optional[dict], record: Optional[dict]) -> Tuple[str, str]:
    """(state, sentence) saying what is actually true about the sign-in.

    ``info`` is ``TokenManager.status()`` (what is on disk, None when unreadable); ``record`` is
    what the collector last learned from the server (None before its first check). A parked car
    is not a state: collection only stops when the sign-in is missing, refused, or unreachable.
    """
    if info is None:
        return "unreadable", "The stored sign-in could not be read."
    if not info.get("signed_in"):
        return "no_sign_in", "No sign-in is on file, so nothing is being collected. Sign in to start."
    state = record.get("state") if isinstance(record, dict) else None
    since = record.get("since") if isinstance(record, dict) else None
    saved_at = info.get("saved_at")
    # A new sign-in written after the recorded problem has not been checked yet.
    newer = isinstance(saved_at, (int, float)) and isinstance(since, (int, float)) and saved_at > since
    if state == "ok":
        return "ok", "Signed in: Volvo is accepting the saved sign-in."
    if state in ("rejected", "no_sign_in", "unreachable") and not newer:
        if state == "rejected":
            detail = []
            if record.get("http_status"):
                detail.append(f"HTTP {record['http_status']}")
            if record.get("error"):
                detail.append(f"error {record['error']}")
            suffix = f" ({', '.join(detail)})" if detail else ""
            return "rejected", (f"Volvo rejected the saved sign-in at {_stamp(since)}{suffix}. "
                                "Collection is stopped until you sign in again. This is not "
                                "caused by the car being parked: the collector keeps the "
                                "sign-in alive by refreshing it every few minutes.")
        if state == "unreachable":
            return "unreachable", (f"Volvo's token service could not be reached since "
                                   f"{_stamp(since)}. The collector keeps retrying; the saved "
                                   "sign-in has not been judged either way.")
        return "no_sign_in", "No sign-in is on file, so nothing is being collected. Sign in to start."
    return "unverified", ("A sign-in is on file; the collector has not yet checked it with "
                          "Volvo, so it is not known whether Volvo still accepts it.")
