"""Fetch the Flex statement over the wire (SPEC §4.1, §13.3, issue #25).

The network path here actively lies, so every load-bearing branch has a test:

* the ``GetStatement`` host is taken from the ``SendRequest`` *response*, and
  both hosts are resolved over DoH, per host, per run;
* interception is caught by *mismatch against the DoH answer*, never by matching
  a known bad address;
* an HTTP 200 carrying an XML error body is a failure, and an empty body is an
  error with *its own branch*, distinct from the 1012/1015/1013 code family;
* a transient "not ready" code (1019) is retried, a portal hard-stop is not;
* the token is resolved through the one secret indirection.
"""

import unittest
from datetime import date

from journal import flex, flex_client, secrets

SEND_HOST = "ndcdyn.interactivebrokers.com"
GET_HOST = "gdcdyn.interactivebrokers.com"
GET_URL = (
    "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
)

SEND_OK = (
    '<FlexStatementResponse timestamp="x">'
    "<Status>Success</Status>"
    "<ReferenceCode>REF123</ReferenceCode>"
    f"<Url>{GET_URL}</Url>"
    "</FlexStatementResponse>"
)
STATEMENT = '<FlexQueryResponse queryName="q" type="AF"><FlexStatements></FlexStatements></FlexQueryResponse>'


def _fail(code, message):
    return (
        '<FlexStatementResponse timestamp="x"><Status>Fail</Status>'
        f"<ErrorCode>{code}</ErrorCode><ErrorMessage>{message}</ErrorMessage>"
        "</FlexStatementResponse>"
    )


class _FakeResolver:
    """DoH stand-in: fixed addresses per host, and a log of what it resolved."""

    def __init__(self, table):
        self._table = table
        self.resolved = []

    def resolve(self, host):
        self.resolved.append(host)
        return self._table[host]


class _FakeTransport:
    """Serves canned bodies by URL substring; ``connected_ip`` per host.

    Each URL is matched to a queue of responses so a poll can return 1019 then
    the statement. ``connected_ip`` defaults to a DoH-matching address.
    """

    def __init__(self, send_bodies, get_bodies, ip_for_host):
        self._queues = {"SendRequest": list(send_bodies), "GetStatement": list(get_bodies)}
        self._ip_for_host = ip_for_host
        self.urls = []
        self.addresses = []

    def get(self, url, addresses):
        self.urls.append(url)
        self.addresses.append(list(addresses))
        key = "SendRequest" if "SendRequest" in url else "GetStatement"
        body = self._queues[key].pop(0)
        host = SEND_HOST if key == "SendRequest" else GET_HOST
        return flex_client.HttpResponse(text=body, connected_ip=self._ip_for_host(host))


def _client(resolver, transport, token="tok", **kw):
    return flex_client.FlexClient(
        resolver=resolver,
        transport=transport,
        resolve_secret=lambda name: token,
        sleep=lambda _s: None,
        **kw,
    )


def _honest_ip(table):
    return lambda host: table[host][0]


class HappyPathTest(unittest.TestCase):
    def setUp(self):
        self.table = {SEND_HOST: ["1.1.1.1"], GET_HOST: ["2.2.2.2"]}
        self.resolver = _FakeResolver(self.table)
        self.transport = _FakeTransport([SEND_OK], [STATEMENT], _honest_ip(self.table))
        self.client = _client(self.resolver, self.transport)

    def test_returns_the_statement_xml(self):
        self.assertEqual(self.client.fetch_statement("QUERYID"), STATEMENT)

    def test_get_host_comes_from_the_send_response(self):
        # The second host is named in the first host's response, and it is the
        # one that gets resolved for the GetStatement leg.
        self.client.fetch_statement("QUERYID")
        self.assertEqual(self.resolver.resolved, [SEND_HOST, GET_HOST])

    def test_token_and_query_ride_the_send_url(self):
        self.client.fetch_statement("QUERYID")
        send_url = self.transport.urls[0]
        self.assertIn("t=tok", send_url)
        self.assertIn("q=QUERYID", send_url)
        self.assertIn("v=3", send_url)

    def test_reference_code_rides_the_get_url(self):
        self.client.fetch_statement("QUERYID")
        get_url = self.transport.urls[1]
        self.assertIn("q=REF123", get_url)
        self.assertIn("v=3", get_url)

    def test_token_resolved_through_the_named_indirection(self):
        seen = []
        client = flex_client.FlexClient(
            resolver=self.resolver,
            transport=self.transport,
            resolve_secret=lambda name: seen.append(name) or "tok",
            sleep=lambda _s: None,
        )
        client.fetch_statement("QUERYID")
        self.assertEqual(seen, [secrets.IBKR_FLEX_TOKEN])


class InterceptionTest(unittest.TestCase):
    def test_mismatch_against_doh_answer_is_interception(self):
        table = {SEND_HOST: ["1.1.1.1"], GET_HOST: ["2.2.2.2"]}
        resolver = _FakeResolver(table)
        # The connection lands on an address DoH never returned — the block page.
        transport = _FakeTransport([SEND_OK], [STATEMENT], lambda host: "203.0.113.9")
        client = _client(resolver, transport)
        with self.assertRaises(flex_client.InterceptionError):
            client.fetch_statement("QUERYID")

    def test_a_doh_listed_address_is_accepted(self):
        table = {SEND_HOST: ["1.1.1.1", "1.1.1.2"], GET_HOST: ["2.2.2.2"]}
        resolver = _FakeResolver(table)
        # Lands on the *second* DoH address — still a match, not interception.
        ip = {SEND_HOST: "1.1.1.2", GET_HOST: "2.2.2.2"}
        transport = _FakeTransport([SEND_OK], [STATEMENT], lambda host: ip[host])
        client = _client(resolver, transport)
        self.assertEqual(client.fetch_statement("QUERYID"), STATEMENT)


class ErrorBranchTest(unittest.TestCase):
    def setUp(self):
        self.table = {SEND_HOST: ["1.1.1.1"], GET_HOST: ["2.2.2.2"]}
        self.resolver = _FakeResolver(self.table)

    def _client_for(self, send_bodies, get_bodies):
        transport = _FakeTransport(send_bodies, get_bodies, _honest_ip(self.table))
        return _client(self.resolver, transport)

    def test_empty_body_has_its_own_branch(self):
        # Distinct from the code family: EmptyResponseError is not a FlexError.
        client = self._client_for([""], [STATEMENT])
        with self.assertRaises(flex_client.EmptyResponseError):
            client.fetch_statement("QUERYID")
        self.assertFalse(issubclass(flex_client.EmptyResponseError, flex.FlexError))

    def test_empty_statement_body_is_an_error_not_a_value(self):
        client = self._client_for([SEND_OK], ["   \n  "])
        with self.assertRaises(flex_client.EmptyResponseError):
            client.fetch_statement("QUERYID")

    def test_expired_token_is_a_flex_error(self):
        # 1012 on SendRequest — a portal hard-stop, surfaced not retried.
        client = self._client_for([_fail("1012", "Token has expired.")], [STATEMENT])
        with self.assertRaises(flex.FlexError) as ctx:
            client.fetch_statement("QUERYID")
        self.assertIn("1012", str(ctx.exception))

    def test_invalid_token_1015_is_a_flex_error(self):
        client = self._client_for([_fail("1015", "Token is invalid.")], [STATEMENT])
        with self.assertRaises(flex.FlexError):
            client.fetch_statement("QUERYID")

    def test_transient_not_ready_is_retried_then_succeeds(self):
        # 1019 "generation in progress" is the designed happy path between the
        # two calls: poll until the statement lands.
        transport = _FakeTransport(
            [SEND_OK],
            [_fail("1019", "in progress"), STATEMENT],
            _honest_ip(self.table),
        )
        client = _client(self.resolver, transport)
        self.assertEqual(client.fetch_statement("QUERYID"), STATEMENT)

    def test_persistent_transient_gives_up_as_a_flex_error(self):
        transport = _FakeTransport(
            [SEND_OK],
            [_fail("1019", "in progress")] * 5,
            _honest_ip(self.table),
        )
        client = _client(self.resolver, transport, retries=3)
        with self.assertRaises(flex.FlexError):
            client.fetch_statement("QUERYID")


class TokenExpiryNoticeTest(unittest.TestCase):
    def setUp(self):
        self.table = {SEND_HOST: ["1.1.1.1"], GET_HOST: ["2.2.2.2"]}
        self.resolver = _FakeResolver(self.table)
        self.transport = _FakeTransport([SEND_OK], [STATEMENT], _honest_ip(self.table))
        self.warnings = []

    def _client(self, today):
        return flex_client.FlexClient(
            resolver=self.resolver,
            transport=self.transport,
            resolve_secret=lambda name: "tok",
            sleep=lambda _s: None,
            warn=self.warnings.append,
            today=today,
        )

    def test_near_expiry_surfaces_the_date_before_it_bites(self):
        self._client(date(2027, 7, 1)).fetch_statement("QUERYID")
        self.assertTrue(any("2027-07-14" in w for w in self.warnings))

    def test_far_from_expiry_stays_quiet(self):
        self._client(date(2026, 8, 13)).fetch_statement("QUERYID")
        self.assertEqual(self.warnings, [])


if __name__ == "__main__":
    unittest.main()
