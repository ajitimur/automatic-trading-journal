"""Bind Stop Declarations to Trades, auto-commit new ones, lapse the rest (ADR 0012).

After intake has landed the Fills, a live **Stop Declaration** whose
``(book, symbol, entry_date)`` matches a clean ``new-trade`` proposal **binds**
to it, and the Trade commits — with the declared Stop and ``recorded``
provenance — through the one confirm door, no ``journal confirm`` (ADR 0012).
A declared ``no sl`` binds the same way and commits a **Declined Stop**, and a
declared Setup lands with the commit either way (#51).

What binds, and what does not:

- Only ``new-trade`` proposals. Add-fills, restatements, orphan exits,
  quarantines and drift are left in the queue for the human door, and a same-day
  round trip's exit waits with them.
- A declared stop **at or above the entry average** does not bind: it would
  invert every R the Trade produces (:class:`~journal.stops.StopAboveEntry`), so
  the Trade stays uncommitted, the declaration stays unbound, and the refusal is
  counted. A corrected declaration binds on a later run.
- A declaration with no matching proposal stays live and binds nothing — a
  ``new-trade`` the trader never declared is simply held.

A live declaration that meets no ``new-trade`` ends one of three ways (#54):

- **A committed Trade with no Stop** — held from before, or confirmed at the
  laptop without a stop answer — takes the declared Stop through the ordinary
  stop path, dated by the declaration, so provenance derives from when it was
  said. A ``no sl`` declines it.
- **A committed Trade that has a Stop, or is frozen,** is left alone and the
  declaration is refused: the Stop is immutable, and a frozen Trade's
  hand-entered fields no longer change.
  A refused declaration stays live, so it is re-derived each run and never
  lapses; the bot answers it once.
- **Nothing at all** for ``LAPSE_TRADING_DAYS`` trading days after its entry
  date: it **lapses**, is recorded as lapsed, and is never live again, so Fills
  for its key that turn up later do not bind it.

Binding is **idempotent**: the event that bound is recorded against its Trade,
so a declaration the relay still holds, or one a re-run re-derives, is seen as
already bound and the committed Trade is left alone. The stop's set-at time is
the declaration's Telegram timestamp, so a stop declared at entry dates from
when it was said (ADR 0009, ADR 0012).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import stops, trades
from .declarations import (
    DECLINED,
    StopDeclarationBindingStore,
    StopDeclarationEvent,
    StopDeclarationLapseStore,
    StopDeclarationStore,
)


ABOVE_ENTRY = "above-entry"  # the stop would invert R
STOP_HELD = "stop-held"      # the Trade already has a Stop, which never changes
FROZEN = "frozen"            # the Trade is frozen, so its stop and setup are locked


@dataclass(frozen=True)
class Refusal:
    """A live declaration that did not bind, and why (``reason``).

    It stays live, so every run re-derives it; the message goes out once.
    """

    relay_id: str
    book: str
    symbol: str
    entry_date: str
    stop: Optional[float]       # None for a refused ``no sl``
    entry_avg: Optional[float] = None
    reason: str = ABOVE_ENTRY
    held_stop: Optional[float] = None  # the Trade's Stop, when it has one


@dataclass(frozen=True)
class BindResult:
    bound: int       # declarations that bound to a Trade
    committed: int   # Trades committed by those bindings
    refusals: tuple[Refusal, ...] = ()  # declarations that did not bind, and why
    lapsed: int = 0  # declarations that lapsed unmatched this run

    @property
    def refused(self) -> int:
        return len(self.refusals)

    def summary(self) -> str:
        return (
            f"{self.bound} declaration(s) bound, {self.committed} Trade(s) "
            f"committed, {self.refused} refused, {self.lapsed} lapsed"
        )


def bind_and_commit(conn: sqlite3.Connection, *, as_of: str, now: str) -> BindResult:
    """Bind every live declaration that matches a new-trade proposal, and commit.

    A live declaration that matches nothing and is ``LAPSE_TRADING_DAYS`` past
    its entry date on ``as_of`` lapses; one matching a committed Trade sets its
    Stop or is refused (:func:`_onto_committed`). ``now`` stamps each binding
    and lapse. Commits.
    """
    declarations = StopDeclarationStore(conn)
    bindings = StopDeclarationBindingStore(conn)
    lapses = StopDeclarationLapseStore(conn)
    live = declarations.live(
        exclude_relay_ids=bindings.bound_relay_ids() | lapses.lapsed_relay_ids()
    )

    committed = {
        (r["book"], r["symbol"], r["entry_date"]): r
        for r in conn.execute(
            "SELECT id, book, symbol, entry_date, entry_avg_price, stop, setup, "
            "frozen FROM trade"
        )
    }
    new_trades = {
        trades.cohort_key(p): p
        for p in trades.propose(conn)
        if p.kind == "new-trade"
    }

    stops_by_trade: dict[trades.CohortKey, float] = {}
    stop_set_at: dict[trades.CohortKey, str] = {}
    declined: set[trades.CohortKey] = set()
    setups: dict[trades.CohortKey, str] = {}
    to_bind: dict[trades.CohortKey, str] = {}
    refusals: list[Refusal] = []
    lapsed = 0
    bound = 0
    for key, decl in live.items():
        trade = committed.get(key)
        if trade is not None:
            refusal = _onto_committed(conn, decl, trade, bindings, now)
            if refusal is not None:
                refusals.append(refusal)
            else:
                bound += 1
            continue
        proposal = new_trades.get(key)
        if proposal is None:
            if _lapses(conn, decl, as_of) and lapses.record(decl.relay_id, as_of, now):
                lapsed += 1
            continue  # otherwise the declaration stays live, waiting for its Fills
        if decl.kind == DECLINED:
            declined.add(key)
        elif proposal.avg_price and decl.stop >= proposal.avg_price:
            # An inverted R never reaches the store; the declaration stays unbound
            # and a corrected one binds on a later run (ADR 0012).
            refusals.append(Refusal(
                relay_id=decl.relay_id, book=decl.book, symbol=decl.symbol,
                entry_date=decl.entry_date, stop=decl.stop,
                entry_avg=proposal.avg_price,
            ))
            continue
        else:
            stops_by_trade[key] = decl.stop
            stop_set_at[key] = decl.telegram_at
        if decl.setup is not None:
            setups[key] = decl.setup
        to_bind[key] = decl.relay_id

    if not to_bind:
        conn.commit()
        return BindResult(
            bound=bound, committed=0, refusals=tuple(refusals), lapsed=lapsed
        )

    result = trades.confirm(
        conn,
        stops_by_trade=stops_by_trade,
        stop_set_at=stop_set_at,
        declined=declined,
        setups_by_trade=setups,
        auto_commit=True,
    )

    ids = {
        (r["book"], r["symbol"], r["entry_date"]): r["id"]
        for r in conn.execute("SELECT id, book, symbol, entry_date FROM trade")
    }
    for key, relay_id in to_bind.items():
        trade_id = ids.get(key)
        if trade_id is not None and bindings.record(relay_id, trade_id, now):
            bound += 1
    conn.commit()
    return BindResult(
        bound=bound, committed=result.new_trades, refusals=tuple(refusals),
        lapsed=lapsed,
    )


def _onto_committed(
    conn: sqlite3.Connection,
    decl: StopDeclarationEvent,
    trade: sqlite3.Row,
    bindings: StopDeclarationBindingStore,
    now: str,
) -> Optional[Refusal]:
    """Apply ``decl`` to a Trade already committed; ``None`` once it has bound.

    Only an unfrozen Trade with no Stop takes one, through the ordinary stop
    path, dated by when the declaration was said, so provenance derives from
    that moment (ADR 0009). A Trade that has a Stop keeps it: the Stop is
    immutable (CONTEXT.md). A declared Setup fills a Setup hole and never
    overwrites one. A refusal changes nothing on the Trade.

    The binding is recorded *before* the stop helpers write, because each of
    them commits: the Stop and the record of which declaration set it land in
    one transaction, so a run that dies part-way never leaves a Stop that a
    later run would mistake for one already held.
    """
    def refuse(reason: str) -> Refusal:
        return Refusal(
            relay_id=decl.relay_id, book=decl.book, symbol=decl.symbol,
            entry_date=decl.entry_date, stop=decl.stop,
            entry_avg=trade["entry_avg_price"], reason=reason,
            held_stop=trade["stop"],
        )

    if trade["frozen"]:
        return refuse(FROZEN)
    if trade["stop"] is not None:
        return refuse(STOP_HELD)
    entry = trade["entry_avg_price"]
    if decl.kind != DECLINED and entry and decl.stop >= entry:
        return refuse(ABOVE_ENTRY)
    bindings.record(decl.relay_id, trade["id"], now)
    if decl.kind == DECLINED:
        stops.decline_stop(conn, trade["id"])
    else:
        stops.set_stop(conn, trade["id"], decl.stop, as_of=decl.telegram_at)
    if decl.setup is not None and trade["setup"] is None:
        stops.set_setup(conn, trade["id"], decl.setup)
    return None


# A declaration waits this many trading days after its entry date for a Trade
# before it lapses (#54) — the same three days as the grace window (ADR 0009),
# since a stop bound any later would read reconstructed anyway.
LAPSE_TRADING_DAYS = stops.GRACE_TRADING_DAYS


def _lapses(conn: sqlite3.Connection, decl: StopDeclarationEvent, as_of: str) -> bool:
    """Whether ``decl`` has waited out its window, unmatched, by ``as_of``.

    Counted in trading days off the symbol's bars, else the Book's benchmark,
    as the grace window is counted for a Trade not yet committed. Where neither
    is cached it is counted in calendar days (#54): unlike provenance, which
    keeps the strict rule when the window cannot be counted (ADR 0009), a
    declaration has to end somehow — a mistyped symbol has no bars and never
    will.
    """
    elapsed = stops.trading_days_after(
        conn, decl.book, decl.symbol, decl.entry_date, as_of
    )
    if elapsed is None:
        elapsed = (date.fromisoformat(as_of) - date.fromisoformat(decl.entry_date)).days
    return elapsed >= LAPSE_TRADING_DAYS
