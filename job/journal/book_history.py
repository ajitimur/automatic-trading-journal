"""Book history — read-time projections over a book's Trade sequence (SPEC §7.9,
ADR 0004).

``seq`` and ``book_drawdown_r_at_entry`` are **not stored fields**. They are
computed at read time from the current Trade set, never written to the Trade,
never part of the freeze snapshot, and a change in either **never fires drift**
(ADR 0004): their only inputs are the journal's own append-only records, so a
backdated Trade renumbers its successors' ``seq`` and moves its neighbours'
drawdown correctly and silently. Pinning defends against an untrusted upstream;
this class has none, so there is nothing to pin.

* **``seq``** — the 1-based ordinal of the Trade on its book by ``entry_date``
  ascending (ties by insertion ``id``). A fact about the book, not a date, so it
  applies to **every** Trade, open or closed.
* **``book_drawdown_r_at_entry``** — ``peak(cum_realized_r) − cum_realized_r``
  over the book's closed, stop-bearing Trades, the cumulative realized-R curve
  evaluated at the Trade's ``entry_date``. Built on the **closed-Trade R curve,
  never on ``EquitySnapshot``**: an equity level has no cash-flow term, so a tax
  withdrawal would read as a drawdown never felt, and snapshots are permitted to
  be sparse; the R curve is dense at every close and immune to cash flows.

The curve steps up at each Trade's **final exit**, where its realized R lands, and
the drawdown is read at a Trade's ``entry_date`` over the R realized **strictly
before** that day — so a Trade never contributes to its own drawdown, and a
same-day round trip on the book is not counted as already-felt.

It inherits the stop-provenance caveat: a Trade with **no recorded stop** has no R
and is skipped by the curve, so the drawdown understates any stretch containing
one — the **excluded count per book ships** beside the values. A *reconstructed*
stop still carries R and stays on the curve (this is an R measure, not adherence;
cf. :func:`journal.counterfactual.r_aggregate` scope ``'r'``). Below
:data:`MIN_CLOSED_STOP_TRADES` closed stop-bearing Trades the drawdown is
:data:`INSUFFICIENT_HISTORY` — **not a drawdown of zero**.

**Books never combine** — two separately funded pots keep separate high-water
marks, so :func:`project` runs one book at a time and never reads across.
"""

from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .counterfactual import realized_r

# The marker the drawdown carries when the book has too few closed stop-bearing
# Trades to state a high-water mark honestly — distinct from a drawdown of zero.
INSUFFICIENT_HISTORY = "insufficient_history"

# The closed stop-bearing Trade count a book needs before the drawdown is a
# number rather than :data:`INSUFFICIENT_HISTORY` (SPEC §7.9).
MIN_CLOSED_STOP_TRADES = 20


@dataclass(frozen=True)
class BookHistoryRow:
    """One Trade's read-time book-history projection.

    ``book_drawdown_r_at_entry`` is ``None`` exactly when ``drawdown_marker`` is
    :data:`INSUFFICIENT_HISTORY`; the two never disagree.
    """

    trade_id: int
    book: str
    symbol: str
    entry_date: str
    status: str
    seq: int
    book_drawdown_r_at_entry: Optional[float]
    drawdown_marker: Optional[str]


@dataclass(frozen=True)
class BookHistory:
    """A book's whole projection: one row per Trade plus the excluded count.

    ``rows`` is ordered by ``seq``. ``closed_with_stop`` is the number of closed
    stop-bearing Trades on the curve; ``excluded_no_stop`` the closed no-stop
    Trades the curve dropped, shipped so a reader never mistakes a short curve for
    a shallow one.
    """

    book: str
    rows: Tuple[BookHistoryRow, ...]
    closed_with_stop: int
    excluded_no_stop: int

    @property
    def sufficient_history(self) -> bool:
        return self.closed_with_stop >= MIN_CLOSED_STOP_TRADES


def _exit_aggregates(
    conn: sqlite3.Connection, book: str
) -> Dict[int, Tuple[str, Optional[float]]]:
    """Per closed Trade: its final exit date and quantity-weighted exit price.

    The final exit is where the Trade's realized R lands on the curve; the
    weighted price feeds :func:`~journal.counterfactual.realized_r`.
    """
    rows = conn.execute(
        "SELECT te.trade_id AS trade_id, MAX(te.exit_date) AS final_exit_date, "
        "SUM(te.price * te.quantity) AS notional, SUM(te.quantity) AS qty "
        "FROM trade_exit te JOIN trade t ON t.id = te.trade_id "
        "WHERE t.book = ? GROUP BY te.trade_id",
        (book,),
    ).fetchall()
    out: Dict[int, Tuple[str, Optional[float]]] = {}
    for r in rows:
        qty = r["qty"]
        avg = r["notional"] / qty if qty else None
        out[r["trade_id"]] = (r["final_exit_date"], avg)
    return out


def project(conn: sqlite3.Connection, book: str) -> BookHistory:
    """Compute ``seq`` and ``book_drawdown_r_at_entry`` for every Trade on ``book``.

    Read-time only: reads the current Trade set and writes nothing, so a backdated
    Trade is reflected on the next call and drift never fires (ADR 0004).
    """
    trades = conn.execute(
        "SELECT id, symbol, entry_date, entry_avg_price, status, stop "
        "FROM trade WHERE book = ? ORDER BY entry_date, id",
        (book,),
    ).fetchall()
    exits = _exit_aggregates(conn, book)

    # The closed stop-bearing R curve, ordered by the date each R was realized.
    curve: List[Tuple[str, int, float]] = []
    excluded_no_stop = 0
    for t in trades:
        if t["status"] != "closed":
            continue
        if t["stop"] is None:
            excluded_no_stop += 1
            continue
        final_exit_date, exit_avg = exits.get(t["id"], (None, None))
        r = realized_r(t["entry_avg_price"], exit_avg, t["stop"])
        if r is None or final_exit_date is None:
            # A closed stop-bearing Trade with no usable exit price cannot enter
            # the curve; it is not a no-stop exclusion, so it drops silently.
            continue
        curve.append((final_exit_date, t["id"], r))
    curve.sort(key=lambda c: (c[0], c[1]))

    # Prefix cumulative R and its running high-water mark. Index k is the state
    # after the first k realized exits; k == 0 is the flat empty book.
    exit_dates = [c[0] for c in curve]
    cum = [0.0]
    peak = [0.0]
    for _, _, r in curve:
        cum.append(cum[-1] + r)
        peak.append(max(peak[-1], cum[-1]))

    sufficient = len(curve) >= MIN_CLOSED_STOP_TRADES
    rows: List[BookHistoryRow] = []
    for seq, t in enumerate(trades, start=1):
        if sufficient:
            # R realized strictly before the entry day (bisect_left on the sorted
            # exit dates counts exactly those), so a Trade never drawdowns itself.
            k = bisect.bisect_left(exit_dates, t["entry_date"])
            drawdown: Optional[float] = peak[k] - cum[k]
            marker: Optional[str] = None
        else:
            drawdown, marker = None, INSUFFICIENT_HISTORY
        rows.append(BookHistoryRow(
            trade_id=t["id"], book=book, symbol=t["symbol"],
            entry_date=t["entry_date"], status=t["status"], seq=seq,
            book_drawdown_r_at_entry=drawdown, drawdown_marker=marker,
        ))

    return BookHistory(
        book=book, rows=tuple(rows),
        closed_with_stop=len(curve), excluded_no_stop=excluded_no_stop,
    )
