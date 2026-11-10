"""Sign-in web endpoints: /auth/login, /auth/callback and two plain result pages.

The authorisation code arrives in the callback URL's query string. Two measures keep it
out of places it could leak:

* the callback never renders a page: it answers with a 303 redirect to a result page
  that has no query string, so the code is not left in the address bar of the final page;
* no access-log line, error line or exception text contains a query string.
"""
from __future__ import annotations

import datetime
import html
import logging
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from . import auth, store, theme, tiles, tripviews, view

log = logging.getLogger("evdash.server")

_QUERY = re.compile(r"\?[^\s'\"]*")
_STATIC = {"leaflet.js": ("text/javascript; charset=utf-8", "leaflet.js"),
           "leaflet.css": ("text/css; charset=utf-8", "leaflet.css"),
           "evmap.js": ("text/javascript; charset=utf-8", "evmap.js")}
_TILE_PATH = re.compile(r"/tiles/(\d{1,2})/(\d{1,8})/(\d{1,8})\.png")

_PAGES = {
    "/auth/done": ("Sign-in done", "Sign-in complete. You can close this tab."),
    "/auth/failed": ("Sign-in failed", "Sign-in did not complete. Start again from the login link."),
}


class App:
    def __init__(self, cfg: auth.OAuthConfig, manager: auth.TokenManager,
                 pending: auth.PendingSignIns, db_path: Optional[Path] = None,
                 location: bool = False, tz: datetime.tzinfo = datetime.timezone.utc,
                 tiles: Optional["tiles.TileCache"] = None):
        self.cfg = cfg
        self.manager = manager
        self.pending = pending
        self.db_path = db_path  # the collector's database, read-only, for the state view
        self.location = location  # opt-in: the trip pages exist only when location is turned on
        self.tz = tz              # the time zone trip times are shown in
        self.tiles = tiles        # opt-in: map tiles, fetched and served by this server


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(_Handler):
        pass
    Handler.app = app
    return ThreadingHTTPServer((host, port), Handler)


class _Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "evdash"
    sys_version = ""

    # -- logging: never a query string ------------------------------------
    def log_request(self, code="-", size="-"):
        # On a malformed request line the parser has not set command/path yet.
        path = urllib.parse.urlsplit(getattr(self, "path", "")).path
        if path.startswith("/tiles/"):
            path = "/tiles/[redacted]"  # tile numbers locate the area being viewed
        log.info("%s %s %s", getattr(self, "command", None) or "-", path or "-", code)

    def log_message(self, format, *args):
        log.info(_QUERY.sub("?[redacted]", format % args))

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/":
            self._home()
        elif parts.path == "/healthz":
            self._text(200, "ok")
        elif parts.path == "/auth/login":
            self._login_page()
        elif parts.path == "/auth/callback":
            self._callback(urllib.parse.parse_qs(parts.query))
        elif parts.path == "/trips" or parts.path.startswith("/trips/"):
            self._trips(parts)
        elif parts.path.startswith("/tiles/"):
            self._tile(parts.path)
        elif parts.path.startswith("/static/"):
            self._static(parts.path)
        elif parts.path in _PAGES:
            title, text = _PAGES[parts.path]
            self._text(200, text, title=title)
        else:
            self._text(404, "Page not found")

    do_HEAD = do_GET

    def do_POST(self):
        # Starting a sign-in is a POST so that a link or an embedded image cannot trigger it
        # (each start uses up one of a few pending slots).
        if urllib.parse.urlsplit(self.path).path != "/auth/login":
            return self._text(404, "Page not found")
        state, challenge = self.app.pending.begin()
        self._redirect(303, auth.build_authorize_url(self.app.cfg, state, challenge))

    def _home(self):
        """The landing page: whether the owner is signed in, and what was granted.

        Shows facts about the sign-in (never a token) and the latest stored vehicle state.
        """
        try:
            info = self.app.manager.status()
        except auth.AuthError:
            info = None
        # What is on disk is one fact; whether Volvo still accepts it is another, and only the
        # collector knows that. Say which, instead of calling a refused sign-in "signed in".
        kind, sentence = auth.describe(info, store.read_auth_record(self.app.db_path))
        headline = {"ok": "Signed in.", "rejected": "Sign-in refused by Volvo.",
                    "unreachable": "Volvo could not be reached.", "unverified": "Sign-in not yet checked.",
                    "no_sign_in": "Not signed in.", "unreadable": "Sign-in unreadable."}[kind]
        pill_kind = {"ok": "good", "unverified": "warn", "unreachable": "warn"}.get(kind, "bad")
        pill_text = {"ok": "Signed in", "rejected": "Sign-in refused", "unreachable": "Volvo unreachable",  # ui-text
                     "unverified": "Not yet checked", "no_sign_in": "Not signed in",
                     "unreadable": "Sign-in unreadable"}[kind]
        lead = headline.rstrip(".") + ": "
        shown = sentence[len(lead):] if sentence.startswith(lead) else sentence  # do not say it twice
        facts = f"<p><strong>{html.escape(headline)}</strong> {html.escape(shown)}</p>"
        if info is not None and info["signed_in"] and kind in ("ok", "unverified", "unreachable"):
            scopes = info.get("granted_scopes") or []
            listing = ("<ul>" + "".join(f"<li><code>{html.escape(s)}</code></li>" for s in scopes)
                       + "</ul>") if scopes else "<p>The server did not report the granted scopes.</p>"
            facts += f"<h3>Granted access ({len(scopes)})</h3>{listing}"
        banner = ""
        if kind != "ok":
            banner = (f'<div class="banner {pill_kind}"><div><strong>{html.escape(headline)}</strong>'
                      f"<p>{html.escape(sentence)}</p></div>"
                      "<a class=btn href=/auth/login>Sign in</a></div>")
        # When something is wrong the banner carries the one prominent button; otherwise a quiet one.
        sign_in = '<a class="btn ghost" href=/auth/login>Sign in again</a>' if kind == "ok" else ""
        page = theme.shell(
            "EV Dashboard",
            banner + view.render(self.app.db_path, tz=self.app.tz)
            + f'<details><summary>Sign-in and access</summary><div class="inner">{facts}</div></details>',
            active="home", show_trips=self.app.location,
            subtitle="A personal, read-only dashboard for your own vehicle",  # ui-text
            right=theme.pill(pill_kind, pill_text) + sign_in)
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._headers(styles=True)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _trips(self, parts):
        if not self.app.location:
            return self._html(404, tripviews.shell("Trips", tripviews.OFF))
        try:
            if parts.path == "/trips":
                status, page = tripviews.render_list(
                    self.app.db_path, urllib.parse.parse_qs(parts.query), self.app.tz,
                    tiles=bool(self.app.tiles))
            else:
                status, page = tripviews.render_detail(
                    self.app.db_path, parts.path[len("/trips/"):], self.app.tz,
                    tiles=bool(self.app.tiles))
        except Exception as err:  # only the class is logged: the message could hold stored data
            log.warning("trip page failed: %s", type(err).__name__)
            status, page = 500, tripviews.shell("Trips", "<p>Something went wrong reading the trips.</p>")
        self._html(status, page)

    def _tile(self, path):
        match = _TILE_PATH.fullmatch(path)
        if not (self.app.tiles and self.app.location) or not match:
            return self._text(404, "Page not found")
        z, x, y = (int(g) for g in match.groups())
        data = self.app.tiles.get(z, x, y)
        if data is None:
            return self._text(404, "tile not available")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _static(self, path):
        """The bundled map script and style: only a fixed list of names, only when the map is on."""
        entry = _STATIC.get(path[len("/static/"):])
        if not (self.app.tiles and self.app.location) or entry is None:
            return self._text(404, "Page not found")
        content_type, name = entry
        try:
            data = (Path(__file__).parent / "static" / name).read_bytes()
        except OSError:
            return self._text(404, "Page not found")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _html(self, status, page):
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._headers(styles=True)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _login_page(self):
        body = theme.shell(
            "Sign in",  # ui-text
            '<div class="card"><p>You will be sent to Volvo to sign in and approve read access. '
            "Tick the remember-me option there so the sign-in lasts longer.</p>"
            '<form method=post action=/auth/login><button class=btn>Continue to Volvo</button></form></div>',
            show_trips=False, heading="EV Dashboard", subtitle="Sign in").encode("utf-8")  # ui-text
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._headers(styles=True)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _callback(self, query):
        def one(name):
            values = query.get(name, [])
            return values[0] if len(values) == 1 else ""

        code, state = one("code"), one("state")
        verifier = self.app.pending.take(state) if state else None
        if one("error") or not code or verifier is None:
            log.warning("sign-in refused: %s", "provider error" if one("error")
                        else "missing code or unknown/expired state")
            return self._redirect(303, "/auth/failed")
        try:
            self.app.manager.complete_sign_in(code, verifier)
        except auth.AuthError as err:
            log.warning("sign-in failed: %s", err)
            return self._redirect(303, "/auth/failed")
        log.info("sign-in complete; tokens saved")
        self._redirect(303, "/auth/done")

    # -- responses --------------------------------------------------------
    def _headers(self, styles=False):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        if styles:  # inline styles only; still no scripts and nothing fetched from elsewhere
            # Map tiles, when enabled, come from this server's own address and nowhere else.
            maps = "; img-src 'self'; script-src 'self'" if self.app.tiles else ""
            styles = "'unsafe-inline' 'self'" if self.app.tiles else "'unsafe-inline'"
            self.send_header("Content-Security-Policy", f"default-src 'none'; style-src {styles}" + maps)
            return
        # Deliberately no form-action directive: browsers apply it to the redirect that answers
        # the sign-in form post, and a restrictive value silently blocks the hand-off to the
        # identity provider (some browsers read it from the redirect response as well).
        self.send_header("Content-Security-Policy", "default-src 'none'")

    def _redirect(self, status, location):
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self._headers()
        self.end_headers()

    def _text(self, status, text, title=""):
        body = (theme.shell(title, f'<div class="card"><p>{text}</p></div>', show_trips=False,
                            heading="EV Dashboard", subtitle=title)
                if title else text).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if title
                         else "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._headers(styles=bool(title))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
