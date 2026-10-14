"""Pull and acknowledge relay items over the wire (ADR 0011, #49).

The relay's Mac-facing contract, as pinned by
``docs/samples/relay-pull-fixture.json``:

* ``GET /pull`` returns ``{"items": [...]}``, unacknowledged items in arrival
  order, each ``{id, telegram_at, text, parse}``.
* ``POST /ack`` takes ``{"ids": [...]}``, deletes those items, answers 204, and
  is idempotent.
* ``PUT /known-us-symbols`` takes ``{"symbols": [...]}`` and replaces the list
  the relay reads a 4-letter symbol as US by (#51).
* All three need ``Authorization: Bearer <MAC_SECRET>``.

The relay sits on the open internet, behind the same ISP whose DNS interception
the Flex client was built against, so it takes the same path: each call resolves
the relay host over DoH and connects only to that answer, and the socket's real
peer is checked against it. The resolver, the transport and the secret lookup
are injected, so nothing here needs a socket under test. The real pieces are the
Flex client's :class:`~journal.doh.DohResolver` and
:class:`~journal.flex_transport.PinnedTransport`.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Callable, Mapping, Optional, Protocol, Sequence

from . import secrets
from .flex_client import EmptyResponseError, HttpResponse, Resolver, check_landed

# The relay's base URL (``https://<worker>.workers.dev``). Install-time config,
# not a secret, carried in the launchd plist like the Flex query id. Unset means
# no relay is configured, and the run says it skipped the pass.
URL_ENV = "JOURNAL_RELAY_URL"


class RelayError(Exception):
    """The relay answered with something that is not its pull payload."""


class Transport(Protocol):
    """An HTTPS request to one of the DoH-resolved addresses it is given."""

    def request(
        self,
        method: str,
        url: str,
        addresses: Sequence[str],
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
    ) -> HttpResponse:
        ...


class RelayClient:
    def __init__(
        self,
        base_url: str,
        *,
        resolver: Resolver,
        transport: Transport,
        secret_name: str = secrets.RELAY_MAC_SECRET,
        resolve_secret: Callable[[str], str] = secrets.resolve_secret,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._resolver = resolver
        self._transport = transport
        self._secret_name = secret_name
        self._resolve_secret = resolve_secret

    def pull(self) -> list[dict]:
        """Every item the relay holds, in arrival order.

        An empty body is an error, never an empty relay: an empty relay answers
        ``{"items": []}``.
        """
        response = self._send("GET", "/pull")
        if not response.text.strip():
            raise EmptyResponseError(
                f"empty response body from {self._host()} — an empty relay "
                f"answers with no items, not with nothing (fix the network)"
            )
        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            raise RelayError(f"relay pull is not JSON: {response.text[:200]!r}") from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RelayError(f"relay pull has no items list: {response.text[:200]!r}")
        return items

    def acknowledge(self, ids: Sequence[str]) -> None:
        """Tell the relay these items are safely recorded, so it deletes them."""
        self._send(
            "POST",
            "/ack",
            body=json.dumps({"ids": list(ids)}).encode("utf-8"),
            content_type="application/json",
        )

    def push_known_us_symbols(self, symbols: Sequence[str]) -> None:
        """Replace the relay's list of symbols the journal knows on the US book."""
        self._send(
            "PUT",
            "/known-us-symbols",
            body=json.dumps({"symbols": list(symbols)}).encode("utf-8"),
            content_type="application/json",
        )

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ) -> HttpResponse:
        host = self._host()
        headers = {"Authorization": f"Bearer {self._resolve_secret(self._secret_name)}"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        addresses = list(self._resolver.resolve(host))
        response = self._transport.request(
            method, f"{self._base_url}{path}", addresses, headers=headers, body=body
        )
        check_landed(host, addresses, response)
        return response

    def _host(self) -> str:
        return urllib.parse.urlparse(self._base_url).hostname or ""


def build_default_client() -> Optional[RelayClient]:
    """The real client, or ``None`` when no relay URL is configured."""
    base_url = os.environ.get(URL_ENV, "").strip()
    if not base_url:
        return None

    from .doh import DohResolver
    from .flex_transport import PinnedTransport

    return RelayClient(base_url, resolver=DohResolver(), transport=PinnedTransport())
