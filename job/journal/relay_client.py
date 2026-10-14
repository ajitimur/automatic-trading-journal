"""Pull and acknowledge relay items, and message the trader, over the wire (ADR 0011).

The relay's Mac-facing contract, as pinned by
``docs/samples/relay-pull-fixture.json``:

* ``GET /pull`` returns ``{"items": [...]}``, unacknowledged items in arrival
  order, each ``{id, telegram_at, text, parse}``.
* ``POST /ack`` takes ``{"ids": [...]}``, deletes those items, answers 204, and
  is idempotent.
* ``GET /documents/<id>`` returns ``{"id", "content_base64"}``, the bytes of a
  forwarded TC PDF the relay still holds (#52).
* ``PUT /known-us-symbols`` takes ``{"symbols": [...]}`` and replaces the list
  the relay reads a 4-letter symbol as US by (#51).
* ``POST /send`` takes ``{"text": ...}``, messages the trader, and answers
  ``{"message_id": n}`` (#53). A message about one Trade adds ``"trade":
  {book, symbol, entry_date}``, which the relay remembers against the message
  id so a bare reply to it declares for that Trade; ``"offer": {stop,
  entry_date}`` adds a one-tap button declaring that Stop (#55).
* All five need ``Authorization: Bearer <MAC_SECRET>``.

The relay sits on the open internet, behind the same ISP whose DNS interception
the Flex client was built against, so it takes the same path: each call resolves
the relay host over DoH and connects only to that answer, and the socket's real
peer is checked against it. The resolver, the transport and the secret lookup
are injected, so nothing here needs a socket under test. The real pieces are the
Flex client's :class:`~journal.doh.DohResolver` and
:class:`~journal.flex_transport.PinnedTransport`.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Protocol, Sequence, Tuple

from . import secrets
from .flex_client import EmptyResponseError, HttpResponse, Resolver, check_landed

# The relay's base URL (``https://<worker>.workers.dev``). Install-time config,
# not a secret, carried in the launchd plist like the Flex query id. Unset means
# no relay is configured, and the run says it skipped the pass.
URL_ENV = "JOURNAL_RELAY_URL"


@dataclass(frozen=True)
class Offer:
    """A Stop a nag offers as its one-tap answer: the earlier Trade's, entered then."""

    stop: float
    entry_date: str


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
        payload = self._json(
            response, "pull", "an empty relay answers with no items, not with nothing"
        )
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

    def document(self, item_id: str) -> bytes:
        """The bytes of a forwarded document the relay holds as item ``item_id``.

        The relay fetches them from Telegram on demand and sends them base64 in
        JSON, so the transport's text body carries a PDF unchanged.
        """
        response = self._send("GET", f"/documents/{urllib.parse.quote(item_id, safe='')}")
        payload = self._json(
            response, f"document {item_id}", "a held document answers with its bytes, not with nothing"
        )
        try:
            return base64.b64decode(payload["content_base64"], validate=True)  # type: ignore[index]
        except (ValueError, KeyError, TypeError) as exc:
            raise RelayError(
                f"relay document {item_id} is not one: {response.text[:200]!r}"
            ) from exc

    def send(
        self,
        text: str,
        *,
        trade: Optional[Tuple[str, str, str]] = None,
        offer: Optional[Offer] = None,
    ) -> int:
        """Message the trader through the relay and return Telegram's message id.

        The relay holds the bot token, so the Mac never does (#53). ``trade``, a
        ``(book, symbol, entry_date)``, makes the message one a bare reply can
        answer; ``offer`` adds the one-tap button (#55). An empty body is the
        ISP's interception, not a delivery, so it raises like a pull does.
        """
        payload: dict = {"text": text}
        if trade is not None:
            book, symbol, entry_date = trade
            payload["trade"] = {"book": book, "symbol": symbol, "entry_date": entry_date}
        if offer is not None:
            payload["offer"] = {"stop": offer.stop, "entry_date": offer.entry_date}
        response = self._send(
            "POST",
            "/send",
            body=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
        payload = self._json(
            response, "send", "a sent message answers with its id, not with nothing"
        )
        message_id = payload.get("message_id") if isinstance(payload, dict) else None
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise RelayError(f"relay send has no message id: {response.text[:200]!r}")
        return message_id

    def _json(self, response: HttpResponse, call: str, empty_means: str) -> object:
        """The parsed body. An empty one is the ISP's interception, never an answer."""
        if not response.text.strip():
            raise EmptyResponseError(
                f"empty response body from {self._host()} — {empty_means} "
                f"(fix the network)"
            )
        try:
            return json.loads(response.text)
        except ValueError as exc:
            raise RelayError(f"relay {call} is not JSON: {response.text[:200]!r}") from exc

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
