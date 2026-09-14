#!/usr/bin/env python3
"""Offline tests for the Embedded Setup oauth_token -> gpmc auth data flow.

A tiny local HTTP server stands in for https://android.clients.google.com/auth,
so nothing here touches the network or a real Google account.

Run with:  python3 tests/test_google_auth.py
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.google_auth import (  # noqa: E402  (path juggling above)
    AuthError,
    build_auth_data,
    create_auth_data,
    exchange_oauth_token,
    mask_auth_data,
    normalize_oauth_token,
    parse_auth_response,
    read_auth_data_file,
    write_auth_data_file,
)

OK_BODY = (
    "SID=fake-sid\n"
    "LSID=fake-lsid\n"
    "Token=aas_et/FAKE-MASTER-TOKEN\n"
    "Email=tester@example.com\n"
    "services=mail,photos\n"
)


class _Handler(BaseHTTPRequestHandler):
    body = OK_BODY
    status = 200
    last_form: dict = {}

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        _Handler.last_form = {key: value[0] for key, value in parse_qs(raw).items()}
        payload = _Handler.body.encode("utf-8")
        self.send_response(_Handler.status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # silence the test server
        pass


class GoogleAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/auth"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _Handler.body = OK_BODY
        _Handler.status = 200
        _Handler.last_form = {}

    # ---- input handling ----

    def test_normalize_accepts_bare_and_prefixed_tokens(self) -> None:
        self.assertEqual(normalize_oauth_token("  oauth2_4/0AfakeTOKENvalue "), "oauth2_4/0AfakeTOKENvalue")
        self.assertEqual(
            normalize_oauth_token("oauth_token=oauth2_4/0AfakeTOKENvalue"),
            "oauth2_4/0AfakeTOKENvalue",
        )

    def test_normalize_rejects_garbage(self) -> None:
        with self.assertRaises(AuthError):
            normalize_oauth_token("short")
        with self.assertRaises(AuthError):
            normalize_oauth_token("androidId=abc&Email=a%40b.com&Token=aas_et/x")

    def test_parse_auth_response_keeps_values_with_equals(self) -> None:
        parsed = parse_auth_response("Token=aas_et/a=b\n\nEmail=a@b.com\n")
        self.assertEqual(parsed["Token"], "aas_et/a=b")
        self.assertEqual(parsed["Email"], "a@b.com")

    # ---- exchange ----

    def test_exchange_posts_the_play_services_form(self) -> None:
        result = exchange_oauth_token(
            "oauth2_4/0AfakeTOKENvalue", endpoint=self.endpoint, timeout=10
        )
        self.assertEqual(result["email"], "tester@example.com")
        self.assertEqual(result["masterToken"], "aas_et/FAKE-MASTER-TOKEN")
        form = _Handler.last_form
        self.assertEqual(form["service"], "ac2dm")
        self.assertEqual(form["add_account"], "1")
        self.assertEqual(form["ACCESS_TOKEN"], "1")
        self.assertEqual(form["Token"], "oauth2_4/0AfakeTOKENvalue")
        self.assertEqual(form["accountType"], "HOSTED_OR_GOOGLE")
        self.assertIn("droidguard_results", form)
        self.assertEqual(len(form["androidId"]), 16)
        int(form["androidId"], 16)  # random 64 bit device id, hex encoded
        self.assertEqual(result["androidId"], form["androidId"])

    def test_exchange_reuses_an_explicit_android_id(self) -> None:
        result = exchange_oauth_token(
            "oauth2_4/0AfakeTOKENvalue",
            android_id="0123456789abcdef",
            endpoint=self.endpoint,
            timeout=10,
        )
        self.assertEqual(result["androidId"], "0123456789abcdef")

    def test_exchange_maps_known_errors(self) -> None:
        _Handler.body = "Error=BadAuthentication\n"
        with self.assertRaises(AuthError) as ctx:
            exchange_oauth_token("oauth2_4/0AfakeTOKENvalue", endpoint=self.endpoint, timeout=10)
        self.assertIn("EmbeddedSetup", str(ctx.exception))

    def test_exchange_reports_unknown_errors(self) -> None:
        _Handler.body = "Error=SomethingElse\n"
        with self.assertRaises(AuthError) as ctx:
            exchange_oauth_token("oauth2_4/0AfakeTOKENvalue", endpoint=self.endpoint, timeout=10)
        self.assertIn("SomethingElse", str(ctx.exception))

    def test_exchange_requires_a_master_token(self) -> None:
        _Handler.body = "Email=tester@example.com\n"
        with self.assertRaises(AuthError) as ctx:
            exchange_oauth_token("oauth2_4/0AfakeTOKENvalue", endpoint=self.endpoint, timeout=10)
        self.assertIn("master token", str(ctx.exception))

    def test_exchange_rejects_http_errors(self) -> None:
        _Handler.status = 403
        _Handler.body = "nope\n"
        with self.assertRaises(AuthError) as ctx:
            exchange_oauth_token("oauth2_4/0AfakeTOKENvalue", endpoint=self.endpoint, timeout=10)
        self.assertIn("403", str(ctx.exception))

    # ---- auth data ----

    def test_auth_data_is_what_gpmc_expects(self) -> None:
        created = create_auth_data(
            "oauth2_4/0AfakeTOKENvalue", endpoint=self.endpoint, timeout=10
        )
        fields = {
            key: value[0]
            for key, value in parse_qs(created["authData"], keep_blank_values=True).items()
        }
        self.assertEqual(fields["Email"], "tester@example.com")
        self.assertEqual(fields["Token"], "aas_et/FAKE-MASTER-TOKEN")
        self.assertEqual(fields["app"], "com.google.android.apps.photos")
        self.assertIn("photos.native", fields["service"])
        self.assertEqual(fields["androidId"], created["androidId"])
        # the sidecar reads the account out of the auth data the same way gpmc does
        self.assertIn("Email=tester%40example.com", created["authData"])

    def test_mask_hides_the_master_token(self) -> None:
        auth_data = build_auth_data("tester@example.com", "aas_et/SUPER-SECRET-VALUE", "deadbeefdeadbeef")
        masked = mask_auth_data(auth_data)
        self.assertNotIn("SUPER-SECRET-VALUE", masked)
        self.assertIn("tester@example.com", masked)

    def test_file_roundtrip_is_owner_only(self) -> None:
        auth_data = build_auth_data("tester@example.com", "aas_et/TOKEN", "deadbeefdeadbeef")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "auth_data"
            written = write_auth_data_file(target, auth_data)
            self.assertEqual(written, target)
            self.assertEqual(read_auth_data_file(target), auth_data)
            mode = stat.S_IMODE(os.stat(target).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(read_auth_data_file(Path(tmp) / "missing"), "")

    def test_config_falls_back_to_the_auth_data_file(self) -> None:
        from app.config import Config

        auth_data = build_auth_data("tester@example.com", "aas_et/TOKEN", "deadbeefdeadbeef")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "auth_data"
            write_auth_data_file(target, auth_data)
            saved = {key: os.environ.get(key) for key in ("GPMC_AUTH_DATA", "GP_AUTH_DATA", "GOTOHP_AUTH_STRING", "GPMC_AUTH_DATA_FILE")}
            try:
                for key in ("GPMC_AUTH_DATA", "GP_AUTH_DATA", "GOTOHP_AUTH_STRING"):
                    os.environ.pop(key, None)
                os.environ["GPMC_AUTH_DATA_FILE"] = str(target)
                cfg = Config.from_env()
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            self.assertEqual(cfg.gpmc_auth_data, auth_data)
            self.assertEqual(cfg.gpmc_auth_data_file, str(target))
            self.assertTrue(cfg.gpmc_configured)


if __name__ == "__main__":
    unittest.main(verbosity=2)
