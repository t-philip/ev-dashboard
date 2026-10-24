import base64
import hashlib
import json
import logging
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from evdash import auth

# Fictitious values only: nothing here is, or resembles, a real credential.
CID = "test-data-client-id"
SECRET = "test-data-client-secret"
REDIRECT = "https://dashboard.example.test/auth/callback"


def make_cfg(token_url="https://idp.example.test/token"):
    return auth.OAuthConfig(CID, SECRET, REDIRECT, "https://idp.example.test/authorize",
                            token_url, ("scope:one", "scope:two"))


class FakeIdP:
    """Records token-endpoint calls and replies from a script."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, data, headers):
        self.calls.append((url, dict(data), dict(headers)))
        return self.replies.pop(0)


class Pkce(unittest.TestCase):
    def test_rfc7636_appendix_b_vector(self):
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(auth.challenge_for(verifier),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_verifier_length_is_valid(self):
        self.assertTrue(43 <= len(auth.new_verifier()) <= 128)

    def test_authorize_url_has_pkce_and_state_and_no_secret(self):
        url = auth.build_authorize_url(make_cfg(), "st", "ch")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["code_challenge"], ["ch"])
        self.assertEqual(q["state"], ["st"])
        self.assertEqual(q["scope"], ["scope:one scope:two"])
        self.assertNotIn(SECRET, url)


class Pending(unittest.TestCase):
    def test_single_use(self):
        p = auth.PendingSignIns()
        state, _ = p.begin()
        self.assertIsNotNone(p.take(state))
        self.assertIsNone(p.take(state))

    def test_unknown_state(self):
        self.assertIsNone(auth.PendingSignIns().take("nope"))

    def test_expires(self):
        now = [1000.0]
        p = auth.PendingSignIns(ttl=10, clock=lambda: now[0])
        state, _ = p.begin()
        now[0] += 11
        self.assertIsNone(p.take(state))

    def test_bounded(self):
        p = auth.PendingSignIns(limit=3)
        first, _ = p.begin()
        for _ in range(3):
            p.begin()
        self.assertIsNone(p.take(first))


class TokenExchange(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = auth.TokenStore(Path(self.dir.name) / "tokens.json")
        self.now = [5000.0]

    def manager(self, idp):
        return auth.TokenManager(make_cfg(), self.store, post=idp, clock=lambda: self.now[0])

    def test_exchange_sends_basic_header_and_no_secret_in_form(self):
        idp = FakeIdP([(200, {"access_token": "test-data-a1", "refresh_token": "test-data-r1",
                              "expires_in": 120})])
        self.manager(idp).complete_sign_in("test-data-code", "test-data-verifier")
        url, form, headers = idp.calls[0]
        expected = base64.b64encode(f"{CID}:{SECRET}".encode()).decode()
        self.assertEqual(headers["Authorization"], "Basic " + expected)
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code_verifier"], "test-data-verifier")
        self.assertEqual(form["redirect_uri"], REDIRECT)
        self.assertNotIn(SECRET, json.dumps(form))
        self.assertNotIn(SECRET, url)
        saved = self.store.load()
        self.assertEqual(saved["refresh_token"], "test-data-r1")
        self.assertEqual(saved["expires_at"], 5120.0)

    def test_basic_header_urlencodes_credentials(self):
        cfg = auth.OAuthConfig("id:1", "se:cr+et%", REDIRECT, "https://a.example.test/a",
                               "https://a.example.test/t", ("s",))
        idp = FakeIdP([(200, {"access_token": "test-data-a1", "refresh_token": "test-data-r1"})])
        auth.TokenManager(cfg, self.store, post=idp,
                          clock=lambda: 1.0).complete_sign_in("c", "v")
        sent = base64.b64decode(idp.calls[0][2]["Authorization"][6:]).decode()
        self.assertEqual(sent, "id%3A1:se%3Acr%2Bet%25")

    def test_granted_scopes_are_stored_and_survive_a_refresh(self):
        idp = FakeIdP([
            (200, {"access_token": "test-data-a1", "refresh_token": "test-data-r1",
                   "expires_in": 120, "scope": "openid scope:one"}),
            (200, {"access_token": "test-data-a2", "refresh_token": "test-data-r2",
                   "expires_in": 120}),  # a refresh reply that omits scope
        ])
        mgr = self.manager(idp)
        mgr.complete_sign_in("c", "v")
        self.assertEqual(mgr.status()["granted_scopes"], ["openid", "scope:one"])
        self.now[0] += 1000
        mgr.access_token()
        self.assertEqual(self.store.load()["scope"], "openid scope:one")

    def test_odd_scope_value_is_ignored(self):
        idp = FakeIdP([(200, {"access_token": "test-data-a1", "refresh_token": "test-data-r1",
                              "scope": "x\ninjected"})])
        mgr = self.manager(idp)
        mgr.complete_sign_in("c", "v")
        self.assertIsNone(mgr.status()["granted_scopes"])

    def test_exchange_without_refresh_token_is_an_error(self):
        idp = FakeIdP([(200, {"access_token": "test-data-a1", "expires_in": 120})])
        with self.assertRaises(auth.AuthError):
            self.manager(idp).complete_sign_in("c", "v")
        self.assertIsNone(self.store.load())

    def test_rejection_message_carries_status_and_code_only(self):
        body = {"error": "invalid_request", "error_description": "test-data-leaky-detail"}
        with self.assertRaises(auth.AuthError) as ctx:
            self.manager(FakeIdP([(400, body)])).complete_sign_in("c", "v")
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("invalid_request", str(ctx.exception))
        self.assertNotIn("leaky", str(ctx.exception))

    def test_hostile_error_value_is_not_echoed(self):
        with self.assertRaises(auth.AuthError) as ctx:
            self.manager(FakeIdP([(400, {"error": "x" * 500})])).complete_sign_in("c", "v")
        self.assertNotIn("xxxx", str(ctx.exception))

    def test_access_token_reused_while_valid(self):
        self.store.save({"access_token": "test-data-a1", "refresh_token": "test-data-r1",
                         "expires_at": 9000.0})
        idp = FakeIdP([])
        self.assertEqual(self.manager(idp).access_token(), "test-data-a1")
        self.assertEqual(idp.calls, [])

    def test_refresh_saves_the_rotated_pair(self):
        self.store.save({"access_token": "test-data-a1", "refresh_token": "test-data-r1",
                         "expires_at": 5030.0})  # inside the refresh margin
        idp = FakeIdP([(200, {"access_token": "test-data-a2", "refresh_token": "test-data-r2",
                              "expires_in": 300})])
        self.assertEqual(self.manager(idp).access_token(), "test-data-a2")
        self.assertEqual(idp.calls[0][1], {"grant_type": "refresh_token",
                                           "refresh_token": "test-data-r1"})
        self.assertEqual(self.store.load()["refresh_token"], "test-data-r2")

    def test_refresh_keeps_old_refresh_token_if_not_rotated(self):
        self.store.save({"access_token": "test-data-a1", "refresh_token": "test-data-r1",
                         "expires_at": 0})
        idp = FakeIdP([(200, {"access_token": "test-data-a2", "expires_in": 300})])
        self.manager(idp).access_token()
        self.assertEqual(self.store.load()["refresh_token"], "test-data-r1")

    def test_dead_refresh_token_leaves_the_file_and_asks_for_sign_in(self):
        self.store.save({"access_token": "test-data-a1", "refresh_token": "test-data-r1",
                         "expires_at": 0})
        before = self.store.path.read_text()
        with self.assertRaises(auth.TokenInvalid):
            self.manager(FakeIdP([(400, {"error": "invalid_grant"})])).access_token()
        self.assertEqual(self.store.path.read_text(), before)

    def test_not_signed_in(self):
        with self.assertRaises(auth.NotSignedIn):
            self.manager(FakeIdP([])).access_token()

    def test_status_shows_no_token_values(self):
        self.store.save({"access_token": "test-data-a1", "refresh_token": "test-data-r1",
                         "expires_at": 5100.0})
        text = json.dumps(self.manager(FakeIdP([])).status())
        self.assertNotIn("test-data", text)


class TokenFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "tokens.json"
        self.store = auth.TokenStore(self.path)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_file_is_owner_only(self):
        self.store.save({"access_token": "test-data-a1"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_crash_during_replace_keeps_old_file_and_leaves_no_temp(self):
        self.store.save({"access_token": "test-data-old", "expires_at": 1.0})
        with mock.patch("evdash.auth.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.store.save({"access_token": "test-data-new", "expires_at": 2.0})
        self.assertEqual(self.store.load()["access_token"], "test-data-old")
        self.assertEqual([p.name for p in Path(self.dir.name).iterdir()
                          if p.name.endswith(".tmp")], [])

    def test_wrong_shape_is_an_auth_error(self):
        for text in ('{}', '[]', '{"access_token": 1, "expires_at": 1}',
                     '{"access_token": "x", "expires_at": "soon"}'):
            self.path.write_text(text)
            with self.assertRaises(auth.AuthError, msg=text):
                self.store.load()

    def test_corrupt_file_is_an_auth_error_not_a_crash(self):
        self.path.write_text("{not json")
        with self.assertRaises(auth.AuthError):
            self.store.load()


class _FakeTokenEndpoint(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        type(self).seen.append((self.path, self.headers.get("Authorization"),
                                self.rfile.read(length).decode()))
        body = json.dumps({"access_token": "test-data-a1", "refresh_token": "test-data-r1",
                           "expires_in": 120}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealHttpExchange(unittest.TestCase):
    """The default HTTP client against a local fake endpoint."""

    def test_post_form_end_to_end(self):
        _FakeTokenEndpoint.seen = []
        srv = HTTPServer(("127.0.0.1", 0), _FakeTokenEndpoint)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(f"http://127.0.0.1:{srv.server_address[1]}/token")
            mgr = auth.TokenManager(cfg, auth.TokenStore(Path(d) / "t.json"))
            mgr.complete_sign_in("test-data-code", "test-data-verifier")
        path, header, body = _FakeTokenEndpoint.seen[0]
        self.assertEqual(path, "/token")
        self.assertTrue(header.startswith("Basic "))
        self.assertNotIn(SECRET, path + body)

    def test_unreachable_endpoint_is_an_auth_error(self):
        with self.assertRaises(auth.AuthError):
            auth.post_form("http://127.0.0.1:1/token", {}, {}, timeout=2)


if __name__ == "__main__":
    unittest.main()
