"""HTTPS transport with ordinary PKI or an explicitly trusted certificate pin.

A pin must be obtained from a DSM certificate export or another trusted
channel. This module never learns a pin from a network connection. Pinning
trusts that exact DER certificate instead of CA/hostname validation; neither
mode permits redirects or environment-selected proxies.
"""

from __future__ import annotations

from functools import partial
import hashlib
import hmac
import http.client
import os
import re
import ssl
from typing import Any
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    build_opener,
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, expected_digest: bytes, **kwargs: Any) -> None:
        self._expected_digest = expected_digest
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        # HTTPConnection.send invokes connect before transmitting its buffered
        # headers or body. The comparison therefore happens before credentials
        # or even an HTTP request line reach an untrusted TLS peer.
        try:
            super().connect()
            certificate = self.sock.getpeercert(binary_form=True) if self.sock else None
            if not certificate or not hmac.compare_digest(
                hashlib.sha256(certificate).digest(), self._expected_digest
            ):
                raise ssl.SSLError("The DSM certificate fingerprint does not match the trusted fingerprint.")
        except BaseException:
            self.close()
            raise


class _PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, expected_digest: bytes, context: ssl.SSLContext) -> None:
        self._expected_digest = expected_digest
        super().__init__(context=context)

    def https_open(self, req: Any) -> Any:
        connection = partial(_PinnedHTTPSConnection, expected_digest=self._expected_digest)
        return self.do_open(connection, req, context=self._context)


def make_opener(
    ca_file: str | os.PathLike[str] | None = None,
    cert_sha256: str | None = None,
) -> OpenerDirector:
    """Build a no-proxy/no-redirect opener with exactly one trust mechanism.

    ``cert_sha256`` accepts 64 hexadecimal characters, optionally separated by
    colons. No certificate is fetched or accepted automatically. A supplied
    pin and CA file are mutually exclusive to avoid ambiguous trust rules.
    """
    if ca_file is not None and cert_sha256 is not None:
        raise ValueError("ca_file and cert_sha256 are mutually exclusive.")
    if cert_sha256 is None:
        context = ssl.create_default_context(cafile=ca_file)
        handler: HTTPSHandler = HTTPSHandler(context=context)
    else:
        if not isinstance(cert_sha256, str):
            raise ValueError("cert_sha256 must contain exactly 64 hexadecimal characters.")
        normalized = cert_sha256.strip().replace(":", "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", normalized):
            raise ValueError("cert_sha256 must contain exactly 64 hexadecimal characters.")
        expected_digest = bytes.fromhex(normalized)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        handler = _PinnedHTTPSHandler(expected_digest, context)
    return build_opener(ProxyHandler({}), _NoRedirect(), handler)
