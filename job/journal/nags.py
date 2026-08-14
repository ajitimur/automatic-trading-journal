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
from typing import List

from . import books


@dataclass
class Nag:
    book: str
    kind: str   # 'missing_stop' | 'missing_setup' | 'idx_equity' | 'idx_intake'
    detail: str


def gather(conn) -> List[Nag]:
    """Collect every stated fact the current store warrants, book order (§13.3)."""
    out: List[Nag] = []
    for book in books.BOOKS:
        out.extend(_missing_field(conn, book, "stop", "missing_stop"))
        out.extend(_missing_field(conn, book, "setup", "missing_setup"))
    out.extend(_idx_equity(conn))
    out.extend(_idx_intake(conn))
    return out


def _missing_field(conn, book: str, column: str, kind: str) -> List[Nag]:
    # Only un-frozen Trades can still have the hole filled (§3.5); a frozen Trade
    # is settled, so nagging about it would be a permanent false alarm.
    count = conn.execute(
        f"SELECT COUNT(*) c FROM trade "
        f"WHERE book = ? AND frozen = 0 AND {column} IS NULL",
        (book,),
    ).fetchone()["c"]
    if count == 0:
        return []
    noun = "stop" if column == "stop" else "setup"
    return [Nag(book, kind, f"{book}: {count} Trade(s) without a {noun}")]


def _idx_equity(conn) -> List[Nag]:
    # IDX Risk % has no denominator without an Equity Snapshot; the last one's
    # date is the fact (§9). US equity comes from the broker automatically, so
    # only IDX — the hand-typed side — is nagged (§11.4).
    row = conn.execute(
        "SELECT MAX(date) d FROM equity_snapshot WHERE book = ?",
        (books.IDX,),
    ).fetchone()
    last = row["d"] if row else None
    detail = (
        f"IDX equity: last snapshot {last}"
        if last
        else "IDX equity: no snapshot recorded"
    )
    return [Nag(books.IDX, "idx_equity", detail)]


def _idx_intake(conn) -> List[Nag]:
    # The IDX TC is hand-dropped (§13.2): a forgotten drop is invisible, so the
    # last drop's date is stated as a fact — "did I miss a day" (§11.4).
    row = conn.execute(
        "SELECT MAX(fetched_at) f FROM raw_document WHERE book = ?",
        (books.IDX,),
    ).fetchone()
    last = row["f"] if row else None
    detail = (
        f"IDX intake: last drop {last}"
        if last
        else "IDX intake: no drop recorded"
    )
    return [Nag(books.IDX, "idx_intake", detail)]
