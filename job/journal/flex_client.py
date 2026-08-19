"""Fetch the Flex statement over the wire (SPEC §4.1, §13.3, issue #25).

This is the riskiest single piece in the build: the network path actively lies.
The client is a thin, response-driven state machine over the two-step Flex Web
Service v3 protocol, with three defences the SPEC calls out by name:

* **DoH per host, per run.** ``SendRequest`` and ``GetStatement`` sit on
  *different* hosts (``ndcdyn`` → ``gdcdyn``), and the second is named in the
  first's response — so each leg resolves its own host over DoH freshly, never
  cached (the Akamai edge rotates minutes apart).
* **The DoH answer is the destination, not just a reference.** The transport
  connects to the addresses resolved here, so the system resolver is never on
  the path; the certificate is still verified against the hostname, which
  catches an interceptor that manages to answer on a DoH-named address. The
  address the socket actually reached must be one DoH returned — an invariant,
  checked by mismatch and never against a known bad address, since the ISP's
  block address has already moved once.
* **HTTP 200 is not success.** Flex signals failure with an XML error body under
  a 200, so status codes are blind. An empty body is an error with *its own
  branch* — "fix the network" — distinct from the 1012/1015/1013 code family,
  which says "go to the portal". Transient "not ready" codes (1019 &c.) are the
  designed handshake between the two calls and are polled, not raised.

The DoH resolver, the HTTP transport and the secret lookup are all injected, so
every branch above is tested without a socket. The one real transport lives in
:mod:`journal.flex_transport`.
"""

from __future__ import annotations

import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional, Protocol, Sequence, Tuple

from . import secrets
from .flex import FlexError

# Flex Web Service v3. ``v`` defaults to 2 if omitted, so it is always sent.
SEND_REQUEST_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
)
FLEX_VERSION = "3"

# "Not ready" codes: the report is still being generated or the servers are
# busy. 1019 is the *designed* state between SendRequest and GetStatement. These
# are polled with backoff, never surfaced (docs/research/ibkr-trade-export.md).
RETRYABLE_CODES = {
    "1001", "1004", "1005", "1006", "1007", "1008", "1009", "1019", "1021",
}

# How close to token expiry before the client states the fact. Regenerating the
# token invalidates the current one (§13.4), so a human must rotate it in time.
TOKEN_EXPIRY_WARN_DAYS = 30


class InterceptionError(Exception):
    """The connection landed on an address DoH did not return (§13.3).

    Deliberately *not* a :class:`FlexError`: this says "fix the network", a
    different remedy from the portal-side token errors.
    """


class EmptyResponseError(Exception):
    """An HTTP 200 with an empty body — an empty series is an error, never a value.

    Its own branch (§4.1, §13.3), distinct from the 1012/1015/1013 code family:
    an empty body means the network path failed silently, not that the portal
    rejected the token.
    """


@dataclass(frozen=True)
class HttpResponse:
    """One HTTP GET result: the body, and the address actually connected to.

    ``connected_ip`` is what the interception check compares to the DoH answer.
    """

    text: str
    connected_ip: str


class Resolver(Protocol):
    """The DoH seam: a hostname to its A-record addresses (see :mod:`journal.doh`)."""

    def resolve(self, host: str) -> Sequence[str]:
        ...


class Transport(Protocol):
    """The HTTP seam: GET a URL over the DoH-resolved addresses it is given.

    The client hands down the addresses rather than letting the transport
    resolve, so the system resolver is never on the path (§13.3). The response
    reports which of them the socket actually reached.
    """

    def get(self, url: str, addresses: Sequence[str]) -> HttpResponse:
        ...


class FlexClient:
    """Fetch a Flex statement, resolving and verifying each host per run."""

    def __init__(
        self,
        resolver: Resolver,
        transport: Transport,
        *,
        token_name: str = secrets.IBKR_FLEX_TOKEN,
        resolve_secret: Callable[[str], str] = secrets.resolve_secret,
        sleep: Callable[[float], None] = time.sleep,
        warn: Optional[Callable[[str], None]] = None,
        today: Optional[date] = None,
        retries: int = 5,
        backoff: float = 1.0,
    ) -> None:
        self._resolver = resolver
        self._transport = transport
        self._token_name = token_name
        self._resolve_secret = resolve_secret
        self._sleep = sleep
        self._warn = warn
        self._today = today
        self.retries = max(1, retries)
        self.backoff = backoff

    def fetch_statement(self, query_id: str) -> str:
        """Run the two-step flow and return the statement XML text.

        ``SendRequest`` names the ``GetStatement`` host in its response; that
        host is resolved over DoH for the second leg. The returned text is the
        raw statement — parsing into Fills stays in :mod:`journal.flex`.
        """
        self._surface_token_expiry()
        token = self._resolve_secret(self._token_name)

        send_url = _with_params(SEND_REQUEST_URL, t=token, q=query_id, v=FLEX_VERSION)
        ref_code, statement_url = self._request(send_url, _extract_dispatch)

        get_url = _with_params(statement_url, t=token, q=ref_code, v=FLEX_VERSION)
        return self._request(get_url, lambda root, text: text)

    def _request(self, url: str, extract: Callable[[ET.Element, str], object]):
        """GET a verified ``url``, polling while Flex reports a transient code.

        On a non-error body ``extract(root, text)`` produces the result; a
        transient code sleeps and retries; any other Fail is raised as a
        :class:`FlexError`.
        """
        last_code: Optional[str] = None
        for attempt in range(self.retries):
            text = self._get_verified(url)
            root = _parse(text)
            code = _fail_code(root)
            if code is None:
                return extract(root, text)
            if code in RETRYABLE_CODES:
                last_code = code
                if attempt + 1 < self.retries:
                    self._sleep(self.backoff * (2 ** attempt))
                    continue
                raise FlexError(
                    f"Flex transient error {code} did not clear after "
                    f"{self.retries} attempts: {_error_message(root)}"
                )
            raise FlexError(f"Flex error {code}: {_error_message(root)}".strip())
        raise FlexError(f"Flex transient error {last_code} did not clear")

    def _get_verified(self, url: str) -> str:
        """GET ``url`` over its DoH-resolved addresses, refusing anything else.

        DoH runs per host, per call (uncached), and its answer *is* the
        connection's destination — the system resolver never sees the host
        (§13.3). The mismatch check stays as an invariant on the socket's real
        peer: nothing may reach an address DoH did not name. An empty body is
        its own error, never an empty statement.
        """
        host = urllib.parse.urlparse(url).hostname or ""
        doh_addresses = list(self._resolver.resolve(host))
        response = self._transport.get(url, doh_addresses)
        if response.connected_ip not in doh_addresses:
            raise InterceptionError(
                f"{host} resolved over DoH to {doh_addresses}, but the "
                f"connection landed on {response.connected_ip} — the socket "
                f"reached an address DoH did not name; fix the network"
            )
        if not response.text.strip():
            raise EmptyResponseError(
                f"empty response body from {host} — an empty series is an "
                f"error, never a value (fix the network)"
            )
        return response.text

    def _surface_token_expiry(self) -> None:
        """State the token's expiry as a fact once it is near, before it bites."""
        if self._warn is None:
            return
        today = self._today or date.today()
        remaining = secrets.days_until_token_expiry(today)
        if remaining <= TOKEN_EXPIRY_WARN_DAYS:
            self._warn(
                f"IBKR Flex token expires {secrets.IBKR_FLEX_TOKEN_EXPIRES} "
                f"(in {remaining} day(s)) — rotate it in the portal; "
                f"regenerating invalidates the current token"
            )


def build_default_client(
    warn: Optional[Callable[[str], None]] = None,
    today: Optional[date] = None,
) -> "FlexClient":
    """Assemble the client with its one real DoH resolver and HTTP transport.

    The concrete network pieces live behind their seams; importing them here
    (not at module top) keeps the seam definitions free of the socket-facing
    modules, the way the bar pipeline keeps yfinance behind its adapter.
    """
    from .doh import DohResolver
    from .flex_transport import PinnedTransport

    return FlexClient(
        resolver=DohResolver(),
        transport=PinnedTransport(),
        warn=warn,
        today=today,
    )


def _with_params(url: str, **params: str) -> str:
    """Append query parameters to ``url`` (the statement URL carries none)."""
    query = urllib.parse.urlencode(params)
    separator = "&" if urllib.parse.urlparse(url).query else "?"
    return f"{url}{separator}{query}"


def _parse(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise FlexError(f"not well-formed Flex XML: {exc}") from exc


def _fail_code(root: ET.Element) -> Optional[str]:
    """The ``ErrorCode`` when the body is a Flex ``Fail``, else ``None``.

    A successful statement (``FlexQueryResponse``) has no ``Status=Fail``, so it
    returns ``None`` and flows straight through.
    """
    status = root.findtext("Status")
    if status and status.strip().lower() == "fail":
        return (root.findtext("ErrorCode") or "").strip() or "unknown"
    return None


def _error_message(root: ET.Element) -> str:
    return (root.findtext("ErrorMessage") or "").strip()


def _extract_dispatch(root: ET.Element, _text: str) -> Tuple[str, str]:
    """Pull the reference code and the GetStatement URL from a SendRequest reply."""
    ref_code = (root.findtext("ReferenceCode") or "").strip()
    statement_url = (root.findtext("Url") or "").strip()
    if not ref_code or not statement_url:
        raise FlexError(
            "SendRequest succeeded but named no ReferenceCode/Url to retrieve"
        )
    return ref_code, statement_url
