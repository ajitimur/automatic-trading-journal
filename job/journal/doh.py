"""DNS resolution over DoH, in-process, per host, per run, never cached (§13.3).

The Flex path does not trust the system resolver or a VPN: those are
human-maintained preconditions whose failure is silent and off-app, and the
failure mode — a redirected or empty answer — is exactly what the confirm queue
exists to surface. Resolving in-process makes the dependency explicit,
version-controlled and testable, and it survives the machine moving networks.

:class:`DohResolver` is the seam. The JSON GET is injected (``get_json``) so the
logic — parse A records, treat an empty answer as failure — is tested without a
socket; :func:`urllib_get_json` is the one real, network-touching implementation
(RFC 8484 JSON, ``application/dns-json``).

Two invariants live here:

* **Only A records.** The interception check compares IPv4 to IPv4; a CNAME or
  AAAA row is not an address the socket lands on.
* **Every ``resolve`` is a fresh lookup.** Nothing is memoised — the Akamai edge
  rotated between two lookups minutes apart, so a cached answer is wrong, not
  merely stale (§13.3).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional

# Cloudflare's DoH JSON endpoint. A resolver, not a destination — the addresses
# it returns are what the Flex hosts are checked against.
DEFAULT_ENDPOINT = "https://cloudflare-dns.com/dns-query"

# RFC 1035 record type for an A (IPv4) record, as it appears in the JSON answer.
_A_RECORD = 1


class DohError(Exception):
    """DoH did not return a usable address for the host.

    An empty or answer-less response is a failure, never "no address, skip":
    without a DoH answer there is nothing to check interception against.
    """


class DohResolver:
    """Resolve a hostname to its A-record addresses over DoH.

    ``get_json`` is any callable that performs the DoH GET and returns the
    decoded JSON body; the default real implementation is :func:`urllib_get_json`.
    """

    def __init__(
        self,
        get_json: Optional[Callable[[str], Dict]] = None,
        endpoint: str = DEFAULT_ENDPOINT,
    ) -> None:
        self._get_json = get_json if get_json is not None else urllib_get_json
        self._endpoint = endpoint

    def resolve(self, host: str) -> List[str]:
        """Return the host's A-record addresses, freshly looked up every call."""
        query = urllib.parse.urlencode({"name": host, "type": "A"})
        answer = self._get_json(f"{self._endpoint}?{query}")
        records = answer.get("Answer") if isinstance(answer, dict) else None
        addresses = [
            r["data"]
            for r in (records or [])
            if r.get("type") == _A_RECORD and r.get("data")
        ]
        if not addresses:
            raise DohError(
                f"DoH returned no A record for {host!r} — cannot verify the "
                f"connection (Status {answer.get('Status') if isinstance(answer, dict) else '?'})"
            )
        return addresses


def urllib_get_json(url: str) -> Dict:
    """The one real DoH GET: fetch ``url`` and decode its JSON body (RFC 8484).

    Isolated so :class:`DohResolver`'s parsing is testable without a socket, the
    way the yfinance download is isolated behind its adapter seam.
    """
    request = urllib.request.Request(url, headers={"accept": "application/dns-json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))
