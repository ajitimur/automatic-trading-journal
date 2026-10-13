"""The relay client speaks the pull/acknowledge contract over DoH (#49).

The resolver and transport are faked, as the Flex client's are: what matters is
that each call carries the Mac secret, resolves its host over DoH and connects
only there, and that a pull body is read as the shared fixture's shape.
"""

import json
import os
import unittest

from journal import flex_client, relay_client
from journal.flex_client import HttpResponse

FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "docs", "samples", "relay-pull-fixture.json"
)
BASE = "https://relay.example.workers.dev"
HOST = "relay.example.workers.dev"
ADDRESSES = ["203.0.113.7"]


class FakeResolver:
    def __init__(self, addresses=ADDRESSES):
        self.addresses = addresses
        self.asked = []

    def resolve(self, host):
        self.asked.append(host)
        return list(self.addresses)


class FakeTransport:
    def __init__(self, text="", connected_ip=ADDRESSES[0]):
        self.text = text
        self.connected_ip = connected_ip
        self.calls = []

    def request(self, method, url, addresses, *, headers=None, body=None):
        self.calls.append(
            {"method": method, "url": url, "addresses": list(addresses),
             "headers": dict(headers or {}), "body": body}
        )
        return HttpResponse(text=self.text, connected_ip=self.connected_ip)


def _client(transport, resolver=None, secret="mac-secret"):
    return relay_client.RelayClient(
        BASE,
        resolver=resolver or FakeResolver(),
        transport=transport,
        resolve_secret=lambda name: {relay_client.secrets.RELAY_MAC_SECRET: secret}[name],
    )


class PullTest(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            self.fixture_text = fh.read()

    def test_pull_returns_the_items_of_the_shared_fixture(self):
        transport = FakeTransport(self.fixture_text)
        items = _client(transport).pull()
        self.assertEqual(items, json.loads(self.fixture_text)["items"])

    def test_pull_is_a_get_carrying_the_mac_secret(self):
        transport = FakeTransport(self.fixture_text)
        _client(transport, secret="s3cret").pull()

        [call] = transport.calls
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], f"{BASE}/pull")
        self.assertEqual(call["headers"]["Authorization"], "Bearer s3cret")

    def test_pull_connects_only_to_the_doh_answer_for_the_relay_host(self):
        resolver = FakeResolver()
        transport = FakeTransport(self.fixture_text)
        _client(transport, resolver=resolver).pull()

        self.assertEqual(resolver.asked, [HOST])
        self.assertEqual(transport.calls[0]["addresses"], ADDRESSES)

    def test_a_connection_landing_off_the_doh_answer_is_interception(self):
        transport = FakeTransport(self.fixture_text, connected_ip="10.0.0.1")
        with self.assertRaises(flex_client.InterceptionError):
            _client(transport).pull()

    def test_an_empty_pull_body_is_an_error_not_an_empty_relay(self):
        with self.assertRaises(flex_client.EmptyResponseError):
            _client(FakeTransport("")).pull()

    def test_a_body_that_is_not_the_pull_payload_is_an_error(self):
        for body in ("<html>blocked</html>", '{"nope": []}', '{"items": {}}'):
            with self.subTest(body=body):
                with self.assertRaises(relay_client.RelayError):
                    _client(FakeTransport(body)).pull()

    def test_a_trailing_slash_on_the_base_url_is_tolerated(self):
        transport = FakeTransport(self.fixture_text)
        relay_client.RelayClient(
            BASE + "/",
            resolver=FakeResolver(),
            transport=transport,
            resolve_secret=lambda name: "x",
        ).pull()
        self.assertEqual(transport.calls[0]["url"], f"{BASE}/pull")


class AcknowledgeTest(unittest.TestCase):
    def test_acknowledge_posts_the_ids_as_json_with_the_mac_secret(self):
        transport = FakeTransport("")
        _client(transport, secret="s3cret").acknowledge(["583201001", "583201002"])

        [call] = transport.calls
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{BASE}/ack")
        self.assertEqual(call["headers"]["Authorization"], "Bearer s3cret")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(call["body"]), {"ids": ["583201001", "583201002"]})

    def test_acknowledge_checks_for_interception_too(self):
        transport = FakeTransport("", connected_ip="10.0.0.1")
        with self.assertRaises(flex_client.InterceptionError):
            _client(transport).acknowledge(["1"])


class DefaultClientTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(relay_client.URL_ENV, None)

    def tearDown(self):
        os.environ.pop(relay_client.URL_ENV, None)
        if self._saved is not None:
            os.environ[relay_client.URL_ENV] = self._saved

    def test_no_url_configured_builds_no_client(self):
        self.assertIsNone(relay_client.build_default_client())

    def test_a_configured_url_builds_a_client(self):
        os.environ[relay_client.URL_ENV] = BASE
        self.assertIsInstance(relay_client.build_default_client(), relay_client.RelayClient)


if __name__ == "__main__":
    unittest.main()
