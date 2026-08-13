"""DNS-over-HTTPS resolution: per host, per call, never cached (SPEC §13.3).

The resolver turns a hostname into the set of addresses the ISP's resolver is
*not* allowed to disagree with. Two facts are load-bearing: only A records
count (an interception check compares IPv4 to IPv4), and every call is a fresh
lookup — the Akamai edge rotated between two lookups minutes apart, so a cached
answer would be wrong, not just stale.
"""

import unittest

from journal import doh


class _FakeJson:
    """A stand-in for the DoH HTTP GET: records calls, returns queued answers."""

    def __init__(self, answer):
        self._answer = answer
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        return self._answer


class DohResolverTest(unittest.TestCase):
    def test_returns_a_record_addresses(self):
        # RFC 8484 JSON: type 1 is an A record; the resolver keeps those.
        get = _FakeJson(
            {
                "Status": 0,
                "Answer": [
                    {"name": "ndcdyn.interactivebrokers.com", "type": 1, "data": "1.2.3.4"},
                    {"name": "ndcdyn.interactivebrokers.com", "type": 1, "data": "1.2.3.5"},
                ],
            }
        )
        resolver = doh.DohResolver(get)
        self.assertEqual(
            resolver.resolve("ndcdyn.interactivebrokers.com"),
            ["1.2.3.4", "1.2.3.5"],
        )

    def test_ignores_non_a_records(self):
        # A CNAME (type 5) is not an address; the mismatch check needs IPv4.
        get = _FakeJson(
            {
                "Status": 0,
                "Answer": [
                    {"type": 5, "data": "edge.akamai.net."},
                    {"type": 1, "data": "9.9.9.9"},
                ],
            }
        )
        self.assertEqual(doh.DohResolver(get).resolve("host"), ["9.9.9.9"])

    def test_each_resolve_is_a_fresh_lookup(self):
        # Never cached: the edge rotates, so two calls hit the wire twice.
        get = _FakeJson({"Status": 0, "Answer": [{"type": 1, "data": "1.1.1.1"}]})
        resolver = doh.DohResolver(get)
        resolver.resolve("host")
        resolver.resolve("host")
        self.assertEqual(len(get.calls), 2)

    def test_query_names_the_host_and_asks_for_a_records(self):
        get = _FakeJson({"Status": 0, "Answer": [{"type": 1, "data": "1.1.1.1"}]})
        doh.DohResolver(get).resolve("gdcdyn.interactivebrokers.com")
        (url,) = get.calls
        self.assertIn("name=gdcdyn.interactivebrokers.com", url)
        self.assertIn("type=A", url)

    def test_empty_answer_is_a_resolution_failure(self):
        get = _FakeJson({"Status": 3, "Answer": []})
        with self.assertRaises(doh.DohError):
            doh.DohResolver(get).resolve("nxdomain.example")

    def test_no_answer_key_is_a_resolution_failure(self):
        with self.assertRaises(doh.DohError):
            doh.DohResolver(_FakeJson({"Status": 2})).resolve("host")


if __name__ == "__main__":
    unittest.main()
