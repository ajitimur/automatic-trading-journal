"""Fill the bar cache for a book — the pass that feeds every other pass (§4.4).

Everything downstream reads the ``bar`` table directly: the regime stamp needs
the book's benchmark series, the counterfactual engine needs each traded
symbol's series, the post-exit window needs 20 trading days past the final exit,
and enrichment needs 200 trading days *before* an entry. None of them fetch —
they read a cache the daily job fills, and until this module existed nothing
filled it, so every one of them reported ``gated`` forever.

**What to fetch is derived from the ledger, never configured.** The symbols are
whichever ones the book actually traded, and each window starts a lookback
before that symbol's earliest entry, so a backdated Trade widens the window on
the next run rather than needing a backfill command (§13.1).

**Neither edge of the window is a hard calendar requirement, and for different
reasons.** The span check exists to catch a reused ticker or a genuinely
truncated series, and it is right to demand manual repair for those (§4.4). But
two ordinary situations trip it:

* *The series begins after the requested start.* The security listed later than
  our lookback. Failing it would leave the symbol with **no** bars at all rather
  than the shorter series it genuinely has.
* *The series ends before ``as_of``.* Today's bar does not exist yet — the job
  runs before any close (§13.3), and a calendar date is not a trading day
  (ADR 0005), so on a Monday the newest bar is Friday's. Requiring a bar dated
  today would fail **every** symbol, **every** run.

``bar_fetch`` records ``covered_start`` and ``covered_end`` before ``ensure``
raises, so both are told apart from the recorded fact rather than guessed at. A
series whose end is within :data:`TRAILING_TOLERANCE_DAYS` of ``as_of`` is
current, and is re-requested over the span it actually has; one that stops
further back is stale or delisted and stays an error against that symbol.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional, Sequence, Tuple

from . import books
from .bars import BarCache, SpanCheckError

# Calendar-day lookbacks, sized from the widest window each series feeds and
# converted at roughly 1.45 calendar days per trading day, with slack. They are
# deliberately generous: over-fetching costs one cached request, while
# under-fetching silently nulls an MA the whole grade depends on.
#
# Benchmark: MA50 plus a 5-day slope lookback = 55 trading days (regime.py).
BENCHMARK_LOOKBACK_DAYS = 120
# Symbol: MA200 plus the 20-day ADR window = 220 trading days (enrichment.py).
SYMBOL_LOOKBACK_DAYS = 340

# How far behind ``as_of`` a series may end and still count as current. A long
# weekend plus a public holiday is the case to survive; the IDX calendar's
# multi-day closures are the widest of them. Past this a series is stale or the
# security is gone, which is the delisting the span check should still catch.
TRAILING_TOLERANCE_DAYS = 7


@dataclass
class SymbolOutcome:
    """What happened to one symbol's series on this run."""

    symbol: str
    status: str  # 'fetched' | 'cached' | 'short' | 'error'
    detail: str


@dataclass
class SyncResult:
    """The book's bar sync, in the shape the run record prints."""

    book: str
    outcomes: List[SymbolOutcome] = field(default_factory=list)

    @property
    def errors(self) -> List[SymbolOutcome]:
        return [o for o in self.outcomes if o.status == "error"]

    def summary(self) -> str:
        counts = {}
        for o in self.outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        parts = [f"{n} {status}" for status, n in sorted(counts.items())]
        return ", ".join(parts) if parts else "nothing to fetch"


def _minus_days(day: str, days: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=days)).isoformat()


def planned_windows(
    conn: sqlite3.Connection, book: str, as_of: str
) -> List[Tuple[str, str, str]]:
    """``(symbol, start, end)`` for every series this book needs, benchmark first.

    The benchmark is always present — the run's own gate gates on it (§13.3), so
    a book with no Trades yet still fetches it and becomes ungated. Traded
    symbols follow, each from a lookback before its earliest entry.
    """
    windows: List[Tuple[str, str, str]] = [
        (
            books.BENCHMARKS[book],
            _minus_days(books.BACKDATING_FLOOR, BENCHMARK_LOOKBACK_DAYS),
            as_of,
        )
    ]
    rows = conn.execute(
        "SELECT symbol, MIN(entry_date) AS first_entry FROM trade "
        "WHERE book = ? GROUP BY symbol ORDER BY symbol",
        (book,),
    ).fetchall()
    for row in rows:
        windows.append(
            (row["symbol"], _minus_days(row["first_entry"], SYMBOL_LOOKBACK_DAYS), as_of)
        )
    return windows


def _actual_span(
    conn: sqlite3.Connection, book: str, symbol: str, as_of: str
) -> Optional[Tuple[str, str]]:
    """The span the series really has, when it is usable despite the span check.

    Reads the fetch record ``ensure`` committed before raising. Returns ``None``
    when the series is genuinely unusable — no rows at all, or an end further
    behind ``as_of`` than a closed market explains — because those are the
    corruption and delisting cases the span check is for.
    """
    row = conn.execute(
        "SELECT covered_start, covered_end FROM bar_fetch "
        "WHERE book = ? AND symbol = ? ORDER BY id DESC LIMIT 1",
        (book, symbol),
    ).fetchone()
    if row is None or row["covered_start"] is None or row["covered_end"] is None:
        return None
    if row["covered_end"] < _minus_days(as_of, TRAILING_TOLERANCE_DAYS):
        return None
    return row["covered_start"], row["covered_end"]


def sync_book(
    conn: sqlite3.Connection, book: str, as_of: str, cache: BarCache
) -> SyncResult:
    """Ensure every series this book needs is cached through ``as_of``.

    One symbol's failure never stops the others — the same rule the confirm
    queue applies to a parked item (§5). Failures are collected and reported
    against the symbol rather than raised, so a delisted ticker cannot stall the
    nightly run for every other name in the book.
    """
    result = SyncResult(book=book)
    for symbol, start, end in planned_windows(conn, book, as_of):
        try:
            check = cache.ensure(book, symbol, start, end)
            status = "cached" if check.rows_fetched == 0 else "fetched"
            result.outcomes.append(SymbolOutcome(symbol, status, check.detail))
            continue
        except SpanCheckError as exc:
            actual = _actual_span(conn, book, symbol, as_of)
            if actual is None:
                result.outcomes.append(SymbolOutcome(symbol, "error", str(exc)))
                continue
        actual_start, actual_end = actual
        try:
            check = cache.ensure(book, symbol, max(actual_start, start), actual_end)
        except SpanCheckError as exc:
            result.outcomes.append(SymbolOutcome(symbol, "error", str(exc)))
            continue
        if actual_start > start:
            result.outcomes.append(
                SymbolOutcome(
                    symbol,
                    "short",
                    f"series begins {actual_start}, after the requested {start} — "
                    f"cached from its first bar; windows reaching further back "
                    f"than that will be null",
                )
            )
        else:
            # The only shortfall was today's bar, which does not exist yet.
            result.outcomes.append(
                SymbolOutcome(symbol, "fetched", f"{check.detail} (through {actual_end})")
            )
    return result
