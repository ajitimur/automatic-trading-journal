"""Parse an IBKR Flex XML file into Fills (SPEC §4.1, ADR 0003, issue #22).

No network here: the file is already on disk. Three facts drive the parsing,
two of which *correct* IBKR's own documentation — get them wrong and the error
is silent:

- **Commission is on every fill, pro rata** — not the first fill only. Each
  ``Trade`` row already carries its own ``ibCommission``; summing them across
  an ``ibOrderID`` reconciles to the broker's order total. Taking only the
  first would undercount multi-fill orders by ~60%
  (``docs/samples/ibkr-flex-findings.md``).
- **Timestamps are US Eastern.** No timezone attribute exists in the file; the
  session floor at exactly ``093000`` settles it as ``America/New_York``. We
  store an ISO 8601 string *with* the derived offset so the instant is
  unambiguous, and derive it per date so a year-long backfill crossing DST
  stays correct.
- **Exec ids are ``<base>.<seq>``.** The base is the logical execution and
  becomes ``source_ref``; the trailing ``seq`` is the ``revision``. A broker
  restatement arrives as the same base with a higher seq — a new revision,
  never an edit (ADR 0003).

Cost attaches at *fill* level because the broker provides it there (SPEC §7.0);
we keep the per-fill commission rather than flattening to an order total.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# IBKR is the US book (SPEC §4.1); every fill this parser emits belongs to it.
SOURCE = "ibkr"
BOOK = "US"

# Trade timestamps carry no offset; the distribution settles them as exchange
# local time (docs/samples/ibkr-flex-findings.md, open item 1).
_EXCHANGE_TZ = ZoneInfo("America/New_York")


class FlexError(Exception):
    """A Flex document that is an error body, not a statement.

    Flex failures return HTTP 200 with an XML error body (SPEC §4.1), so a
    parser that trusts the file blindly would treat a failure as zero trades.
    """


@dataclass(frozen=True)
class Fill:
    """One execution row, the append-only unit of the ledger (ADR 0003)."""

    source: str
    source_ref: str
    revision: int
    book: str
    symbol: str
    side: str          # 'BUY' | 'SELL'
    quantity: float    # signed, as the broker reports it
    price: float
    commission: float  # signed, per-fill pro-rata share (SPEC §7.0)
    executed_at: str   # ISO 8601 US Eastern, offset included
    order_id: str      # ibOrderID — the key commission reconciles across


def parse_exec_id(exec_id: str) -> tuple[str, int]:
    """Split ``<base>.<seq>`` into (logical execution, version).

    The base is everything up to the last dot; the seq is the digits after it.
    Highest seq wins when deriving Trades later, but the ledger keeps them all.
    """
    base, _, seq = exec_id.rpartition(".")
    if not base or not seq:
        raise FlexError(f"unrecognised ibExecID {exec_id!r}")
    return base, int(seq)


def parse_timestamp(value: str) -> str:
    """Parse ``YYYYMMDD;HHMMSS`` as US Eastern and return ISO 8601 with offset.

    The offset is derived from the date, so April (EDT, -04:00) and January
    (EST, -05:00) come out correctly without a stored fixed offset.
    """
    naive = datetime.strptime(value, "%Y%m%d;%H%M%S")
    return naive.replace(tzinfo=_EXCHANGE_TZ).isoformat()


def _fill_from_trade(trade: ET.Element) -> Fill:
    get = trade.get
    base, seq = parse_exec_id(get("ibExecID") or "")
    return Fill(
        source=SOURCE,
        source_ref=base,
        revision=seq,
        book=BOOK,
        symbol=get("symbol") or "",
        side=get("buySell") or "",
        quantity=float(get("quantity") or 0),
        price=float(get("tradePrice") or 0),
        commission=float(get("ibCommission") or 0),
        executed_at=parse_timestamp(get("dateTime") or ""),
        order_id=get("ibOrderID") or "",
    )


def parse_flex(xml_text: str) -> list[Fill]:
    """Parse a Flex XML string into Fills, one per execution row.

    Only ``levelOfDetail="EXECUTION"`` rows become Fills: the field exists
    precisely because a query *can* mix levels, and non-execution rows would
    double-count (docs/samples/ibkr-flex-findings.md).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FlexError(f"not well-formed Flex XML: {exc}") from exc

    _reject_error_body(root)

    fills = [
        _fill_from_trade(trade)
        for trade in root.iter("Trade")
        if (trade.get("levelOfDetail") or "").upper() == "EXECUTION"
    ]
    return fills


def _reject_error_body(root: ET.Element) -> None:
    """Raise if the document is a Flex error rather than a statement.

    Flex signals failure with a ``Status`` of ``Fail`` and an ``ErrorCode``
    (e.g. 1012, token expired) inside an HTTP 200 body. Treat that as an error,
    never as an empty statement (SPEC §4.1).
    """
    status = root.findtext("Status")
    if status and status.strip().lower() == "fail":
        code = (root.findtext("ErrorCode") or "").strip()
        message = (root.findtext("ErrorMessage") or "").strip()
        raise FlexError(f"Flex error {code}: {message}".strip())
