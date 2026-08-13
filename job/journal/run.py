"""The daily run (SPEC §13.1, §13.6).

Every run is *"for each book, advance from ``last_processed_trading_date`` to
the present"* — not "process today". So a missed day is not an error, and a run
that catches up several days records that it did (§13.1). The skeleton has no
bars and no domain logic, so "advancing" is only moving the per-book cursor to
the run's as-of date and writing the run record. The trading-day calendar and
the actual work land with the bar-cache ticket (#24).

Errors are recorded as data, not raised (§13.6): a book that fails is written
to its ``run_book`` row with ``status='error'`` and the run still exits zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from . import backup, books


@dataclass
class BookOutcome:
    book: str
    status: str  # 'advanced' | 'no-op' | 'error'
    from_date: Optional[str]
    to_date: str
    days_advanced: int
    error: Optional[str] = None


@dataclass
class RunResult:
    run_id: int
    as_of_date: str
    status: str  # 'ok' | 'no-op' | 'error'
    started_at: str
    finished_at: str
    books: list[BookOutcome] = field(default_factory=list)
    # The durability snapshot taken at the tail of a successful run (SPEC §13.5,
    # #39). ``snapshot`` is the SnapshotResult when one was written; a
    # best-effort failure records ``snapshot_error`` instead and never sinks the
    # run (§13.6 — durability is a convenience over an already-committed run).
    snapshot: Optional["backup.SnapshotResult"] = None
    snapshot_error: Optional[str] = None

    @property
    def is_noop(self) -> bool:
        return self.status == "no-op"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _calendar_days(start: str, end: str) -> int:
    """Placeholder day count between two ISO dates.

    Counts calendar days. The real *trading*-day count lands with the trading
    calendar (#24); until then ``from_date``/``to_date`` carry the precise
    fact and this is a convenience counter only.
    """
    delta = date.fromisoformat(end) - date.fromisoformat(start)
    return max(delta.days, 0)


def _advance_book(conn, book: str, as_of: str) -> BookOutcome:
    """Advance one book's cursor to ``as_of`` and describe what happened."""
    row = conn.execute(
        "SELECT last_processed_trading_date FROM book_cursor WHERE book = ?",
        (book,),
    ).fetchone()
    cursor: Optional[str] = row["last_processed_trading_date"] if row else None

    # A book already at (or past) the as-of date is a no-op — the second run of
    # the day, or a run while the machine has not seen a new close.
    if cursor is not None and cursor >= as_of:
        return BookOutcome(book, "no-op", cursor, cursor, 0)

    baseline = cursor if cursor is not None else books.BACKDATING_FLOOR
    days = _calendar_days(baseline, as_of)
    status = "advanced" if days > 0 else "no-op"

    conn.execute(
        "INSERT INTO book_cursor (book, last_processed_trading_date) VALUES (?, ?) "
        "ON CONFLICT(book) DO UPDATE SET last_processed_trading_date = excluded.last_processed_trading_date",
        (book, as_of),
    )
    return BookOutcome(book, status, cursor, as_of, days)


def execute_run(conn, as_of: Optional[str] = None) -> RunResult:
    """Run the daily job against an open connection and return the result.

    ``as_of`` defaults to today (UTC date). It is injectable so tests and
    backfill can pin the frontier deterministically.
    """
    as_of = as_of or datetime.now(timezone.utc).date().isoformat()
    # Validate shape early — an unparseable as-of is a caller bug, not run data.
    date.fromisoformat(as_of)

    started_at = _now_iso()
    cur = conn.execute(
        "INSERT INTO run (started_at, as_of_date, status) VALUES (?, ?, ?)",
        (started_at, as_of, "ok"),
    )
    run_id = cur.lastrowid

    outcomes: list[BookOutcome] = []
    for book in books.BOOKS:
        try:
            outcome = _advance_book(conn, book, as_of)
        except Exception as exc:  # a book failing must not sink the whole run
            outcome = BookOutcome(book, "error", None, as_of, 0, error=str(exc))
        outcomes.append(outcome)
        conn.execute(
            "INSERT INTO run_book (run_id, book, status, from_date, to_date, "
            "days_advanced, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                outcome.book,
                outcome.status,
                outcome.from_date,
                outcome.to_date,
                outcome.days_advanced,
                outcome.error,
            ),
        )

    if any(o.status == "error" for o in outcomes):
        status = "error"
    elif all(o.status == "no-op" for o in outcomes):
        status = "no-op"
    else:
        status = "ok"

    finished_at = _now_iso()
    conn.execute(
        "UPDATE run SET finished_at = ?, status = ? WHERE id = ?",
        (finished_at, status, run_id),
    )
    conn.commit()

    result = RunResult(
        run_id=run_id,
        as_of_date=as_of,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        books=outcomes,
    )

    # Durability tail (SPEC §13.5): every *successful* run leaves a timestamped
    # VACUUM INTO snapshot under rolling retention, plus an off-machine copy when
    # one is configured. A run whose books errored is recorded but not
    # snapshotted — a failed pass has nothing new worth freezing off-site. The
    # snapshot is best-effort: it happens after the run record is committed, so a
    # durability failure is logged into the result, never raised.
    if status != "error":
        _snapshot_after_run(conn, result)

    return result


def _snapshot_after_run(conn, result: RunResult) -> None:
    db_path = backup._db_path_of(conn)
    try:
        result.snapshot = backup.snapshot_database(
            conn,
            snapshots_dir=backup.snapshots_dir_for(db_path),
            timestamp=backup.snapshot_timestamp(datetime.now(timezone.utc)),
            offsite_dir=backup.offsite_dir(),
        )
    except Exception as exc:  # durability must not sink an already-committed run
        result.snapshot_error = str(exc)
