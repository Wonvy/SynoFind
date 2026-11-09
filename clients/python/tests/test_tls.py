"""Real local HTTPS tests; certificates and keys are generated temporarily.

No DSM connection or real account is used. Tests skip when OpenSSL is not
available, and generated private keys never enter the repository.
"""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synofind_dsm.transport import make_opener


class TLSHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self._respond()

    def do_GET(self):
        self._respond()

    def _respond(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, data))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.server.base_url + "/forbidden-target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required to generate a temporary local TLS certificate")
class TLSTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="synofind-tls-test-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.cert = Path(cls.directory.name) / "loopback-cert.pem"
        cls.key = Path(cls.directory.name) / "loopback-key.pem"
        subprocess.run(
            [shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048",
             "-nodes", "-keyout", str(cls.key), "-out", str(cls.cert),
             "-days", "1", "-subj", "/CN=localhost",
             "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
            check=True, capture_output=True, timeout=30,
        )
        certificate = ssl.PEM_cert_to_DER_cert(cls.cert.read_text(encoding="ascii"))
        cls.fingerprint = hashlib.sha256(certificate).hexdigest()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, cls.key)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), TLSHandler)
        cls.server.daemon_threads = True
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.url = f"https://127.0.0.1:{cls.server.server_port}"
        cls.server.base_url = cls.url
        cls.server.requests = []
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.stop_server)

    @classmethod
    def stop_server(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.server.requests = []

    def request(self, opener, path="/ok"):
        return opener.open(Request(self.url + path,
                                   data=b"synthetic-password=not-a-real-secret",
                                   method="POST"), timeout=3)

    def test_verified_fingerprint_accepts_exact_self_signed_certificate(self):
        opener = make_opener(cert_sha256=self.fingerprint)
        try:
            with self.request(opener) as response:
                self.assertEqual(response.read(), b'{"ok":true}')
            self.assertEqual(len(self.server.requests), 1)
            self.assertEqual(self.server.requests[0][2], b"synthetic-password=not-a-real-secret")
        finally:
            opener.close()

    def test_colon_separated_uppercase_fingerprint_is_supported(self):
        pin = ":".join(self.fingerprint[index:index + 2] for index in range(0, 64, 2)).upper()
        opener = make_opener(cert_sha256=pin)
        try:
            with self.request(opener) as response:
                self.assertEqual(response.status, 200)
        finally:
            opener.close()

    def test_wrong_pin_fails_before_any_http_headers_or_body(self):
        pin = ("0" if self.fingerprint[0] != "0" else "1") + self.fingerprint[1:]
        opener = make_opener(cert_sha256=pin)
        try:
            with self.assertRaises(URLError) as caught:
                self.request(opener)
            self.assertIsInstance(caught.exception.reason, ssl.SSLError)
            self.assertIn("does not match the trusted fingerprint", str(caught.exception.reason))
            self.assertNotIn(pin, str(caught.exception))
            self.assertEqual(self.server.requests, [])
        finally:
            opener.close()

    def test_no_pin_does_not_trust_a_self_signed_certificate(self):
        opener = make_opener()
        try:
            with self.assertRaises(URLError) as caught:
                self.request(opener)
            self.assertIsInstance(caught.exception.reason, ssl.SSLCertVerificationError)
            self.assertEqual(self.server.requests, [])
        finally:
            opener.close()

    def test_explicit_ca_uses_normal_certificate_validation(self):
        opener = make_opener(ca_file=self.cert)
        try:
            with self.request(opener) as response:
                self.assertEqual(response.status, 200)
        finally:
            opener.close()

    def test_redirect_is_not_followed_or_given_credentials(self):
        opener = make_opener(cert_sha256=self.fingerprint)
        try:
            with self.assertRaises(HTTPError) as caught:
                self.request(opener, "/redirect")
            self.assertEqual(caught.exception.code, 302)
            caught.exception.close()
            self.assertEqual([request[1] for request in self.server.requests], ["/redirect"])
        finally:
            opener.close()

    def test_proxy_environment_is_ignored(self):
        with patch.dict(os.environ, {"https_proxy": "http://127.0.0.1:1",
                                     "HTTPS_PROXY": "http://127.0.0.1:1",
                                     "no_proxy": "", "NO_PROXY": ""}):
            opener = make_opener(cert_sha256=self.fingerprint)
            try:
                with self.request(opener) as response:
                    self.assertEqual(response.status, 200)
            finally:
                opener.close()

    def test_invalid_fingerprint_rejected_before_connection(self):
        for value in ("", "a" * 63, "g" * 64, "a" * 65, "a " * 32, 123):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    make_opener(cert_sha256=value)
        self.assertEqual(self.server.requests, [])

    def test_ca_and_pin_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            make_opener(ca_file=self.cert, cert_sha256=self.fingerprint)
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
