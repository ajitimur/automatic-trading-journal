"""Bind Stop Declarations to new Trades and auto-commit them (ADR 0012, #50).

After intake has landed the Fills, a live **Stop Declaration** whose
``(book, symbol, entry_date)`` matches a clean ``new-trade`` proposal **binds**
to it, and the Trade commits — with the declared Stop and ``recorded``
provenance — through the one confirm door, no ``journal confirm`` (ADR 0012).

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

Binding is **idempotent**: the event that bound is recorded against its Trade,
so a declaration the relay still holds, or one a re-run re-derives, is seen as
already bound and the committed Trade is left alone. The stop's set-at time is
the declaration's Telegram timestamp, so a stop declared at entry dates from
when it was said (ADR 0009, ADR 0012).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import trades
from .declarations import StopDeclarationBindingStore, StopDeclarationStore


@dataclass(frozen=True)
class BindResult:
    bound: int       # declarations that bound to a Trade
    committed: int   # Trades committed by those bindings
    refused: int     # declarations whose stop sat at or above entry

    def summary(self) -> str:
        return (
            f"{self.bound} declaration(s) bound, {self.committed} Trade(s) "
            f"committed, {self.refused} refused"
        )


def bind_and_commit(conn: sqlite3.Connection, *, now: str) -> BindResult:
    """Bind every live declaration that matches a new-trade proposal, and commit.

    ``now`` stamps each binding. Commits (through :func:`trades.confirm`).
    """
    declarations = StopDeclarationStore(conn)
    bindings = StopDeclarationBindingStore(conn)
    live = declarations.live(exclude_relay_ids=bindings.bound_relay_ids())

    new_trades = {
        trades.cohort_key(p): p
        for p in trades.propose(conn)
        if p.kind == "new-trade"
    }

    stops_by_trade: dict[trades.CohortKey, float] = {}
    stop_set_at: dict[trades.CohortKey, str] = {}
    to_bind: dict[trades.CohortKey, str] = {}
    refused = 0
    for key, decl in live.items():
        proposal = new_trades.get(key)
        if proposal is None:
            continue  # no clean new-trade to bind to — the declaration stays live
        if proposal.avg_price and decl.stop >= proposal.avg_price:
            # An inverted R never reaches the store; the declaration stays unbound
            # and a corrected one binds on a later run (ADR 0012).
            refused += 1
            continue
        stops_by_trade[key] = decl.stop
        stop_set_at[key] = decl.telegram_at
        to_bind[key] = decl.relay_id

    if not stops_by_trade:
        return BindResult(bound=0, committed=0, refused=refused)

    result = trades.confirm(
        conn,
        stops_by_trade=stops_by_trade,
        stop_set_at=stop_set_at,
        auto_commit=True,
    )

    ids = {
        (r["book"], r["symbol"], r["entry_date"]): r["id"]
        for r in conn.execute("SELECT id, book, symbol, entry_date FROM trade")
    }
    bound = 0
    for key, relay_id in to_bind.items():
        trade_id = ids.get(key)
        if trade_id is not None and bindings.record(relay_id, trade_id, now):
            bound += 1
    conn.commit()
    return BindResult(bound=bound, committed=result.new_trades, refused=refused)
