"""The daily job's nags — stated facts, never alarms (SPEC §11.4, §13.1, §13.6).

The job "carries the nags" alongside enrichment: **missing stop, missing setup,
missing IDX equity, last IDX drop**. Each is phrased as a plain fact, because a
genuine no-trade stretch is normal for a swing trader and a crying-wolf warning
is ignored within a month (§11.4). They are recorded on the run and read off the
banner on next open — there is no push channel (§13.6).

A missing stop or setup is only a fact *before freeze*: once the freeze fuse
locks the hand-entered fields (§3.5) the hole can no longer be filled, so a frozen
Trade is not nagged about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from . import bars, books


@dataclass
class Nag:
    book: str
    kind: str   # 'missing_stop' | 'missing_setup' | 'idx_equity' | 'idx_intake'
    detail: str


def gather(conn, as_of: Optional[str] = None) -> List[Nag]:
    """Collect every stated fact the current store warrants, book order (§13.3).

    ``as_of`` is the date the facts are stated on, defaulting to today. It is a
    parameter because the elapsed counts below are measured against it, and a
    backfilled run must state what was true on the day it is replaying.
    """
    as_of = as_of or date.today().isoformat()
    out: List[Nag] = []
    for book in books.BOOKS:
        out.extend(_missing_field(conn, book, "stop", "missing_stop"))
        out.extend(_missing_field(conn, book, "setup", "missing_setup"))
    out.extend(_idx_equity(conn, as_of))
    out.extend(_idx_intake(conn, as_of))
    return out


def _elapsed(conn, book: str, since: str, as_of: str) -> str:
    """`` (7 trading days ago)``, or empty when the calendar cannot be counted.

    The elapsed count is what makes a date glanceable — §11.4's facts are read
    at a weekly cadence, and "11 Aug" asks the reader to know the book's own
    calendar before it means anything. Silent rather than approximate when the
    benchmark's bars are missing: an invented count is worse than a bare date.
    """
    days = bars.book_trading_days_between(conn, book, after=since, through=as_of)
    if days is None:
        return ""
    return f" ({days} trading day{'' if days == 1 else 's'} ago)"


def _missing_field(conn, book: str, column: str, kind: str) -> List[Nag]:
    # Only un-frozen Trades can still have the hole filled (§3.5); a frozen Trade
    # is settled, so nagging about it would be a permanent false alarm.
    #
    # A declined stop is likewise not a hole to chase: confirm asked, the trader
    # answered, and the cost was accepted on the record (ADR 0010). Nagging about
    # an answered question is how a banner teaches you to stop reading it.
    extra = " AND stop_declined = 0" if column == "stop" else ""
    count = conn.execute(
        f"SELECT COUNT(*) c FROM trade "
        f"WHERE book = ? AND frozen = 0 AND {column} IS NULL{extra}",
        (book,),
    ).fetchone()["c"]
    if count == 0:
        return []
    return [Nag(book, kind, f"{book}: {count} Trade(s) without a {column}")]


def _idx_equity(conn, as_of: str) -> List[Nag]:
    # IDX Risk % has no denominator without an Equity Snapshot; the last one's
    # date is the fact (§9). US equity comes from the broker automatically, so
    # only IDX — the hand-typed side — is nagged (§11.4).
    row = conn.execute(
        "SELECT MAX(date) d FROM equity_snapshot WHERE book = ?",
        (books.IDX,),
    ).fetchone()
    last = row["d"] if row else None
    detail = (
        f"IDX equity: last snapshot {last}{_elapsed(conn, books.IDX, last, as_of)}"
        if last
        else "IDX equity: no snapshot recorded"
    )
    return [Nag(books.IDX, "idx_equity", detail)]


def _idx_intake(conn, as_of: str) -> List[Nag]:
    # The IDX TC is hand-dropped (§13.2): a forgotten drop is invisible, so the
    # last drop's date is stated as a fact — "did I miss a day" (§11.4).
    #
    # Scoped to the TC kind, not to every IDX document. The question is when the
    # *trade* intake last ran; a hand-typed equity snapshot is a different fact
    # with its own nag, and letting it answer this one would report a healthy
    # intake on a book that has not seen a TC in weeks.
    row = conn.execute(
        "SELECT MAX(fetched_at) f FROM raw_document WHERE book = ? AND kind = ?",
        (books.IDX, "stockbit-tc"),
    ).fetchone()
    last = row["f"] if row else None
    # ``fetched_at`` carries a full timestamp; the banner states a day (§11.4).
    detail = (
        f"IDX intake: last drop {last[:10]}{_elapsed(conn, books.IDX, last[:10], as_of)}"
        if last
        else "IDX intake: no drop recorded"
    )
    return [Nag(books.IDX, "idx_intake", detail)]
