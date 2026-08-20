"""The one real Flex transport connects only where DoH pointed (issue #25).

The socket and TLS work are patched out — what matters is the contract the
client leans on: the connection goes to a *given* address while the hostname
carries SNI, ``Host`` and certificate verification; the reported address is the
socket's real peer; and a certificate mismatch surfaces as an
``InterceptionError`` rather than escaping as an ``ssl`` traceback.
"""

import socket
import ssl
import unittest
from unittest import mock

from journal import flex_client, flex_transport

URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/x?t=tok&q=1"
HOST = "ndcdyn.interactivebrokers.com"


class _FakeResponse:
    def __init__(self, body, status=200, reason="OK"):
        self._body = body
        self.status = status
        self.reason = reason

    def read(self):
        return self._body


class _FakeConnection:
    """Stands in for the pinned HTTPS connection, recording how it was built."""

    made = []

    def __init__(self, host, address, **kwargs):
        self.host = host
        self.address = address
        self.kwargs = kwargs
        self.peer_address = address
        self.requests = []
        self.closed = False
        self.response = _FakeResponse(b"<xml/>")
        self.error = None
        _FakeConnection.made.append(self)

    def request(self, method, target, headers=None):
        if self.error is not None:
            raise self.error
        self.requests.append((method, target, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def _patch_connection(configure=None):
    """Patch the connection class, letting a test configure each instance."""

    def factory(host, address, **kwargs):
        conn = _FakeConnection(host, address, **kwargs)
        if configure is not None:
            configure(conn)
        return conn

    _FakeConnection.made = []
    return mock.patch.object(flex_transport, "_PinnedHTTPSConnection", factory)


class PinnedTransportTest(unittest.TestCase):
    def test_connects_to_the_doh_address_and_returns_the_body(self):
        with _patch_connection():
            response = flex_transport.PinnedTransport().get(URL, ["96.17.180.44"])

        self.assertIsInstance(response, flex_client.HttpResponse)
        self.assertEqual(response.text, "<xml/>")
        self.assertEqual(response.connected_ip, "96.17.180.44")

        conn = _FakeConnection.made[0]
        # The hostname stays the hostname — only the TCP destination is pinned.
        self.assertEqual(conn.host, HOST)
        self.assertEqual(conn.address, "96.17.180.44")
        self.assertEqual(conn.requests[0][0], "GET")
        # Path and query survive; the token is not dropped on the floor.
        self.assertEqual(conn.requests[0][1], "/AccountManagement/x?t=tok&q=1")
        # Akamai answers 403 without a User-Agent, so both headers are sent.
        self.assertEqual(
            conn.requests[0][2],
            {"Host": HOST, "User-Agent": flex_transport.USER_AGENT},
        )
        self.assertTrue(conn.closed)

    def test_reports_the_sockets_real_peer_not_the_requested_address(self):
        def redirected(conn):
            conn.peer_address = "114.7.173.246"

        with _patch_connection(redirected):
            response = flex_transport.PinnedTransport().get(URL, ["96.17.180.44"])

        # The transport does not assert the peer is honest — it reports it, and
        # the client's invariant check raises on it.
        self.assertEqual(response.connected_ip, "114.7.173.246")

    def test_certificate_mismatch_is_stated_as_interception(self):
        def bad_cert(conn):
            conn.error = ssl.SSLCertVerificationError(
                "certificate verify failed: Hostname mismatch"
            )

        with _patch_connection(bad_cert):
            with self.assertRaises(flex_client.InterceptionError) as caught:
                flex_transport.PinnedTransport().get(URL, ["96.17.180.44"])

        self.assertIn(HOST, str(caught.exception))
        self.assertIn("fix the network", str(caught.exception))

    def test_a_certificate_failure_does_not_retry_the_next_address(self):
        def bad_cert(conn):
            conn.error = ssl.SSLCertVerificationError("Hostname mismatch")

        with _patch_connection(bad_cert):
            with self.assertRaises(flex_client.InterceptionError):
                flex_transport.PinnedTransport().get(URL, ["1.1.1.1", "2.2.2.2"])

        # Interception is a fact about the path, not about one edge: trying the
        # second address would only bury it.
        self.assertEqual(len(_FakeConnection.made), 1)

    def test_an_unroutable_address_falls_through_to_the_next(self):
        def first_fails(conn):
            if conn.address == "1.1.1.1":
                conn.error = socket.timeout("timed out")

        with _patch_connection(first_fails):
            response = flex_transport.PinnedTransport().get(
                URL, ["1.1.1.1", "2.2.2.2"]
            )

        self.assertEqual(response.connected_ip, "2.2.2.2")
        self.assertEqual(len(_FakeConnection.made), 2)

    def test_every_address_failing_is_a_transport_error_naming_them(self):
        def all_fail(conn):
            conn.error = OSError("no route to host")

        with _patch_connection(all_fail):
            with self.assertRaises(flex_transport.TransportError) as caught:
                flex_transport.PinnedTransport().get(URL, ["1.1.1.1", "2.2.2.2"])

        self.assertIn("1.1.1.1", str(caught.exception))
        self.assertIn("2.2.2.2", str(caught.exception))

    def test_an_empty_doh_answer_never_falls_back_to_the_system_resolver(self):
        with _patch_connection():
            with self.assertRaises(flex_transport.TransportError) as caught:
                flex_transport.PinnedTransport().get(URL, [])

        self.assertIn("system resolver", str(caught.exception))
        self.assertEqual(_FakeConnection.made, [])

    def test_a_non_200_surfaces_its_body_rather_than_swallowing_it(self):
        def blocked(conn):
            conn.response = _FakeResponse(
                b"<html>blocked by your provider</html>", status=403, reason="Forbidden"
            )

        with _patch_connection(blocked):
            with self.assertRaises(flex_transport.TransportError) as caught:
                flex_transport.PinnedTransport().get(URL, ["96.17.180.44"])

        self.assertIn("403", str(caught.exception))
        self.assertIn("blocked by your provider", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
