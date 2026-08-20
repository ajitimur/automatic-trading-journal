"""The full confirm queue — Trades derived from Fills through one door (SPEC §5, #27).

A **Trade is an entry-day cohort** (ADR 0001): all entry (BUY) fills for one
symbol on one book on one calendar date, plus the exits allocated to it.
Entering the same symbol on a *later* day is a **second Trade, never an
addition** — the counter-intuitive case the proposal states out loud.

**One confirm queue is the only thing that commits** (SPEC §5.1). Broker imports
and hand-entered backdated Trades arrive through it identically. Nothing here
writes a Trade on its own: :func:`propose` reads the Fill ledger and returns
*proposals*; :func:`confirm` is the only writer.

**Every failure is one of eight proposal kinds, never an exception** (SPEC §5.2)::

    new-trade · add-fills · exit-allocation · restatement · quarantine
    · orphan-exit · enrichment-repair · drift

**Blocked items park; they never stall** (SPEC §5.2). A parked item (an
orphan-exit, or an exit that cannot fully allocate) sinks below the confirmable
ones and confirm skips it — one orphan exit must not halt the week's whole
import. Because the entry side is *re-derived from Fills on every confirm*,
re-checking a parked item is inherent: hand-enter the missing Trade and the next
confirm allocates the sell that was waiting on it — no re-drop (SPEC §5.2).

**Corrections: a fact once, a rule forever** (SPEC §5.4). A wrong *quantity* is a
fact about one fill — :func:`correct_quantity` lands a corrected revision and it
is not remembered. A wrong *symbol* is a *rule* about the parser —
:func:`remember_symbol_rule` stores it, :func:`apply_symbol_rules` applies it
before anything reaches the queue again, and it repairs Trades already committed
under the wrong symbol.

**Bulk confirm covers exit reasons and nothing else** (SPEC §5.8):
:func:`bulk_confirm_exits` accepts a batch of exit reasons in one action and
leaves new Trades and parked items untouched.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from . import stockbit, stops

# The eight proposal kinds — every failure path produces one of these, never an
# exception (SPEC §5.2). Enumerated so a caller can assert the taxonomy is total.
PROPOSAL_KINDS = (
    "new-trade",
    "add-fills",
    "exit-allocation",
    "restatement",
    "quarantine",
    "orphan-exit",
    "enrichment-repair",
    "drift",
)

# Exit reasons (SPEC §7 / §5.8, the prototype's EXIT_REASONS). The proposed
# reason is the bars-less default — a partial sell reads as taking strength off,
# a full close as an MA10 break — good enough to accept unread in the weekly
# rhythm, and overridable on the review surface (SPEC §11.3).
EXIT_REASONS = (
    "partial_strength",
    "close_below_ma10",
    "close_below_ma20",
    "stop_hit",
    "written_off",       # a delisting/suspension end — freezes immediately (SPEC §3.5, #34)
    "discretionary",
)


@dataclass(frozen=True)
class Allocation:
    """One slice of a sell Fill assigned to a Trade's open quantity."""

    book: str
    symbol: str
    entry_date: str        # the cohort key — resolves to a trade id at commit
    quantity: float        # positive shares taken from this Trade


@dataclass(frozen=True)
class Proposal:
    """A single item awaiting confirm. ``kind`` is one of :data:`PROPOSAL_KINDS`.

    The **interpreted Trade is the shape carried here** — symbol, entry date,
    quantity, average price — with the raw broker rows one disclosure away in the
    Fill ledger (SPEC §5.9). A proposal never commits; :func:`confirm` does.
    """

    kind: str
    book: str
    symbol: str
    note: str = ""
    trade_id: Optional[int] = None      # add-fills/restatement/drift/enrichment-repair target
    # new-trade / add-fills
    entry_date: Optional[str] = None
    quantity: float = 0.0
    avg_price: Optional[float] = None
    # exit-allocation / orphan-exit
    exit_date: Optional[str] = None
    price: Optional[float] = None
    source: Optional[str] = None
    source_ref: Optional[str] = None
    allocations: Tuple[Allocation, ...] = ()
    over_allocated: float = 0.0
    proposed_reason: Optional[str] = None
    # add-fills / restatement / drift — what the committed snapshot holds vs. the
    # ledger now derives, so the human sees the delta before it lands.
    stored_qty: Optional[float] = None
    derived_qty: Optional[float] = None
    # quarantine
    detail: Optional[str] = None

    @property
    def blocked(self) -> bool:
        """A parked item: it sinks below the confirmable ones and confirm skips it.

        An ``orphan-exit`` has nothing journalled to come out of, so it parks
        until the missing entry is journalled (SPEC §5.2). ``quarantine``,
        ``enrichment-repair`` and ``drift`` are attention items, not
        confirmable-in-place, and count as parked for the skip.

        An ``exit-allocation`` that over-allocates does **not** park. §3.4 forbids
        allocating a Trade more than it holds open; it does not require refusing
        the part that fits. Parking the whole proposal turned a partly-known exit
        into a wholly-unknown one and left Trades reading ``open`` months after
        they were sold — the remainder resurfaces as an ``orphan-exit`` instead.
        """
        return self.kind in ("orphan-exit", "quarantine", "enrichment-repair", "drift")


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


def _derive_buy_cohorts(
    fills: Sequence[sqlite3.Row],
) -> Dict[Tuple[str, str, str], Tuple[float, float]]:
    """Entry-day cohort → (quantity, quantity-weighted avg price) from BUY fills.

    The one place the entry side is derived (ADR 0001): a quantity-weighted mean
    so ``avg_price * qty`` equals the cash that left (SPEC §3.1). Both the confirm
    re-derivation and the drift override read the same shape from here.
    """
    totals: Dict[Tuple[str, str, str], Tuple[float, float]] = {}
    for f in fills:
        if f["side"] != "BUY":
            continue
        key = (f["book"], f["symbol"], _entry_date(f["executed_at"]))
        qty, weighted = totals.get(key, (0.0, 0.0))
        totals[key] = (qty + abs(f["quantity"]), weighted + abs(f["quantity"]) * f["price"])
    return {k: (q, w / q if q else 0.0) for k, (q, w) in totals.items()}


@dataclass(frozen=True)
class _Committed:
    """A committed Trade's snapshot, for detecting add-fills / restatement / drift."""

    id: int
    book: str
    symbol: str
    entry_date: str
    entry_qty: float
    entry_avg_price: float
    frozen: bool
    open_qty: float


def _committed_trades(
    conn: sqlite3.Connection,
) -> Tuple[List[_Committed], Dict[Tuple[str, str, str], _Committed]]:
    """Every committed Trade with its derived open quantity and its stored snapshot.

    A Trade's open quantity is its entry quantity less everything already
    allocated to it — derived, never stored, so it always ties back to the Fills
    and Exits on record. The stored ``entry_qty``/``entry_avg_price`` are the
    snapshot a fresh derivation is compared against to spot an add-fills or a
    restatement.
    """
    committed: List[_Committed] = []
    by_cohort: Dict[Tuple[str, str, str], _Committed] = {}
    for t in conn.execute(
        """
        SELECT t.id, t.book, t.symbol, t.entry_date, t.entry_qty, t.entry_avg_price,
               t.frozen,
               COALESCE((SELECT SUM(x.quantity) FROM trade_exit x WHERE x.trade_id = t.id), 0)
                   AS allocated
        FROM trade t
        ORDER BY t.entry_date, t.id
        """
    ):
        c = _Committed(
            id=t["id"],
            book=t["book"],
            symbol=t["symbol"],
            entry_date=t["entry_date"],
            entry_qty=t["entry_qty"],
            entry_avg_price=t["entry_avg_price"],
            frozen=bool(t["frozen"]),
            open_qty=t["entry_qty"] - t["allocated"],
        )
        committed.append(c)
        by_cohort[(c.book, c.symbol, c.entry_date)] = c
    return committed, by_cohort


def _entry_proposals(
    fills: Sequence[sqlite3.Row],
    committed: Mapping[Tuple[str, str, str], _Committed],
    open_symbols: Mapping[Tuple[str, str], List[str]],
) -> List[Proposal]:
    """Turn buy cohorts into entry proposals (ADR 0001, SPEC §5.2/§5.3).

    A cohort with no committed Trade is a ``new-trade``. A cohort that *is*
    already a Trade but whose derivation from the Fills now differs is either an
    ``add-fills`` (new logical buys landed in the same entry day) or a
    ``restatement`` (the broker restated an existing buy as a higher revision) —
    unless the Trade already froze, in which case the change becomes ``drift``,
    never a silent rewrite (SPEC §5.3).
    """
    cohorts: Dict[Tuple[str, str, str], List[sqlite3.Row]] = {}
    for f in fills:
        if f["side"] != "BUY":
            continue
        key = (f["book"], f["symbol"], _entry_date(f["executed_at"]))
        cohorts.setdefault(key, []).append(f)
    totals = _derive_buy_cohorts(fills)

    # Sibling entry days in this same batch count too: a Monday and a Wednesday
    # cohort dropped together are two Trades, and each says so about the other.
    batch_dates: Dict[Tuple[str, str], set] = {}
    for (book, symbol, date) in cohorts:
        batch_dates.setdefault((book, symbol), set()).add(date)

    proposals: List[Proposal] = []
    for (book, symbol, date), group in cohorts.items():
        qty, avg_price = totals[(book, symbol, date)]
        prior = committed.get((book, symbol, date))

        if prior is None:
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
            continue

        # Already a Trade. Nothing to say unless the Fills now derive differently.
        if _close(qty, prior.entry_qty) and _close(avg_price, prior.entry_avg_price):
            continue

        restated = any(f["revision"] > 1 for f in group)
        if prior.frozen:
            kind, note = "drift", (
                f"{symbol} froze at {prior.entry_qty:g} @ {prior.entry_avg_price:.4f}, "
                f"but the Fills now derive {qty:g} @ {avg_price:.4f}. The snapshot was "
                "built on a fact that changed — apply only if a broker restated it, "
                "never rewrite it for moved data (SPEC §5.3)."
            )
        elif restated:
            kind, note = "restatement", (
                f"A buy in this {symbol} {date} Trade was restated: it now derives "
                f"{qty:g} @ {avg_price:.4f} (was {prior.entry_qty:g} @ "
                f"{prior.entry_avg_price:.4f}). Confirm re-derives; the old revision is kept."
            )
        else:
            kind, note = "add-fills", (
                f"{qty - prior.entry_qty:g} more share(s) landed in the same {symbol} "
                f"{date} entry day — same Trade, re-derived to {qty:g} @ {avg_price:.4f} "
                "(a later day would be a separate Trade)."
            )
        proposals.append(
            Proposal(
                kind=kind,
                book=book,
                symbol=symbol,
                entry_date=date,
                trade_id=prior.id,
                quantity=qty,
                avg_price=avg_price,
                stored_qty=prior.entry_qty,
                derived_qty=qty,
                note=note,
            )
        )
    return proposals


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def _unallocated_sells(
    conn: sqlite3.Connection, fills: Sequence[sqlite3.Row]
) -> List[_Sell]:
    """The still-unallocated *quantity* of each sell Fill (idempotent on ``source_ref``).

    Allocation is a quantity question, not a boolean: a sell larger than the
    Trades journalled as open allocates as far as it can and leaves a residual
    (SPEC §3.4). Reporting that residual here is what keeps it visible — on the
    next derivation it has no open capacity left to take it, so it resurfaces as
    an ``orphan-exit`` rather than vanishing because the Fill was "seen".
    """
    allocated: Dict[Tuple[str, str], float] = {}
    for r in conn.execute(
        "SELECT source, source_ref, SUM(quantity) q FROM trade_exit "
        "GROUP BY source, source_ref"
    ):
        allocated[(r["source"], r["source_ref"])] = r["q"] or 0.0
    sells = []
    for f in fills:
        if f["side"] != "SELL":
            continue
        residual = abs(f["quantity"]) - allocated.get((f["source"], f["source_ref"]), 0.0)
        if residual <= 1e-9:
            continue
        sells.append(
            _Sell(
                source=f["source"],
                source_ref=f["source_ref"],
                book=f["book"],
                symbol=f["symbol"],
                exit_date=_entry_date(f["executed_at"]),
                quantity=residual,
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
    """Derive the full proposal queue from the Fill ledger. Commits nothing (SPEC §5.1).

    Confirmable items come first and parked items (orphan exits, unfillable
    exits, enrichment repairs) sink to the bottom, so ``confirm`` skips them and
    one blocked item never stalls the batch (SPEC §5.2).
    """
    fills = latest_fills(conn)
    committed_lots, by_cohort = _committed_trades(conn)

    open_symbols: Dict[Tuple[str, str], List[str]] = {}
    for lot in committed_lots:
        if lot.open_qty > 0:
            open_symbols.setdefault((lot.book, lot.symbol), []).append(lot.entry_date)

    entries = _entry_proposals(fills, by_cohort, open_symbols)

    # Sells allocate across committed-open lots *and* the cohorts we are about to
    # propose — so an entry and its exit dropped together allocate cleanly.
    lots = [_Lot(l.book, l.symbol, l.entry_date, l.open_qty) for l in committed_lots]
    for e in entries:
        if e.kind == "new-trade":
            lots.append(_Lot(e.book, e.symbol, e.entry_date or "", e.quantity))
    lots.sort(key=lambda l: (l.book, l.symbol, l.entry_date))

    exits: List[Proposal] = []
    for sell in _unallocated_sells(conn, fills):
        capacity = sum(l.open_qty for l in lots if l.book == sell.book and l.symbol == sell.symbol)
        allocations, remainder = _allocate_fifo(sell, lots)
        if capacity <= 0:
            # Nothing in the symbol is journalled as open — a distinct kind, not a
            # partial fill. It parks; RECHECK (re-derive) clears it once the
            # missing Trade is entered by hand (SPEC §5.2).
            exits.append(
                Proposal(
                    kind="orphan-exit",
                    book=sell.book,
                    symbol=sell.symbol,
                    exit_date=sell.exit_date,
                    quantity=sell.quantity,
                    price=sell.price,
                    source=sell.source,
                    source_ref=sell.source_ref,
                    over_allocated=remainder,
                    note=(
                        f"{sell.quantity:g} share(s) of {sell.symbol} sold on "
                        f"{sell.exit_date}, but nothing in {sell.symbol} is journalled "
                        "as open. Enter the missing Trade by hand, then RECHECK."
                    ),
                )
            )
            continue
        # A partial sell reads as taking strength off, a full close as an MA10
        # break — the bars-less default, overridable on the review surface (§5.8).
        proposed_reason = "partial_strength" if sell.quantity < capacity else "close_below_ma10"
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
                proposed_reason=proposed_reason,
                note=(
                    f"{remainder:g} share(s) have no open {sell.symbol} Trade to "
                    "come out of — parks until the entry is journalled."
                    if remainder > 0
                    else "Allocated FIFO — oldest open Trade first."
                ),
            )
        )

    repairs = _enrichment_repairs(conn, committed_lots)

    # Confirmable first, then parked — the queue ordering the skip relies on.
    ordered = [p for p in entries + exits if not p.blocked]
    parked = [p for p in entries + exits + repairs if p.blocked]
    return ordered + parked


def _enrichment_repairs(
    conn: sqlite3.Connection, committed: Sequence[_Committed]
) -> List[Proposal]:
    """A committed Trade whose symbol has no bars, while its book has others (SPEC §5.7).

    This is a *repair*, not "no data": the daily job is fetching bars for the
    book, but this symbol is dark — the usual cause is a bad symbol mapping. An
    empty bar cache (nothing fetched yet) is not a repair and stays silent.
    """
    repairs: List[Proposal] = []
    for t in committed:
        book_has_bars = conn.execute(
            "SELECT 1 FROM bar WHERE book = ? LIMIT 1", (t.book,)
        ).fetchone()
        if not book_has_bars:
            continue
        symbol_spans = conn.execute(
            "SELECT 1 FROM bar WHERE book = ? AND symbol = ? AND date <= ? LIMIT 1",
            (t.book, t.symbol, t.entry_date),
        ).fetchone()
        if symbol_spans:
            continue
        repairs.append(
            Proposal(
                kind="enrichment-repair",
                book=t.book,
                symbol=t.symbol,
                entry_date=t.entry_date,
                trade_id=t.id,
                note=(
                    f"{t.symbol} is committed, but no bar series spans {t.entry_date}. "
                    "Every derived field is held for repair — fix the symbol mapping or "
                    "the source, then re-run enrichment. The Trade itself is safe."
                ),
            )
        )
    return repairs


@dataclass
class ConfirmResult:
    new_trades: int = 0
    added_fills: int = 0
    restatements: int = 0
    drifts: int = 0
    exits_allocated: int = 0
    parked_exits: int = 0
    closed_trades: List[str] = field(default_factory=list)
    # New Trades held back because the stop demand went unanswered (ADR 0010).
    # Their Fills stay in the ledger and they re-propose on the next confirm —
    # nothing is lost, and the answer is one flag away.
    unanswered: List[str] = field(default_factory=list)
    stops_declined: int = 0


# An override maps a sell's source_ref to the (entry_date, quantity) slices the
# trader wants instead of FIFO. Bounded by what each Trade holds open (SPEC §3.4).
Override = Mapping[str, Sequence[Tuple[str, float]]]
# A reason map assigns an exit reason to a sell's source_ref, overriding the
# proposed default (SPEC §5.8). Anything unlisted keeps its proposed reason.
Reasons = Mapping[str, str]


class AllocationError(ValueError):
    """An override that over-allocates a Trade or mis-totals a sell (SPEC §3.4)."""


def confirm(
    conn: sqlite3.Connection,
    overrides: Optional[Override] = None,
    reasons: Optional[Reasons] = None,
    stops_by_symbol: Optional[Mapping[str, float]] = None,
    declined: Optional[Sequence[str]] = None,
    demand_stop: bool = False,
) -> ConfirmResult:
    """Commit the proposals: land new Trades, apply add-fills/restatements, allocate exits.

    This is the only writer (SPEC §5.1). New Trades come from the entry-day
    grouping; add-fills and restatements re-derive the entry side of a non-frozen
    committed Trade from its Fills; exits default to FIFO with a bounded override
    and carry an exit reason. **Blocked items park** — an orphan exit or a sell
    that cannot fully allocate is skipped, never a roadblock (SPEC §5.2). Drift on
    a frozen Trade is surfaced but never applied here (see :func:`apply_drift`).

    **Under ``demand_stop`` the stop is demanded, and answerable two ways**
    (ADR 0010, reversing SPEC §5.5's chaseable path). A new Trade lands only once
    its symbol appears in ``stops_by_symbol`` or in ``declined``. What is refused
    is not a missing stop — it is committing *without answering*: the chaseable
    path returned zero stops over 207 Trades because "later" was always available
    and never chosen. Declining is one flag and is recorded, so the hole is a
    decision on the record rather than an omission nobody sees until freeze.

    An unanswered Trade is **held, not lost**: its Fills are untouched, it
    re-proposes on the next confirm, and §5.7's "the fills are facts" still holds
    — the ledger never rejected anything, only the derived Trade waits.

    The demand is **off by default** because it is a property of the door, not of
    the commit: ``journal confirm`` turns it on, and that is the only way a Trade
    reaches this function from a human. Leaving the primitive ungated keeps the
    mechanical concerns — cohorts, FIFO, restatement — testable without every
    case having to answer a workflow question it is not about. ``stops_by_symbol``
    is still honoured when the demand is off, so a caller may supply a stop
    without being forced to.
    """
    overrides = overrides or {}
    reasons = reasons or {}
    stops_by_symbol = stops_by_symbol or {}
    declined_set = set(declined or ())
    result = ConfirmResult()
    proposals = propose(conn)

    # Land new Trades first so exits in the same batch have a Trade to hit.
    answered: List[Proposal] = []
    for p in proposals:
        if p.kind != "new-trade":
            continue
        if (
            demand_stop
            and p.symbol not in stops_by_symbol
            and p.symbol not in declined_set
        ):
            result.unanswered.append(f"{p.book} {p.symbol} {p.entry_date}")
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO trade (book, symbol, entry_date, entry_qty, "
            "entry_avg_price, status) VALUES (?, ?, ?, ?, ?, 'open')",
            (p.book, p.symbol, p.entry_date, p.quantity, p.avg_price),
        )
        result.new_trades += cur.rowcount
        answered.append(p)

    result.added_fills = sum(1 for p in proposals if p.kind == "add-fills")
    result.restatements = sum(1 for p in proposals if p.kind == "restatement")
    result.drifts = sum(1 for p in proposals if p.kind == "drift")
    # Add-fills and restatement land by re-deriving the entry side from the Fills.
    _rederive_entry_sides(conn)

    # Resolve every cohort key to its trade id (committed just now or earlier).
    ids = _trade_ids(conn)

    # Record the answer to the stop demand, now that the Trades have ids. A stop
    # supplied here derives `recorded` — no Exit can precede a Trade's own commit
    # (ADR 0009's grace window is for the ones answered a day or two later).
    for p in answered:
        trade_id = ids.get((p.book, p.symbol, p.entry_date))
        if trade_id is None:
            continue
        if p.symbol in stops_by_symbol:
            stops.set_stop(conn, trade_id, stops_by_symbol[p.symbol])
        else:
            conn.execute(
                "UPDATE trade SET stop_declined = 1 WHERE id = ?", (trade_id,)
            )
            result.stops_declined += 1

    # Working open quantities, so overrides can be bounds-checked as we go.
    open_qty = _working_open_qty(conn)

    touched: set[int] = set()
    for p in proposals:
        if p.kind == "orphan-exit":
            result.parked_exits += 1
            continue
        if p.kind != "exit-allocation":
            continue
        allocations = _resolve_allocations(p, overrides, ids, open_qty)
        if p.over_allocated > 0:
            # The part that fits commits below; the remainder stays unallocated
            # on the Fill and re-derives as an orphan exit (SPEC §3.4, §5.2).
            result.parked_exits += 1
        reason = reasons.get(p.source_ref or "") or p.proposed_reason
        _commit_exit(conn, p, allocations, reason, ids, open_qty, touched)
        result.exits_allocated += 1

    result.closed_trades = _close_filled(conn, open_qty, touched)
    conn.commit()
    return result


def _trade_ids(conn: sqlite3.Connection) -> Dict[Tuple[str, str, str], int]:
    """Cohort key → committed trade id, for resolving an allocation to its target."""
    return {
        (r["book"], r["symbol"], r["entry_date"]): r["id"]
        for r in conn.execute("SELECT id, book, symbol, entry_date FROM trade")
    }


def _working_open_qty(conn: sqlite3.Connection) -> Dict[int, float]:
    """Each Trade's derived open quantity — entry less everything allocated to it."""
    return {
        r["id"]: r["entry_qty"] - (r["allocated"] or 0)
        for r in conn.execute(
            "SELECT t.id, t.entry_qty, "
            "(SELECT SUM(x.quantity) FROM trade_exit x WHERE x.trade_id = t.id) AS allocated "
            "FROM trade t"
        )
    }


def _rederive_entry_sides(conn: sqlite3.Connection) -> None:
    """Recompute entry_qty/entry_avg_price for non-frozen Trades from their buy Fills.

    The entry side is a pure function of the Fills (ADR 0001), so this applies an
    add-fills or a restatement idempotently — a Trade already reflecting its Fills
    is left unchanged. Frozen Trades are never rewritten; a change to one is drift
    (SPEC §5.3), applied only through :func:`apply_drift`.
    """
    derived = _derive_buy_cohorts(latest_fills(conn))
    for t in conn.execute(
        "SELECT id, book, symbol, entry_date, entry_qty, entry_avg_price, frozen FROM trade"
    ):
        if t["frozen"]:
            continue
        got = derived.get((t["book"], t["symbol"], t["entry_date"]))
        if got is None:
            continue
        qty, avg = got
        if not (_close(qty, t["entry_qty"]) and _close(avg, t["entry_avg_price"])):
            conn.execute(
                "UPDATE trade SET entry_qty = ?, entry_avg_price = ? WHERE id = ?",
                (qty, avg, t["id"]),
            )


def _commit_exit(
    conn: sqlite3.Connection,
    proposal: Proposal,
    allocations: Sequence[Allocation],
    reason: Optional[str],
    ids: Mapping[Tuple[str, str, str], int],
    open_qty: Dict[int, float],
    touched: set,
) -> None:
    """Write one sell's allocations to ``trade_exit`` with its reason (SPEC §5.8)."""
    for alloc in allocations:
        trade_id = ids[(alloc.book, alloc.symbol, alloc.entry_date)]
        conn.execute(
            "INSERT INTO trade_exit (trade_id, source, source_ref, exit_date, "
            "quantity, price, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade_id, proposal.source, proposal.source_ref, proposal.exit_date,
             alloc.quantity, proposal.price, reason),
        )
        open_qty[trade_id] -= alloc.quantity
        touched.add(trade_id)


def _close_filled(
    conn: sqlite3.Connection, open_qty: Mapping[int, float], touched: set
) -> List[str]:
    """A Trade whose open quantity reached zero advances open → closed (SPEC §3.5)."""
    closed: List[str] = []
    for trade_id in touched:
        if open_qty[trade_id] <= 1e-9:
            row = conn.execute(
                "SELECT symbol, entry_date FROM trade WHERE id = ?", (trade_id,)
            ).fetchone()
            conn.execute("UPDATE trade SET status = 'closed' WHERE id = ?", (trade_id,))
            closed.append(f"{row['symbol']} {row['entry_date']}")
    return closed


def _resolve_allocations(
    proposal: Proposal,
    overrides: Override,
    ids: Mapping[Tuple[str, str, str], int],
    open_qty: Mapping[int, float],
) -> List[Allocation]:
    """FIFO allocations for a sell, or a validated override.

    Always allocates as much as the open Trades can absorb; an over-allocating
    sell leaves its remainder unallocated rather than refusing outright.
    """
    override = overrides.get(proposal.source_ref) if proposal.source_ref else None
    if override is not None:
        allocations = [
            Allocation(proposal.book, proposal.symbol, entry_date, qty)
            for entry_date, qty in override
        ]
        _validate_override(proposal, allocations, ids, open_qty)
        return allocations

    # An over-allocating sell still commits the part that fits (§3.4); the
    # remainder stays unallocated on the Fill and re-derives as an orphan exit.
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


# ── Bulk confirm: exit reasons only (SPEC §5.8) ──────────────────────────────
def bulk_confirm_exits(
    conn: sqlite3.Connection, reasons: Optional[Reasons] = None
) -> ConfirmResult:
    """Confirm every confirmable exit in one action, at its reason. Nothing else.

    A week of statements is mostly agreeing with the exit reasons enrichment
    proposed, so those go in one action (SPEC §5.8). **New Trades are left one at
    a time** — a mis-parsed quantity there poisons everything downstream, and
    catching it is what confirm exists for. **Parked items are untouched.** An
    exit whose allocation targets a cohort not yet committed is skipped too, so
    this never lands a new Trade as a side effect.
    """
    reasons = reasons or {}
    result = ConfirmResult()
    ids = _trade_ids(conn)
    open_qty = _working_open_qty(conn)

    touched: set[int] = set()
    for p in propose(conn):
        if p.kind != "exit-allocation" or p.blocked:
            continue
        # Only exits that resolve entirely to already-committed Trades — bulk
        # confirm must not commit a new Trade to satisfy an allocation.
        if any((a.book, a.symbol, a.entry_date) not in ids for a in p.allocations):
            continue
        reason = reasons.get(p.source_ref or "") or p.proposed_reason
        _commit_exit(conn, p, list(p.allocations), reason, ids, open_qty, touched)
        result.exits_allocated += 1

    result.closed_trades = _close_filled(conn, open_qty, touched)
    conn.commit()
    return result


# ── Corrections: a fact once, a rule forever (SPEC §5.4) ─────────────────────
def correct_quantity(
    conn: sqlite3.Connection, source: str, source_ref: str, quantity: float
) -> None:
    """Correct one fill's quantity — a *fact* about one fill, not remembered.

    A wrong quantity is a fact, not a rule (SPEC §5.4): it is fixed here and never
    applied to a future statement. The correction lands as a new Fill *revision*
    on the same ``(source, source_ref)`` so the append-only ledger keeps the
    superseded value (ADR 0003); the higher revision then drives the derivation.
    The sign follows the existing side, so a magnitude is all the caller supplies.
    """
    prior = conn.execute(
        "SELECT * FROM fill WHERE source = ? AND source_ref = ? "
        "ORDER BY revision DESC LIMIT 1",
        (source, source_ref),
    ).fetchone()
    if prior is None:
        raise ValueError(f"no fill {source}/{source_ref} to correct")
    signed = abs(quantity) if prior["side"] == "BUY" else -abs(quantity)
    conn.execute(
        "INSERT INTO fill (source, source_ref, revision, book, symbol, side, "
        "quantity, price, commission, executed_at, order_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, source_ref, prior["revision"] + 1, prior["book"], prior["symbol"],
         prior["side"], signed, prior["price"], prior["commission"],
         prior["executed_at"], prior["order_id"]),
    )
    conn.commit()


def remember_symbol_rule(
    conn: sqlite3.Connection, source: str, from_symbol: str, to_symbol: str
) -> int:
    """Remember a wrong *symbol* as a parser rule, and repair what it already broke.

    A wrong symbol is a *rule*, not a fact (SPEC §5.4): stored once here, it is
    applied to every future statement before it reaches the queue
    (:func:`apply_symbol_rules`), so the same misparse is never re-confirmed. It
    also **repairs Trades already committed under the wrong symbol** — the Fills
    and Trades carrying it are rewritten to the real symbol. Returns the number of
    committed Trades repaired.
    """
    conn.execute(
        "INSERT OR IGNORE INTO parse_rule (source, from_symbol, to_symbol, created_at) "
        "VALUES (?, ?, ?, ?)",
        (source, from_symbol, to_symbol, _now_iso()),
    )
    # Repair the ledger: every Fill from this source under the wrong symbol, and
    # every Trade derived from them. This is a parser correction, not a broker
    # restatement — the symbol was never a fact about the market, so it is fixed
    # in place rather than revised (SPEC §5.4).
    conn.execute(
        "UPDATE fill SET symbol = ? WHERE source = ? AND symbol = ?",
        (to_symbol, source, from_symbol),
    )
    books = {r["book"] for r in conn.execute(
        "SELECT DISTINCT book FROM fill WHERE source = ?", (source,)
    )}
    repaired = 0
    for book in books:
        cur = conn.execute(
            "UPDATE trade SET symbol = ? WHERE symbol = ? AND book = ?",
            (to_symbol, from_symbol, book),
        )
        repaired += cur.rowcount
    conn.commit()
    return repaired


def apply_symbol_rules(conn: sqlite3.Connection, records: Sequence) -> List:
    """Rewrite each Fill's symbol per the remembered rules — *before* the queue.

    The other half of "a rule forever" (SPEC §5.4): the drop/import path runs
    parsed Fills through this so a remembered misparse is corrected silently, with
    no second confirmation. A record with no matching rule passes through
    unchanged. ``records`` are frozen Fill dataclasses; the corrected copies are
    returned, the originals untouched.
    """
    rules = {
        (r["source"], r["from_symbol"]): r["to_symbol"]
        for r in conn.execute("SELECT source, from_symbol, to_symbol FROM parse_rule")
    }
    if not rules:
        return list(records)
    out = []
    for f in records:
        to = rules.get((f.source, f.symbol))
        out.append(replace(f, symbol=to) if to else f)
    return out


# ── Hand entry: the same door, backdated (SPEC §5.1) ─────────────────────────
def hand_enter_trade(
    conn: sqlite3.Connection,
    book: str,
    symbol: str,
    entry_date: str,
    quantity: float,
    price: float,
) -> str:
    """Journal a backdated entry by hand — a BUY Fill through the one door (SPEC §5.1).

    Hand entry needs no second surface: it lands a manual BUY Fill and is then
    picked up by :func:`propose`/:func:`confirm` like any import. Entering a
    missing Trade this way is exactly what clears a parked orphan exit on the next
    confirm (RECHECK is inherent to re-deriving, SPEC §5.2). Returns the Fill's
    ``source_ref``.
    """
    source_ref = f"manual:{book}:{symbol}:{entry_date}:{quantity:g}@{price:g}"
    conn.execute(
        "INSERT OR IGNORE INTO fill (source, source_ref, revision, book, symbol, "
        "side, quantity, price, commission, executed_at, order_id) "
        "VALUES ('manual', ?, 1, ?, ?, 'BUY', ?, ?, 0, ?, ?)",
        (source_ref, book, symbol, abs(quantity), price, f"{entry_date}T00:00:00", source_ref),
    )
    conn.commit()
    return source_ref


# ── Quarantine: a failed document gate is a proposal, not an exception (§5.2) ─
def parse_document_or_quarantine(text: str) -> Tuple[List, Optional[Proposal]]:
    """Parse a dropped Stockbit TC, or turn its failure into a ``quarantine`` proposal.

    Every failure path is a queue item, never an exception (SPEC §5.2): if the
    fee-identity gate (or any parse) rejects the document, this returns no Fills
    and a ``quarantine`` proposal describing why, rather than raising. A clean
    document returns its Fills and no proposal.
    """
    try:
        return stockbit.parse_tc_text(text), None
    except stockbit.StockbitError as exc:
        return [], Proposal(
            kind="quarantine",
            book=stockbit.BOOK,
            symbol="",
            detail=str(exc),
            note=(
                "Nothing from this document commits. The fee identity or the parse "
                "failed — fix the parser against the new layout, then re-drop (SPEC §5.6)."
            ),
        )


# ── Drift: apply only a restated fact to a frozen snapshot (SPEC §5.3) ────────
def apply_drift(conn: sqlite3.Connection, trade_id: int) -> None:
    """Re-derive a frozen Trade's entry side from its Fills — the drift override.

    Drift on a frozen Trade is never applied by a plain confirm (SPEC §5.3): the
    snapshot is the record of what was believed at freeze, and moved *bar data*
    must never overwrite it. Only a **restated fact** justifies rewriting it, and
    only through this explicit call, which re-derives the entry side from the
    (now higher-revision) Fills. The superseded Fill revisions remain on record.
    """
    t = conn.execute(
        "SELECT book, symbol, entry_date FROM trade WHERE id = ?", (trade_id,)
    ).fetchone()
    if t is None:
        raise ValueError(f"no Trade with id {trade_id}")
    got = _derive_buy_cohorts(latest_fills(conn)).get(
        (t["book"], t["symbol"], t["entry_date"])
    )
    if got and got[0]:
        qty, avg = got
        conn.execute(
            "UPDATE trade SET entry_qty = ?, entry_avg_price = ? WHERE id = ?",
            (qty, avg, trade_id),
        )
        conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
