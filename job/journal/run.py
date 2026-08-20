"""The daily run (SPEC §13.1, §13.3, §13.6).

Every run is *"for each book, advance from ``last_processed_trading_date`` to
the present"* — not "process today". So a missed day is not an error, and a run
that catches up several days records that it did (§13.1). Advancing moves the
per-book cursor to the run's as-of date; then, **per book**, the enrichment
passes run — regime, counterfactual, freeze — each **gating on whether that
book's prior trading day has actually closed** (§13.3), read from the benchmark
bar cache rather than trusting the clock. **The job enriches but never commits a
Trade**, and it carries the nags (§11.4) as stated facts on the run record.

Errors are recorded as data, not raised (§13.6): a book that fails advancing is
written to its ``run_book`` row with ``status='error'``, a pass that fails is
written to its ``run_pass`` row with ``status='error'``, and the run still exits
zero. Two book-scoped passes in one job (§13.3): ``books.BOOKS`` order is the
pass order, US then IDX.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from . import backup, books, counterfactual, nags, post_exit, regime


@dataclass
class BookOutcome:
    book: str
    status: str  # 'advanced' | 'no-op' | 'error'
    from_date: Optional[str]
    to_date: str
    days_advanced: int
    error: Optional[str] = None


@dataclass
class PassOutcome:
    book: str
    name: str    # 'regime' | 'counterfactual' | 'freeze'
    status: str  # 'ran' | 'gated' | 'error'
    detail: Optional[str] = None


@dataclass
class RunResult:
    run_id: int
    as_of_date: str
    status: str  # 'ok' | 'no-op' | 'error'
    started_at: str
    finished_at: str
    books: list[BookOutcome] = field(default_factory=list)
    passes: list[PassOutcome] = field(default_factory=list)
    nags: list["nags.Nag"] = field(default_factory=list)
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


# The book-scoped enrichment passes the daily run carries (SPEC §13.1). Named so
# the run record reads the same order every run. Each is a callable
# ``(conn, book, as_of) -> detail`` that enriches in place and never commits a
# Trade — the job "enriches but never commits" (§13.1).
def _pass_regime(conn, book: str, as_of: str) -> str:
    """Stamp the book's RegimeSnapshot for each cached benchmark trading day.

    Backfill-shaped (§13.1): every benchmark bar in the caught-up window gets a
    ``(book, date)`` snapshot, so a run that advances several days stamps the
    regime for each of them, not just today. The earliest bar has no prior close
    to stamp from and is skipped.
    """
    bars = _benchmark_bars(conn, book)
    store = regime.RegimeStore(conn)
    stamped = 0
    for b in bars:
        if b.date > as_of:
            continue
        snap = regime.compute_snapshot(book, b.date, bars)
        if snap is None:
            continue
        store.upsert(snap)
        stamped += 1
    return f"{stamped} snapshot(s) stamped"


def _pass_counterfactual(conn, book: str, as_of: str) -> str:
    """Score every closed Trade on the book and persist the deltas (§10.9)."""
    store = counterfactual.CounterfactualStore(conn)
    results = counterfactual.compute_book(conn, book)
    for tc in results:
        store.upsert(tc.trade_id, tc)
    return f"{len(results)} closed Trade(s) scored"


def _pass_freeze(conn, book: str, as_of: str) -> str:
    """Settle any closed Trade on the book whose post-exit window has landed.

    ``freeze_sweep`` is store-wide; scoping the reported count to the book keeps
    the per-book run record honest without a second sweep.
    """
    froze = post_exit.freeze_sweep(conn)
    if not froze:
        return "0 Trade(s) frozen"
    placeholders = ",".join("?" for _ in froze)
    in_book = conn.execute(
        f"SELECT COUNT(*) c FROM trade WHERE book = ? AND id IN ({placeholders})",
        (book, *froze),
    ).fetchone()["c"]
    return f"{in_book} Trade(s) frozen"


_PASSES = (
    ("regime", _pass_regime),
    ("counterfactual", _pass_counterfactual),
    ("freeze", _pass_freeze),
)


def _benchmark_bars(conn, book: str):
    from .bars import Bar

    rows = conn.execute(
        "SELECT date, open, high, low, close, volume, dividend FROM bar "
        "WHERE book = ? AND symbol = ? ORDER BY date",
        (book, books.BENCHMARKS[book]),
    ).fetchall()
    return [
        Bar(date=r["date"], open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r["volume"], dividend=r["dividend"])
        for r in rows
    ]


def _prior_close_landed(conn, book: str, as_of: str) -> bool:
    """Has the book's prior trading day actually closed (SPEC §13.3)?

    Gates on data, not the clock: the book's benchmark must have a cached bar
    *strictly before* ``as_of``. Absent it — a holiday, a slow feed, a fresh
    machine — the prior close is not in yet, so the passes wait for a later run
    rather than enriching against a stale or empty series.
    """
    row = conn.execute(
        "SELECT 1 FROM bar WHERE book = ? AND symbol = ? AND date < ? LIMIT 1",
        (book, books.BENCHMARKS[book], as_of),
    ).fetchone()
    return row is not None


def _run_book_passes(conn, book: str, as_of: str) -> list[PassOutcome]:
    """Run (or gate) every enrichment pass for one book on this run."""
    if not _prior_close_landed(conn, book, as_of):
        return [
            PassOutcome(book, name, "gated", "prior trading day not yet closed")
            for name, _ in _PASSES
        ]
    outcomes: list[PassOutcome] = []
    for name, fn in _PASSES:
        try:
            detail = fn(conn, book, as_of)
            outcomes.append(PassOutcome(book, name, "ran", detail))
        except Exception as exc:  # one pass failing must not sink the run (§13.6)
            outcomes.append(PassOutcome(book, name, "error", str(exc)))
    return outcomes


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
    passes: list[PassOutcome] = []
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

        # The two book-scoped enrichment passes, each gating on that book's prior
        # trading day having closed (§13.3). A book that errored advancing is not
        # enriched — there is no fresh frontier to enrich against.
        book_passes = (
            _run_book_passes(conn, book, as_of)
            if outcome.status != "error"
            else []
        )
        for p in book_passes:
            conn.execute(
                "INSERT INTO run_pass (run_id, book, name, status, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, p.book, p.name, p.status, p.detail),
            )
        passes.extend(book_passes)

    # The nags ride the same run (§11.4): stated facts, recorded then read off the
    # banner on next open. Gathered once over the enriched store, not per book.
    run_nags = nags.gather(conn, as_of)
    for n in run_nags:
        conn.execute(
            "INSERT INTO run_nag (run_id, book, kind, detail) VALUES (?, ?, ?, ?)",
            (run_id, n.book, n.kind, n.detail),
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
        passes=passes,
        nags=run_nags,
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
    db_path = backup.db_path_of(conn)
    try:
        result.snapshot = backup.snapshot_database(
            conn,
            snapshots_dir=backup.snapshots_dir_for(db_path),
            timestamp=backup.snapshot_timestamp(datetime.now(timezone.utc)),
            offsite_dir=backup.offsite_dir(),
        )
    except Exception as exc:  # durability must not sink an already-committed run
        result.snapshot_error = str(exc)
