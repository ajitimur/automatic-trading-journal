"""The one real Flex transport reports the system-resolved address (issue #25).

The socket and HTTP calls are patched out — what matters is the contract the
interception check leans on: the ``connected_ip`` is the address the *system*
resolver returned (the interceptable path), and the body is the decoded HTTP
response.
"""

import io
import socket
import unittest
import urllib.request
from unittest import mock

from journal import flex_client, flex_transport


class _FakeHttp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SystemTransportTest(unittest.TestCase):
    def test_reports_system_address_and_decoded_body(self):
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 443))]
        with mock.patch.object(socket, "getaddrinfo", return_value=addrinfo) as gai, \
                mock.patch.object(
                    urllib.request, "urlopen", return_value=_FakeHttp(b"<xml/>")
                ):
            response = flex_transport.SystemTransport().get(
                "https://ndcdyn.interactivebrokers.com/x?t=tok"
            )
        self.assertIsInstance(response, flex_client.HttpResponse)
        self.assertEqual(response.connected_ip, "1.2.3.4")
        self.assertEqual(response.text, "<xml/>")
        # The host, not the full URL, is what the system resolver is asked for.
        self.assertEqual(gai.call_args.args[0], "ndcdyn.interactivebrokers.com")


if __name__ == "__main__":
    unittest.main()
