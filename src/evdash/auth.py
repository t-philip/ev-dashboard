"""OAuth 2.0 authorisation-code flow with PKCE, and the protected token cache.

Nothing in this module logs, prints or formats a token, a code or a client secret.
Error messages carry the HTTP status and the short ``error`` code the server sent,
never a response body.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

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
    """A sign-in or token problem. The message never contains a secret."""


class NotSignedIn(AuthError):
    """No token file exists yet: the owner has to sign in."""


class TokenInvalid(AuthError):
    """The server refused the refresh token: the owner has to sign in again."""


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


# --- token endpoint ---------------------------------------------------------

def post_form(url: str, data: Dict[str, str], headers: Dict[str, str],
              timeout: float = 15.0) -> Tuple[int, dict]:
    body = urllib.parse.urlencode(data).encode("ascii")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json", **headers})
    attempts = max(1, DNS_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, raw = response.status, response.read(1 << 20)
            break
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read(1 << 20)
            break
        except (urllib.error.URLError, OSError) as err:
            # Only a failed name lookup is retried: the request has not left this host, so
            # repeating it cannot spend a rotating refresh token twice. (A redirect to another
            # host that then fails to resolve is the one theoretical exception; the token
            # endpoint does not redirect.) Any later failure is not retried.
            lookup_failed = isinstance(getattr(err, "reason", err), socket.gaierror)
            if lookup_failed and attempt < attempts:
                time.sleep(DNS_RETRY_PAUSE)
                continue
            raise AuthError("token endpoint unreachable") from None
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
            raise TokenInvalid(message)
        raise AuthError(message)
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
    return tokens


def _scope_claim(token: str) -> Optional[str]:
    """The ``scope`` claim of a JWT access token, read without verifying it.

    Informational only (what to show the owner); nothing is trusted or decided from it.
    Returns None for an opaque token or anything unexpected.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None
    scope = claims.get("scope") if isinstance(claims, dict) else None
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
            self._store.save(new)
            return new["access_token"]

    def status(self) -> dict:
        """Facts about the cache that are safe to show: never a token value."""
        tokens = self._store.load()
        if tokens is None:
            return {"signed_in": False}
        scope = tokens.get("scope")
        granted = sorted(scope.split()) if isinstance(scope, str) else None
        return {"signed_in": True, "granted_scopes": granted,
                "access_token_valid_for": max(0, int(tokens.get("expires_at", 0) - self._clock()))}
