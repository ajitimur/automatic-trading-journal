"""EquitySnapshot — the risk/exposure denominator (SPEC §9, issue #31).

`EquitySnapshot` has exactly one job: the denominator a position-sizer divides
by. It is **mark-to-market NAV, not deposited capital** (§9.1) — under deposited
capital a losing streak silently raises real risk while the display still reads
1%. It is not the book's equity curve and nothing here should grow toward making
it one.

The two books get **structurally different creation mechanisms**, and forcing a
single one was never on the table (§9.2):

* **IBKR — automatic; capture is the whole point.** A *second* Activity Flex
  Query carrying the NAV Summary in Base section (``EquitySummaryInBase``). Every
  ``reportDate`` row becomes a snapshot. The denominator is ``total`` — not
  ``cash`` (which goes negative on margin) and not a reconstructed ``cash+stock``
  (which drops the accrual residual). The reachable window is a rolling 365 days,
  so snapshots are **captured and persisted, never re-derived**, and the NAV XML
  joins the keep-forever raw tier (§13.5) — anything uncaptured is gone forever.

* **IDX — hand-typed, and deliberately no second parser.** Twelve numbers a year
  against a drifting PDF layout does not clear the prefer-derived bar. The
  components (``portfolio``, ``ledger_balance``, ``cash_investor``) are stored
  beside the total so the deferred ``Cash Investor`` question is a config change
  (:func:`idx_equity`) rather than a re-read of PDFs. The SoA PDF still reaches
  the raw tier so a parser stays addable later.

Both books **write straight through, never the confirm queue** (§9.7): a snapshot
has no cohort, no FIFO, no exit to allocate, so there is nothing to confirm.
"""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from . import db

# IBKR is the US book; IDX is hand-typed. Sources name the mechanism, not just
# the broker, so a row's origin is legible without a join.
BOOK_US = "US"
BOOK_IDX = "IDX"
SOURCE_IBKR = "ibkr"
SOURCE_IDX = "idx"

# The IDX denominator, expressed over the stored components so switching it is a
# one-constant config change (§9.3). Equity NAB is the printed, self-checking,
# and *smaller* candidate — it over-flags rather than under-flags, the safe
# direction for a discipline check (§9.2). The deferred alternative folds in
# Cash Investor; both stay derivable because the components are stored.
IDX_DENOMINATOR = "equity_nab"


class EquityError(Exception):
    """A NAV document that is a Flex error body, not a statement (§9.2, §4.1).

    Flex signals failure with HTTP 200 and an XML error body, so a reader that
    trusts the file would record "no equity" instead of failing loudly.
    """


@dataclass(frozen=True)
class EquitySnapshot:
    """One (book, date) denominator on the calendar axis (§9.3, §9.5)."""

    book: str
    date: str            # ISO 8601 calendar date
    equity: float        # the denominator: IBKR total, IDX Equity NAB
    provenance: str      # 'stated' | 'estimated'
    source: str          # 'ibkr' | 'idx'
    # Book-specific components (§9.3); the other book's are None.
    cash: Optional[float] = None            # US
    stock: Optional[float] = None           # US
    portfolio: Optional[float] = None       # IDX
    ledger_balance: Optional[float] = None  # IDX
    cash_investor: Optional[float] = None   # IDX


def _normalize_date(value: str) -> str:
    """Accept ``YYYYMMDD`` or ``YYYY-MM-DD`` and return ISO ``YYYY-MM-DD``.

    The NAV element is documented with a hyphenated ``reportDate`` while the
    Trades export uses compact ``YYYYMMDD``; normalise so the calendar axis is
    one shape regardless of which spelling a given account's Flex emits.
    """
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    return value


def parse_nav_flex(xml_text: str) -> list[EquitySnapshot]:
    """Parse a NAV Summary in Base Flex XML into one snapshot per report date.

    Every ``EquitySummaryByReportDateInBase`` row becomes a US snapshot with
    ``total`` as the denominator and ``cash``/``stock`` stored beside it. Raises
    :class:`EquityError` on a Flex error body rather than reading it as empty.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise EquityError(f"not well-formed NAV Flex XML: {exc}") from exc

    _reject_error_body(root)

    snaps = [
        EquitySnapshot(
            book=BOOK_US,
            date=_normalize_date(row.get("reportDate") or ""),
            equity=float(row.get("total") or 0),
            provenance="stated",
            source=SOURCE_IBKR,
            cash=float(row.get("cash") or 0),
            stock=float(row.get("stock") or 0),
        )
        for row in root.iter("EquitySummaryByReportDateInBase")
    ]
    return snaps


def _reject_error_body(root: ET.Element) -> None:
    status = root.findtext("Status")
    if status and status.strip().lower() == "fail":
        code = (root.findtext("ErrorCode") or "").strip()
        message = (root.findtext("ErrorMessage") or "").strip()
        raise EquityError(f"Flex error {code}: {message}".strip())


def idx_equity(components: Mapping[str, float], denominator: str = IDX_DENOMINATOR) -> float:
    """Compute the IDX denominator from its stored components (§9.3).

    ``equity_nab`` — the live default — is ``Portfolio + <ledger closing
    balance>`` (exact, per §9.2). ``portfolio_plus_cash`` is the deferred rival
    that folds in ``Cash Investor``. Storing the components is what makes the
    choice a config change here rather than a re-read of PDFs.
    """
    portfolio = components.get("portfolio") or 0.0
    ledger_balance = components.get("ledger_balance") or 0.0
    cash_investor = components.get("cash_investor") or 0.0
    if denominator == "equity_nab":
        return portfolio + ledger_balance
    if denominator == "portfolio_plus_cash":
        # Cash = Cash Investor + ledger closing balance (§9.2).
        return portfolio + cash_investor + ledger_balance
    raise ValueError(f"unknown IDX denominator {denominator!r}")


_INSERT_SNAPSHOT = """
INSERT OR REPLACE INTO equity_snapshot
    (book, date, equity, provenance, source, fetch_date, raw_ref,
     cash, stock, portfolio, ledger_balance, cash_investor)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _write_snapshot(
    conn: sqlite3.Connection, snap: EquitySnapshot, *, fetch_date: Optional[str], raw_ref: Optional[int]
) -> None:
    conn.execute(
        _INSERT_SNAPSHOT,
        (
            snap.book, snap.date, snap.equity, snap.provenance, snap.source,
            fetch_date, raw_ref,
            snap.cash, snap.stock, snap.portfolio, snap.ledger_balance,
            snap.cash_investor,
        ),
    )


def _store_raw_document(
    conn: sqlite3.Connection, *, book: str, kind: str, fetched_at: str, content: str
) -> int:
    """Persist a raw source document to the keep-forever tier, returning its id.

    Delegates to the shared writer so every intake path lands in one table the
    same way — the IDX drop path had its own arrangement and the intake nag went
    blind as a result (SPEC §11.4).
    """
    return db.record_raw_document(
        conn, book=book, kind=kind, fetched_at=fetched_at, content=content
    )


def import_nav_flex_text(
    conn: sqlite3.Connection, xml_text: str, *, fetch_date: str
) -> int:
    """Capture a NAV Flex statement: persist the raw XML, then write snapshots.

    The raw XML lands in the keep-forever tier *first* (§13.5) so that even a
    partial write leaves the source recoverable; every ``reportDate`` snapshot
    then writes straight through (§9.7), pointing back at that raw document.
    Returns the number of report-date rows captured. A re-fetch of an
    overlapping window is idempotent — the (book, date) key replaces in place.
    """
    snaps = parse_nav_flex(xml_text)
    raw_ref = _store_raw_document(
        conn, book=BOOK_US, kind="nav-flex-xml", fetched_at=fetch_date, content=xml_text
    )
    for snap in snaps:
        _write_snapshot(conn, snap, fetch_date=fetch_date, raw_ref=raw_ref)
    conn.commit()
    return len(snaps)


def record_idx_snapshot(
    conn: sqlite3.Connection,
    *,
    date: str,
    portfolio: float,
    ledger_balance: float,
    cash_investor: Optional[float] = None,
    provenance: str = "stated",
    fetch_date: Optional[str] = None,
) -> None:
    """Hand-enter one IDX snapshot straight through (§9.2, §9.7).

    ``equity`` is Equity NAB, derived from the components via :func:`idx_equity`;
    the components are stored so the denominator choice stays a config change.
    A plain write — no confirm step, since a snapshot has nothing to reconcile.
    """
    components = {
        "portfolio": portfolio,
        "ledger_balance": ledger_balance,
        "cash_investor": cash_investor,
    }
    snap = EquitySnapshot(
        book=BOOK_IDX,
        date=date,
        equity=idx_equity(components),
        provenance=provenance,
        source=SOURCE_IDX,
        portfolio=portfolio,
        ledger_balance=ledger_balance,
        cash_investor=cash_investor,
    )
    _write_snapshot(conn, snap, fetch_date=fetch_date, raw_ref=None)
    conn.commit()


def record_idx_series(
    conn: sqlite3.Connection, series: Iterable[Mapping[str, object]], *, fetch_date: Optional[str] = None
) -> int:
    """Enter a month-end IDX series in one sitting (§9.6).

    Backdating takes a month-end series, not a snapshot per Trade. Each entry is
    a mapping with ``date``, ``portfolio``, ``ledger_balance`` and optionally
    ``cash_investor``/``provenance``. Returns the number of rows written.
    """
    written = 0
    for entry in series:
        record_idx_snapshot(
            conn,
            date=str(entry["date"]),
            portfolio=float(entry["portfolio"]),  # type: ignore[arg-type]
            ledger_balance=float(entry["ledger_balance"]),  # type: ignore[arg-type]
            cash_investor=(
                float(entry["cash_investor"])  # type: ignore[arg-type]
                if entry.get("cash_investor") not in (None, "")
                else None
            ),
            provenance=str(entry.get("provenance") or "stated"),
            fetch_date=fetch_date,
        )
        written += 1
    return written


def read_snapshots(conn: sqlite3.Connection, book: str) -> Sequence[sqlite3.Row]:
    """All snapshots for a book, oldest first — the calendar-ordered series."""
    return conn.execute(
        "SELECT * FROM equity_snapshot WHERE book = ? ORDER BY date", (book,)
    ).fetchall()
