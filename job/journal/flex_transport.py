"""The one real HTTP transport for the Flex client (SPEC §13.3, issue #25).

:class:`FlexClient` depends on the :class:`~journal.flex_client.Transport` seam,
never on this module — the way the bar pipeline depends on ``BarFetcher`` and
not on yfinance. This is the single concrete implementation; no second transport
is written speculatively.

The interception check needs the address the connection *actually* lands on, so
the transport reports it. Resolution goes through the system path (the layer an
ISP intercepts): a redirected DNS answer sends the socket to the block address,
whose IP will not be in the DoH answer the client checks against — which is
exactly how the mismatch is caught, without ever matching a known bad address.

The socket work is isolated in :meth:`get`, the way the yfinance download is
isolated behind its adapter, so nothing above it needs the network to import.
"""

from __future__ import annotations

import socket
import urllib.parse
import urllib.request

from .flex_client import HttpResponse


class SystemTransport:
    """GET a URL and report the system-resolved address the socket reached."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def get(self, url: str) -> HttpResponse:
        """Fetch ``url`` over HTTPS, returning its body and the connected address.

        The address is the system resolver's answer for the host — the value the
        client compares against the DoH answer to detect interception.
        """
        host = urllib.parse.urlparse(url).hostname or ""
        connected_ip = _system_address(host)
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            body = response.read().decode("utf-8")
        return HttpResponse(text=body, connected_ip=connected_ip)


def _system_address(host: str) -> str:
    """The first IPv4 address the system resolver returns for ``host``.

    This is the potentially-intercepted path; the client rejects it unless it
    matches the DoH answer.
    """
    infos = socket.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    return infos[0][4][0]
