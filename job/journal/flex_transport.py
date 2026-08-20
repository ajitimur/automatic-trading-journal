"""The one real HTTP transport for the Flex client (SPEC §13.3, issue #25).

:class:`FlexClient` depends on the :class:`~journal.flex_client.Transport` seam,
never on this module — the way the bar pipeline depends on ``BarFetcher`` and
not on yfinance. This is the single concrete implementation; no second transport
is written speculatively.

**The socket connects to the address DoH returned.** §13.3 says resolution is
"not a VPN and not the system resolver"; an earlier transport resolved over DoH
but then *connected* through the system path and compared the two afterwards,
which detected interception without surviving it. On an intercepted network the
TLS handshake failed against the block server's certificate before the client
could state anything, so the operator got a stack trace where the SPEC asks for
a stated fact. Pinning the connection to the DoH answer removes the system
resolver from the path entirely.

The mismatch check upstream is kept, now as an invariant rather than the primary
defence: the address reported back is the socket's real peer, so a transport that
ever lands somewhere DoH did not name still raises. The certificate is verified
against the *hostname* — SNI and ``Host`` both carry it — so an interceptor that
answers on a DoH-named address is still caught by TLS, and that failure is
surfaced as :class:`InterceptionError`, not as a traceback.

The socket work is isolated in :meth:`get`, the way the yfinance download is
isolated behind its adapter, so nothing above it needs the network to import.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import urllib.parse
from typing import Sequence

from .flex_client import HttpResponse, InterceptionError

# Akamai answers 403 to a request with no User-Agent, so one is always sent.
# Named after the job rather than a browser: the fetch is unattended and should
# say so, and a spoofed browser string would be a lie the logs have to carry.
USER_AGENT = "automatic-trading-journal/0.0.1 (+flex-web-service)"

# How much of an unexpected error body to quote back. Enough to read the cause,
# bounded so a block page's HTML cannot flood the run record (§13.3: error
# bodies are surfaced, never swallowed).
_BODY_EXCERPT = 400


class TransportError(Exception):
    """The HTTP GET did not produce a body to hand upward.

    Distinct from :class:`InterceptionError` ("fix the network") and from the
    Flex error codes ("go to the portal"): this is the connection itself
    failing, or the server answering with something that is not a statement.
    """


class PinnedTransport:
    """GET a URL over HTTPS, connecting only to the addresses DoH named."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._context = ssl.create_default_context()

    def get(self, url: str, addresses: Sequence[str]) -> HttpResponse:
        """Fetch ``url``, connecting to one of ``addresses``.

        Each address is tried in turn — Akamai names several edges and one may
        be unroutable — and the first that answers wins. A certificate failure
        is *not* retried against the next address: a valid DoH address serving
        the wrong certificate means the path is intercepted, and trying again
        elsewhere would only bury that fact.
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or 443
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"

        if not addresses:
            raise TransportError(
                f"no DoH address to connect to for {host} — refusing to fall "
                f"back to the system resolver (§13.3)"
            )

        last_error: Exception | None = None
        for address in addresses:
            try:
                return self._get_from(host, port, target, address)
            except ssl.SSLCertVerificationError as exc:
                raise InterceptionError(
                    f"{host} resolved over DoH to {list(addresses)}, but the "
                    f"certificate served at {address} is not valid for {host} "
                    f"({getattr(exc, 'verify_message', None) or exc}) — the "
                    f"connection is being "
                    f"intercepted; fix the network"
                ) from exc
            except OSError as exc:
                last_error = exc
        raise TransportError(
            f"could not reach {host} at any DoH address {list(addresses)}: "
            f"{last_error}"
        )

    def _get_from(
        self, host: str, port: int, target: str, address: str
    ) -> HttpResponse:
        """One GET against one pinned address, reporting the socket's real peer."""
        conn = _PinnedHTTPSConnection(
            host, address, port=port, timeout=self._timeout, context=self._context
        )
        try:
            conn.request(
                "GET", target, headers={"Host": host, "User-Agent": USER_AGENT}
            )
            response = conn.getresponse()
            body = response.read().decode("utf-8")
            if response.status != 200:
                raise TransportError(
                    f"{host} answered HTTP {response.status} {response.reason} "
                    f"from {conn.peer_address}: {body[:_BODY_EXCERPT].strip()}"
                )
            return HttpResponse(text=body, connected_ip=conn.peer_address)
        finally:
            conn.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection whose socket goes to ``address``, not to DNS.

    ``self.host`` stays the hostname throughout, so SNI, the ``Host`` header and
    certificate verification all see the name — only the TCP destination is
    pinned. ``peer_address`` is read back off the connected socket rather than
    assumed, so the address reported upward is the one actually reached.
    """

    def __init__(self, host: str, address: str, **kwargs) -> None:
        super().__init__(host, **kwargs)
        self._address = address
        self.peer_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port), self.timeout, self.source_address
        )
        self.peer_address = self.sock.getpeername()[0]
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
