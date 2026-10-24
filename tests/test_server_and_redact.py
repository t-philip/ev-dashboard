import http.client
import io
import logging
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

from evdash import auth, config, redact
from evdash.server import App, make_server

from test_auth import CID, REDIRECT, SECRET, FakeIdP, make_cfg

# Fictitious, token-shaped values.
CODE = "test-data-code-0123456789abcdefghijklmnop"
ACCESS = "test-data-access-0123456789abcdefghijklmnop"
REFRESH = "test-data-refresh-0123456789abcdefghijklmnop"


class Scrub(unittest.TestCase):
    def test_known_values_removed(self):
        self.assertNotIn("abc123", redact.scrub("value abc123 here", known=["abc123"]))

    def test_bearer_and_basic(self):
        out = redact.scrub("Authorization: Bearer abc.def-ghi and Basic dGVzdDp0ZXN0")
        self.assertNotIn("abc.def", out)
        self.assertNotIn("dGVzdDp0ZXN0", out)

    def test_key_value_pairs(self):
        for text in ('refresh_token=zzz', '"access_token": "zzz"', "code=zzz&state=yyy",
                     "client_secret: zzz", "vcc-api-key: zzz"):
            self.assertNotIn("zzz", redact.scrub(text), text)

    def test_query_strings(self):
        self.assertNotIn("zzz", redact.scrub("GET /auth/callback?code=zzz&state=yyy HTTP/1.1"))

    def test_long_token_shaped_strings(self):
        self.assertNotIn(ACCESS, redact.scrub(f"got {ACCESS} back"))

    def test_vin_shaped_strings(self):
        self.assertNotIn("TESTDATA123456789", redact.scrub("vehicle TESTDATA123456789 ok"))

    def test_ordinary_text_survives(self):
        text = "polled 12 endpoints in 1.4s, budget 40 percent used"
        self.assertEqual(redact.scrub(text), text)

    def test_traceback_is_scrubbed_through_the_formatter(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(redact.RedactingFormatter("%(message)s", known=[SECRET]))
        logger = logging.getLogger("evdash.test.tb")
        logger.propagate = False
        logger.addHandler(handler)
        try:
            raise RuntimeError(f"boom {SECRET} access_token={ACCESS}")
        except RuntimeError:
            logger.exception("failed")
        out = stream.getvalue()
        self.assertNotIn(SECRET, out)
        self.assertNotIn(ACCESS, out)
        self.assertIn("Traceback", out)


class Config(unittest.TestCase):
    def env(self, d, **extra):
        (Path(d) / "client_id").write_text(CID + "\n")
        (Path(d) / "client_secret").write_text(SECRET + "\n")
        base = {"CREDENTIALS_DIRECTORY": d, "EVDASH_REDIRECT_URI": REDIRECT,
                "EVDASH_SCOPES": "scope:one scope:two"}
        base.update(extra)
        return base

    def test_loads_from_credentials_directory(self):
        with tempfile.TemporaryDirectory() as d:
            s = config.load(self.env(d))
        self.assertEqual(s.oauth.client_secret, SECRET)
        self.assertEqual(s.listen_port, 8100)

    def test_secret_in_environment_value_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.env(d)
            (Path(d) / "client_secret").unlink()
            env["EVDASH_CLIENT_SECRET"] = SECRET
            with self.assertRaises(config.ConfigError):
                config.load(env)

    def test_plain_http_token_url_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(config.ConfigError):
                config.load(self.env(d, EVDASH_TOKEN_URL="http://idp.example.test/token"))

    def test_http_redirect_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(config.ConfigError):
                config.load(self.env(d, EVDASH_REDIRECT_URI="http://localhost:8080/auth/callback"))

    def test_errors_never_contain_values(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.env(d)
            (Path(d) / "client_secret").write_text("")
            with self.assertRaises(config.ConfigError) as ctx:
                config.load(env)
            self.assertNotIn(SECRET, str(ctx.exception))


class Live(unittest.TestCase):
    """The real request handler on a loopback port, with a scripted identity provider."""

    def start(self, replies):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.idp = FakeIdP(replies)
        self.store = auth.TokenStore(Path(self.dir.name) / "tokens.json")
        manager = auth.TokenManager(make_cfg(), self.store, post=self.idp)
        self.pending = auth.PendingSignIns()
        self.server = make_server(App(make_cfg(), manager, self.pending), "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

        self.log = io.StringIO()
        handler = logging.StreamHandler(self.log)
        logger = logging.getLogger("evdash.server")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.removeHandler, handler)

    def get(self, target):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", target)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp

    def login(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/auth/login")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 303)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(resp.getheader("Location")).query)
        return q["state"][0]

    def test_full_sign_in(self):
        self.start([(200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 300})])
        state = self.login()
        resp = self.get(f"/auth/callback?code={CODE}&state={state}")
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/auth/done"))
        self.assertEqual(self.store.load()["refresh_token"], REFRESH)
        self.assertEqual(self.idp.calls[0][1]["code"], CODE)
        # the code is verified against the verifier that matches the challenge we issued
        self.assertTrue(self.idp.calls[0][1]["code_verifier"])

    def test_access_log_has_no_query_string_or_secrets(self):
        self.start([(200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 300})])
        state = self.login()
        self.get(f"/auth/callback?code={CODE}&state={state}")
        self.get("/auth/done")
        text = self.log.getvalue()
        for needle in (CODE, state, ACCESS, REFRESH, "?", "code="):
            self.assertNotIn(needle, text, needle)
        self.assertIn("/auth/callback", text)

    def test_malformed_request_line_error_is_scrubbed(self):
        self.start([])
        import socket
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(f"GET /auth/callback?code={CODE} BROKEN LINE HTTP/9.9\r\n\r\n".encode())
            reply = s.recv(4096)
        # An unsupported HTTP version is answered HTTP/0.9-style (body only); the point is that
        # the handler answers instead of dying while logging.
        self.assertIn(b"505", reply)
        self.assertNotIn(CODE, self.log.getvalue())

    def test_wrong_state_is_refused_and_nothing_is_exchanged(self):
        self.start([])
        self.login()
        resp = self.get(f"/auth/callback?code={CODE}&state=test-data-forged")
        self.assertEqual(resp.getheader("Location"), "/auth/failed")
        self.assertEqual(self.idp.calls, [])
        self.assertIsNone(self.store.load())

    def test_state_cannot_be_replayed(self):
        self.start([(200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 300})])
        state = self.login()
        self.get(f"/auth/callback?code={CODE}&state={state}")
        resp = self.get(f"/auth/callback?code={CODE}&state={state}")
        self.assertEqual(resp.getheader("Location"), "/auth/failed")
        self.assertEqual(len(self.idp.calls), 1)

    def test_provider_error_redirects_to_failed(self):
        self.start([])
        state = self.login()
        resp = self.get(f"/auth/callback?error=access_denied&state={state}")
        self.assertEqual(resp.getheader("Location"), "/auth/failed")

    def test_rejected_exchange_redirects_to_failed(self):
        self.start([(400, {"error": "invalid_grant"})])
        state = self.login()
        resp = self.get(f"/auth/callback?code={CODE}&state={state}")
        self.assertEqual(resp.getheader("Location"), "/auth/failed")
        self.assertIsNone(self.store.load())
        self.assertNotIn(CODE, self.log.getvalue())

    def test_pages_carry_privacy_headers_and_no_scripts(self):
        self.start([])
        resp = self.get("/auth/done")
        self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("Content-Security-Policy"),
                         "default-src 'none'; form-action 'self'")

    def test_get_login_page_does_not_start_a_sign_in(self):
        self.start([])
        for _ in range(40):
            self.assertEqual(self.get("/auth/login").status, 200)
        state = self.login()  # still works: the GETs used up no slots
        self.assertIsNotNone(self.pending.take(state))

    def test_unknown_path_404(self):
        self.start([])
        self.assertEqual(self.get("/nope").status, 404)


if __name__ == "__main__":
    unittest.main()
