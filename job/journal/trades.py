"""Derive Trades from Fills through a confirm step (SPEC §3.1/§3.4/§5.1, #23).

A **Trade is an entry-day cohort** (ADR 0001): all entry (BUY) fills for one
symbol on one book on one calendar date, plus the exits allocated to it.
Entering the same symbol on a *later* day is a **second Trade, never an
addition** — the counter-intuitive case the proposal states out loud.

Nothing here writes a Trade on its own. :func:`propose` reads the Fill ledger and
returns *proposals* — ``new-trade`` and ``exit-allocation`` — and commits
nothing (the one-door rule, SPEC §5.1). :func:`confirm` is the only thing that
commits: it derives the same proposals and lands them, defaulting exits to FIFO
across open Trades and honouring a bounded override.

Trades are **recomputed from Fills, never matched**: the entry side is a pure
grouping of buys, and the only human decision that is not derivable is an exit
override — which is exactly why it is the one thing confirm lets you change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Allocation:
    """One slice of a sell Fill assigned to a Trade's open quantity."""

    book: str
    symbol: str
    entry_date: str        # the cohort key — resolves to a trade id at commit
    quantity: float        # positive shares taken from this Trade


@dataclass(frozen=True)
class Proposal:
    """A single item awaiting confirm. ``kind`` is ``new-trade`` | ``exit-allocation``."""

    kind: str
    book: str
    symbol: str
    note: str = ""
    # new-trade
    entry_date: Optional[str] = None
    quantity: float = 0.0
    avg_price: Optional[float] = None
    # exit-allocation
    exit_date: Optional[str] = None
    price: Optional[float] = None
    source: Optional[str] = None
    source_ref: Optional[str] = None
    allocations: Tuple[Allocation, ...] = ()
    over_allocated: float = 0.0


@dataclass
class _Lot:
    """A Trade's open quantity while allocating sells (a mutable working copy)."""

    book: str
    symbol: str
    entry_date: str
    open_qty: float


@dataclass(frozen=True)
class _Sell:
    source: str
    source_ref: str
    book: str
    symbol: str
    exit_date: str
    quantity: float        # positive shares
    price: float


def latest_fills(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every logical Fill at its highest revision (ADR 0003: highest wins).

    A restatement lands beside the earlier revision, so deriving Trades must
    take one row per ``(source, source_ref)`` — the greatest ``revision``.
    """
    return list(
        conn.execute(
            """
            SELECT f.* FROM fill f
            WHERE f.revision = (
                SELECT MAX(f2.revision) FROM fill f2
                WHERE f2.source = f.source AND f2.source_ref = f.source_ref
            )
            ORDER BY f.executed_at, f.source_ref
            """
        )
    )


def _entry_date(executed_at: str) -> str:
    """The calendar trade date of a fill — the cohort axis (ADR 0001)."""
    return executed_at[:10]


def _committed_lots(conn: sqlite3.Connection) -> Tuple[List[_Lot], Dict[Tuple[str, str, str], int]]:
    """Open lots for already-committed Trades, plus a cohort-key → id map.

    A Trade's open quantity is its entry quantity less everything already
    allocated to it — derived, never stored, so it always ties back to the
    Fills and Exits on record.
    """
    lots: List[_Lot] = []
    ids: Dict[Tuple[str, str, str], int] = {}
    for t in conn.execute(
        """
        SELECT t.id, t.book, t.symbol, t.entry_date, t.entry_qty,
               COALESCE((SELECT SUM(x.quantity) FROM trade_exit x WHERE x.trade_id = t.id), 0)
                   AS allocated
        FROM trade t
        ORDER BY t.entry_date, t.id
        """
    ):
        ids[(t["book"], t["symbol"], t["entry_date"])] = t["id"]
        lots.append(
            _Lot(
                book=t["book"],
                symbol=t["symbol"],
                entry_date=t["entry_date"],
                open_qty=t["entry_qty"] - t["allocated"],
            )
        )
    return lots, ids


def _new_trade_proposals(
    fills: Sequence[sqlite3.Row],
    committed_keys: Sequence[Tuple[str, str, str]],
    open_symbols: Mapping[Tuple[str, str], List[str]],
) -> List[Proposal]:
    """Group un-committed buys into entry-day cohorts (ADR 0001)."""
    committed = set(committed_keys)
    cohorts: Dict[Tuple[str, str, str], List[sqlite3.Row]] = {}
    for f in fills:
        if f["side"] != "BUY":
            continue
        key = (f["book"], f["symbol"], _entry_date(f["executed_at"]))
        cohorts.setdefault(key, []).append(f)

    # Sibling entry days in this same batch count too: a Monday and a Wednesday
    # cohort dropped together are two Trades, and each says so about the other.
    batch_dates: Dict[Tuple[str, str], set] = {}
    for (book, symbol, date) in cohorts:
        batch_dates.setdefault((book, symbol), set()).add(date)

    proposals: List[Proposal] = []
    for (book, symbol, date), group in cohorts.items():
        if (book, symbol, date) in committed:
            continue  # this cohort is already a Trade; a re-drop adds nothing
        qty = sum(abs(f["quantity"]) for f in group)
        # Quantity-weighted mean: avg_price * qty == the cash that left (SPEC §3.1).
        weighted = sum(abs(f["quantity"]) * f["price"] for f in group)
        avg_price = weighted / qty if qty else 0.0
        siblings = set(open_symbols.get((book, symbol), [])) | batch_dates[(book, symbol)]
        others = [d for d in siblings if d != date]
        note = (
            f"A different entry day is a different Trade — this does not add to "
            f"the open {symbol} Trade(s) entered {', '.join(sorted(others))}."
            if others
            else "First open Trade in this symbol."
        )
        proposals.append(
            Proposal(
                kind="new-trade",
                book=book,
                symbol=symbol,
                entry_date=date,
                quantity=qty,
                avg_price=avg_price,
                note=note,
            )
        )
    return proposals


def _unallocated_sells(
    conn: sqlite3.Connection, fills: Sequence[sqlite3.Row]
) -> List[_Sell]:
    """Sell fills not yet allocated to any Trade (idempotent on ``source_ref``)."""
    allocated = {
        (r["source"], r["source_ref"])
        for r in conn.execute("SELECT source, source_ref FROM trade_exit")
    }
    sells = []
    for f in fills:
        if f["side"] != "SELL":
            continue
        if (f["source"], f["source_ref"]) in allocated:
            continue
        sells.append(
            _Sell(
                source=f["source"],
                source_ref=f["source_ref"],
                book=f["book"],
                symbol=f["symbol"],
                exit_date=_entry_date(f["executed_at"]),
                quantity=abs(f["quantity"]),
                price=f["price"],
            )
        )
    return sells


def _allocate_fifo(sell: _Sell, lots: Sequence[_Lot]) -> Tuple[List[Allocation], float]:
    """Take ``sell.quantity`` from open lots oldest-first, mutating ``open_qty``.

    Returns the allocations and any remainder that no open Trade could absorb
    (an over-allocation — a sell with no journalled entry to come out of).
    """
    remaining = sell.quantity
    allocations: List[Allocation] = []
    for lot in lots:
        if remaining <= 0:
            break
        if lot.book != sell.book or lot.symbol != sell.symbol or lot.open_qty <= 0:
            continue
        take = min(remaining, lot.open_qty)
        lot.open_qty -= take
        remaining -= take
        allocations.append(
            Allocation(
                book=lot.book,
                symbol=lot.symbol,
                entry_date=lot.entry_date,
                quantity=take,
            )
        )
    return allocations, remaining


def propose(conn: sqlite3.Connection) -> List[Proposal]:
    """Derive proposals from the Fill ledger. Commits nothing (SPEC §5.1)."""
    fills = latest_fills(conn)
    committed_lots, ids = _committed_lots(conn)

    open_symbols: Dict[Tuple[str, str], List[str]] = {}
    for lot in committed_lots:
        if lot.open_qty > 0:
            open_symbols.setdefault((lot.book, lot.symbol), []).append(lot.entry_date)

    new_trades = _new_trade_proposals(fills, list(ids.keys()), open_symbols)

    # Sells allocate across committed-open lots *and* the cohorts we are about to
    # propose — so an entry and its exit dropped together allocate cleanly.
    lots = [_Lot(l.book, l.symbol, l.entry_date, l.open_qty) for l in committed_lots]
    for nt in new_trades:
        lots.append(_Lot(nt.book, nt.symbol, nt.entry_date or "", nt.quantity))
    lots.sort(key=lambda l: (l.book, l.symbol, l.entry_date))

    exits: List[Proposal] = []
    for sell in _unallocated_sells(conn, fills):
        allocations, remainder = _allocate_fifo(sell, lots)
        exits.append(
            Proposal(
                kind="exit-allocation",
                book=sell.book,
                symbol=sell.symbol,
                exit_date=sell.exit_date,
                quantity=sell.quantity,
                price=sell.price,
                source=sell.source,
                source_ref=sell.source_ref,
                allocations=tuple(allocations),
                over_allocated=remainder,
                note=(
                    f"{remainder:g} share(s) have no open {sell.symbol} Trade to "
                    "come out of — parks until the entry is journalled."
                    if remainder > 0
                    else "Allocated FIFO — oldest open Trade first."
                ),
            )
        )
    return new_trades + exits


@dataclass
class ConfirmResult:
    new_trades: int = 0
    exits_allocated: int = 0
    parked_exits: int = 0
    closed_trades: List[str] = field(default_factory=list)


# An override maps a sell's source_ref to the (entry_date, quantity) slices the
# trader wants instead of FIFO. Bounded by what each Trade holds open (SPEC §3.4).
Override = Mapping[str, Sequence[Tuple[str, float]]]


class AllocationError(ValueError):
    """An override that over-allocates a Trade or mis-totals a sell (SPEC §3.4)."""


def confirm(conn: sqlite3.Connection, overrides: Optional[Override] = None) -> ConfirmResult:
    """Commit the proposals: land new Trades, allocate exits, close what filled.

    This is the only writer. New Trades come from the entry-day grouping; exits
    default to FIFO but an ``overrides`` entry for a sell replaces its allocation,
    validated so no Trade is allocated more than it holds open.
    """
    overrides = overrides or {}
    result = ConfirmResult()
    proposals = propose(conn)

    # Land new Trades first so exits in the same batch have a Trade to hit.
    for p in proposals:
        if p.kind != "new-trade":
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO trade (book, symbol, entry_date, entry_qty, "
            "entry_avg_price, status) VALUES (?, ?, ?, ?, ?, 'open')",
            (p.book, p.symbol, p.entry_date, p.quantity, p.avg_price),
        )
        result.new_trades += cur.rowcount

    # Resolve every cohort key to its trade id (committed just now or earlier).
    ids = {
        (r["book"], r["symbol"], r["entry_date"]): r["id"]
        for r in conn.execute("SELECT id, book, symbol, entry_date FROM trade")
    }
    # Working open quantities, so overrides can be bounds-checked as we go.
    open_qty = {
        r["id"]: r["entry_qty"]
        - (r["allocated"] or 0)
        for r in conn.execute(
            "SELECT t.id, t.entry_qty, "
            "(SELECT SUM(x.quantity) FROM trade_exit x WHERE x.trade_id = t.id) AS allocated "
            "FROM trade t"
        )
    }

    touched: set[int] = set()
    for p in proposals:
        if p.kind != "exit-allocation":
            continue
        allocations = _resolve_allocations(p, overrides, ids, open_qty)
        if allocations is None:
            result.parked_exits += 1
            continue
        for alloc in allocations:
            trade_id = ids[(alloc.book, alloc.symbol, alloc.entry_date)]
            conn.execute(
                "INSERT INTO trade_exit (trade_id, source, source_ref, exit_date, "
                "quantity, price) VALUES (?, ?, ?, ?, ?, ?)",
                (trade_id, p.source, p.source_ref, p.exit_date, alloc.quantity, p.price),
            )
            open_qty[trade_id] -= alloc.quantity
            touched.add(trade_id)
        result.exits_allocated += 1

    # A Trade whose open quantity reached zero advances open → closed (SPEC §3.5).
    for trade_id in touched:
        if open_qty[trade_id] <= 1e-9:
            row = conn.execute(
                "SELECT symbol, entry_date FROM trade WHERE id = ?", (trade_id,)
            ).fetchone()
            conn.execute(
                "UPDATE trade SET status = 'closed' WHERE id = ?", (trade_id,)
            )
            result.closed_trades.append(f"{row['symbol']} {row['entry_date']}")

    conn.commit()
    return result


def _resolve_allocations(
    proposal: Proposal,
    overrides: Override,
    ids: Mapping[Tuple[str, str, str], int],
    open_qty: Mapping[int, float],
) -> Optional[List[Allocation]]:
    """FIFO allocations for a sell, or a validated override; ``None`` if it parks."""
    override = overrides.get(proposal.source_ref) if proposal.source_ref else None
    if override is not None:
        allocations = [
            Allocation(proposal.book, proposal.symbol, entry_date, qty)
            for entry_date, qty in override
        ]
        _validate_override(proposal, allocations, ids, open_qty)
        return allocations

    if proposal.over_allocated > 0:
        return None  # nothing (or not enough) open to absorb it — it parks
    return list(proposal.allocations)


def _validate_override(
    proposal: Proposal,
    allocations: Sequence[Allocation],
    ids: Mapping[Tuple[str, str, str], int],
    open_qty: Mapping[int, float],
) -> None:
    total = 0.0
    for alloc in allocations:
        key = (alloc.book, alloc.symbol, alloc.entry_date)
        if key not in ids:
            raise AllocationError(
                f"override targets {alloc.symbol} {alloc.entry_date}, which is not a Trade"
            )
        if alloc.quantity > open_qty[ids[key]] + 1e-9:
            raise AllocationError(
                f"override allocates {alloc.quantity:g} to {alloc.symbol} "
                f"{alloc.entry_date}, more than the {open_qty[ids[key]]:g} it holds open"
            )
        total += alloc.quantity
    if abs(total - proposal.quantity) > 1e-9:
        raise AllocationError(
            f"override allocates {total:g} of a {proposal.quantity:g}-share sell"
        )
