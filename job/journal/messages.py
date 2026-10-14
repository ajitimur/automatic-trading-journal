"""What the journal says back to the trader through the relay (#53, ADR 0012).

The relay holds the bot token, so the Mac messages the trader by asking the
relay to send (:meth:`journal.relay_client.RelayClient.send`), over the same DoH
path and secret as a pull. Two messages so far, both sent by the daily run after
binding:

- **Refusal.** A declaration whose stop sits at or above the entry average does
  not bind (ADR 0012). The bot names both prices and asks for a resend. Sent
  once per declaration, however many runs see it still live.
- **Undeclared Trade nag.** A ``new-trade`` still in the queue after binding has
  no bound declaration, and is held until it gets one (ADR 0012). The bot states
  its book, symbol, quantity, entry date and the trading days left in the grace
  window (ADR 0009). Sent at most once per Trade per run day, so a second run the
  same day stays quiet and a catch-up run after a long sleep sends one message
  per Trade, not one per day missed. A declined or frozen Trade is a committed
  Trade, never a ``new-trade``, so it is never nagged.

A Trade gets at most one message per run: a refusal sent this run stands in for
its nag.

Each message names its Trade to the relay, which remembers it against the
Telegram message id, so the trader can answer by replying with just ``4120`` or
``no sl`` (#55). An entry filled across two days is two Trades (ADR 0001), and a
declaration binds only to the first; so the second day's nag offers the first
day's Stop as a one-tap answer, when that Trade has one and was entered at most
:data:`SPLIT_DAY_TRADING_DAYS` trading days before.

Each message is recorded only once the relay has taken it, with the Telegram
message id the relay hands back. A send that fails leaves no row, so the next run
sends it again; the failure is reported to the caller, which puts it on the run
row. The banner nags (:mod:`journal.nags`) are unchanged; these are additional.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional, Protocol

from . import stops, trades
from .binding import Refusal
from .relay_client import Offer


UNDECLARED = "undeclared"
REFUSAL = "refusal"

# How far back a split-day nag looks for the earlier day's Stop. A Trade entered
# longer ago than this is a separate entry, not the first half of a split fill.
SPLIT_DAY_TRADING_DAYS = 3


class Sender(Protocol):
    """The outbound half of the relay seam: returns the Telegram message id."""

    def send(
        self,
        text: str,
        *,
        trade: Optional[trades.CohortKey] = None,
        offer: Optional[Offer] = None,
    ) -> int:
        ...


@dataclass(frozen=True)
class Message:
    kind: str  # UNDECLARED | REFUSAL
    book: str
    symbol: str
    entry_date: str
    text: str
    relay_id: Optional[str] = None
    offer: Optional[Offer] = None

    @property
    def key(self) -> trades.CohortKey:
        return (self.book, self.symbol, self.entry_date)


@dataclass(frozen=True)
class SendResult:
    sent: int
    left: int                 # due but not sent, because a send failed
    error: Optional[str] = None

    def summary(self) -> str:
        detail = f"{self.sent} message(s) sent"
        if self.error is None:
            return detail
        return f"{detail}; send failed, {self.left} left for the next run: {self.error}"


def send_due(
    conn: sqlite3.Connection,
    sender: Sender,
    *,
    refusals: Iterable[Refusal],
    as_of: str,
    run_id: int,
    now: str,
) -> SendResult:
    """Send every message due on ``as_of``, recording each one the relay took.

    Stops at the first failed send: an unreachable relay fails them all, and
    whatever was not sent is still due on the next run. Commits.
    """
    due = due_messages(conn, refusals=refusals, as_of=as_of)
    sent = 0
    for i, message in enumerate(due):
        try:
            message_id = sender.send(message.text, trade=message.key, offer=message.offer)
        except Exception as exc:  # the network path lies (§13.3) — report, never raise
            conn.commit()
            return SendResult(sent=sent, left=len(due) - i, error=str(exc) or repr(exc))
        _record(conn, message, message_id, as_of=as_of, run_id=run_id, now=now)
        sent += 1
    conn.commit()
    return SendResult(sent=sent, left=0)


def due_messages(
    conn: sqlite3.Connection, *, refusals: Iterable[Refusal], as_of: str
) -> list[Message]:
    """Refusals not yet sent, then nags for new Trades not yet nagged ``as_of``."""
    out = [_refusal(r) for r in refusals if not _refusal_sent(conn, r.relay_id)]
    refused_now = {m.key for m in out}
    for p in trades.propose(conn):
        if p.kind != "new-trade":
            continue
        key = trades.cohort_key(p)
        if key in refused_now or _nagged_on(conn, key, as_of):
            continue
        out.append(_nag(conn, p, as_of))
    return out


def _refusal(r: Refusal) -> Message:
    text = (
        f"✗ {r.book} {r.symbol} SL {_number(r.stop)} is at or above the entry "
        f"average {_number(r.entry_avg)}, entry {r.entry_date}, so it was not "
        f"bound. Resend a stop below {_number(r.entry_avg)}."
    )
    return Message(REFUSAL, r.book, r.symbol, r.entry_date, text, relay_id=r.relay_id)


def _nag(conn: sqlite3.Connection, p: trades.Proposal, as_of: str) -> Message:
    entry_date = p.entry_date or ""
    left = stops.grace_days_left(conn, p.book, p.symbol, entry_date, as_of)
    price = f" at {_number(p.avg_price)}" if p.avg_price else ""
    offer = _split_day_offer(conn, p.book, p.symbol, entry_date)
    question = (
        f"Same as {p.symbol} {_day_month(offer.entry_date)} ({_number(offer.stop)})?"
        if offer
        else "Reply with the stop, e.g. 4120, or no sl"
    )
    text = (
        f"⏳ {p.book} {p.symbol} is waiting for a stop\n"
        f"{_number(p.quantity)} shares{price}, entry {entry_date}\n"
        f"{_grace(left)}\n"
        f"{question}"
    )
    return Message(UNDECLARED, p.book, p.symbol, entry_date, text, offer=offer)


def _split_day_offer(
    conn: sqlite3.Connection, book: str, symbol: str, entry_date: str
) -> Optional[Offer]:
    """The Stop of this symbol's latest earlier Trade on the Book, if recent enough.

    Counted in trading days like the grace window, falling back to calendar
    days when no bars are cached (ADR 0009's fallback).
    """
    row = conn.execute(
        "SELECT entry_date, stop FROM trade WHERE book = ? AND symbol = ? "
        "AND entry_date < ? AND stop IS NOT NULL ORDER BY entry_date DESC LIMIT 1",
        (book, symbol, entry_date),
    ).fetchone()
    if row is None:
        return None
    elapsed = stops.trading_days_after(conn, book, symbol, row["entry_date"], entry_date)
    if elapsed is None:
        elapsed = (date.fromisoformat(entry_date) - date.fromisoformat(row["entry_date"])).days
    if elapsed > SPLIT_DAY_TRADING_DAYS:
        return None
    return Offer(stop=row["stop"], entry_date=row["entry_date"])


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day_month(iso: str) -> str:
    """``13 Oct``, as the relay echoes an entry date, whatever the locale."""
    d = date.fromisoformat(iso)
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _grace(left: Optional[int]) -> str:
    if left is None:
        return "The grace window cannot be counted yet (no bars cached)"
    if left == 0:
        return "The grace window has closed: a stop set now reads reconstructed"
    return f"{left} trading day{'' if left == 1 else 's'} left in the grace window"


def _number(x: float) -> str:
    """``4,120`` or ``95.5``: thousands grouped, no trailing zeros."""
    return f"{x:,.6f}".rstrip("0").rstrip(".")


def _refusal_sent(conn: sqlite3.Connection, relay_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM outbound_message WHERE kind = ? AND relay_id = ?",
        (REFUSAL, relay_id),
    ).fetchone() is not None


def _nagged_on(conn: sqlite3.Connection, key: trades.CohortKey, as_of: str) -> bool:
    book, symbol, entry_date = key
    return conn.execute(
        "SELECT 1 FROM outbound_message WHERE kind = ? "
        "AND book = ? AND symbol = ? AND entry_date = ? AND sent_on = ?",
        (UNDECLARED, book, symbol, entry_date, as_of),
    ).fetchone() is not None


def _record(
    conn: sqlite3.Connection,
    message: Message,
    message_id: int,
    *,
    as_of: str,
    run_id: int,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO outbound_message (run_id, kind, book, symbol, entry_date, "
        "relay_id, sent_on, sent_at, text, telegram_message_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id, message.kind, message.book, message.symbol, message.entry_date,
            message.relay_id, as_of, now, message.text, message_id,
        ),
    )
