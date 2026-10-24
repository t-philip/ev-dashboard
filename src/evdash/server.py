"""Sign-in web endpoints: /auth/login, /auth/callback and two plain result pages.

The authorisation code arrives in the callback URL's query string. Two measures keep it
out of places it could leak:

* the callback never renders a page: it answers with a 303 redirect to a result page
  that has no query string, so the code is not left in the address bar of the final page;
* no access-log line, error line or exception text contains a query string.
"""
from __future__ import annotations

import html
import logging
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import auth

log = logging.getLogger("evdash.server")

_QUERY = re.compile(r"\?[^\s'\"]*")

_PAGES = {
    "/auth/done": ("Sign-in done", "Sign-in complete. You can close this tab."),
    "/auth/failed": ("Sign-in failed", "Sign-in did not complete. Start again from the login link."),
}


class App:
    def __init__(self, cfg: auth.OAuthConfig, manager: auth.TokenManager,
                 pending: auth.PendingSignIns):
        self.cfg = cfg
        self.manager = manager
        self.pending = pending


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

        Shows facts about the sign-in only: never a token, and no vehicle data (none is
        collected yet).
        """
        try:
            info = self.app.manager.status()
        except auth.AuthError:
            info = None
        if info is None:
            state = "<p><strong>The stored sign-in could not be read.</strong> Sign in again.</p>"
        elif info["signed_in"]:
            scopes = info.get("granted_scopes") or []
            listing = ("<ul>" + "".join(f"<li><code>{html.escape(s)}</code></li>" for s in scopes)
                       + "</ul>") if scopes else "<p>The server did not report the granted scopes.</p>"
            state = ("<p><strong>Signed in.</strong> Access tokens refresh automatically; "
                     "you only sign in again if the grant lapses.</p>"
                     f"<h2>Granted access ({len(scopes)})</h2>{listing}")
        else:
            state = "<p><strong>Not signed in.</strong></p>"
        body = (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            "<title>EV Dashboard</title><style>"
            ":root{color-scheme:light dark}body{font:16px/1.6 system-ui,sans-serif;"
            "max-width:40rem;margin:2rem auto;padding:0 16px}"
            "code{font-size:.9em}a.button{display:inline-block;padding:.5rem 1rem;"
            "border:1px solid currentColor;border-radius:6px;text-decoration:none}"
            "</style><h1>EV Dashboard</h1>" + state
            + "<p><a class=button href=/auth/login>Sign in</a></p>"
            "<p>No vehicle data is collected yet.</p></html>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._headers(styles=True)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _login_page(self):
        body = ('<!doctype html><meta charset=utf-8><title>Sign in</title>'
                '<form method=post action=/auth/login><button>Sign in</button></form>'
                ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._headers()
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
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'")
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
        body = (f"<!doctype html><meta charset=utf-8><title>{title or 'EV Dashboard'}</title>"
                f"<p>{text}</p>" if title else text).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if title
                         else "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
