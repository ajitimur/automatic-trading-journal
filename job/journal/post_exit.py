"""Section F — the post-exit window, the freeze trigger, and drift (SPEC
§3.5/§3.6/§7.5, issue #34).

Three things that are really one thing live here, because the spec makes them one:

**F. The post-exit counterfactual window.** ``20`` trading days beginning the day
*after* the final exit date, baselined on ``C_x`` — the final exit day's close,
which is execution-noise-free and comparable across every Trade (SPEC §7.5).
``exit_avg_price`` sits beside ``C_x`` so the gap between *what was got* and *what
the day was worth* stays derivable rather than silently baked in. The fields are
**null until the window completes** — :func:`compute_post_exit` returns ``None``
until ``20`` trading days are on record after the exit.

**The freeze trigger.** Completing that window **is** the freeze; there is no
second clock (SPEC §7.5). :func:`settle` computes the window and, when it lands,
snapshots it and calls :func:`journal.stops.freeze`. The **fuse counts traded
days** (SPEC §3.6): it counts *bars*, and a suspension arrives as missing bars
(zero-volume rows are filtered at the cache boundary, ADR 0005), so a suspended
symbol stretches the fuse in calendar time rather than burning through it.

**A ``written_off`` Exit freezes immediately** (SPEC §3.5). The window is
meaningless for a symbol with no further trading days, so its post-exit fields and
all variants record ``not_applicable`` — a marker distinct from a null-for-history
and from a real value. The price is hand-entered (a delisting can pay a residual),
so ``exit_avg_price`` still carries through.

**Drift carries a cause, and the cause decides what may be done** (SPEC §3.6). A
snapshot at freeze stays recomputable forever, so a later disagreement is
detectable:

* **Broker restatement** — the fact under the snapshot was wrong, so it **may be
  applied**. :meth:`PostExitStore.apply_restatement` writes the corrected snapshot
  as a new append-only revision, keeping the superseded one beside it — the same
  shape the Fill ledger uses.
* **Revised bar series** — **acknowledge only, never applied**.
  :func:`detect_bar_drift` surfaces it and there is deliberately no write path:
  nothing was wrong at freeze, and overwriting would destroy the record of what
  was believed then, which is the entire point of freezing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from .bars import Bar
from . import stops

__all__ = [
    "WINDOW",
    "NOT_APPLICABLE",
    "BROKER_RESTATEMENT",
    "REVISED_BAR_SERIES",
    "PostExit",
    "PostExitStore",
    "Drift",
    "compute_post_exit",
    "written_off_post_exit",
    "is_window_complete",
    "remaining_fuse",
    "detect_bar_drift",
    "settle",
    "freeze_sweep",
]

# The window is 20 *trading* days after the final exit (SPEC §7.5).
WINDOW = 20

# Written-off marker — distinct from a null (history too short) and from a value.
NOT_APPLICABLE = "not_applicable"

# The exit reason that ends a Trade with no further trading days (SPEC §3.3).
WRITTEN_OFF = "written_off"

# Drift causes (SPEC §3.6). The cause decides what may be done.
BROKER_RESTATEMENT = "broker_restatement"
REVISED_BAR_SERIES = "revised_bar_series"

# The derived fields the window snapshots — the set a marker can be asked about.
_FWD_FIELDS = (
    "fwd_return_20d", "fwd_close_20d", "fwd_high", "fwd_high_date",
    "fwd_low", "fwd_low_date",
)


@dataclass(frozen=True)
class PostExit:
    """The post-exit counterfactual for one Trade (SPEC §7.5).

    Baselined on ``cx`` (``C_x``, the final exit day's close), with
    ``exit_avg_price`` stored beside it so the gap stays derivable. Numeric fields
    are ``None`` when the window is incomplete (this dataclass is only built once
    it completes) or, for a ``written_off`` Trade, ``None`` with
    ``not_applicable`` set — :meth:`marker` tells the two apart.
    """

    final_exit_date: str
    cx: Optional[float]                # C_x, the baseline close
    exit_avg_price: Optional[float]    # sits beside C_x; hand-entered if written off
    fwd_return_20d: Optional[float]    # (C_20 / C_x − 1) × 100
    fwd_close_20d: Optional[float]     # close on the 20th trading day (C_20)
    fwd_high: Optional[float]          # maximum High in the window
    fwd_high_date: Optional[str]
    fwd_low: Optional[float]           # minimum Low in the window
    fwd_low_date: Optional[str]
    not_applicable: bool = False       # written-off: the window is meaningless
    cause: Optional[str] = None        # None for the freeze snapshot; a cause for a restatement

    def marker(self, field: str) -> Optional[str]:
        """:data:`NOT_APPLICABLE` for a written-off field, else ``None``.

        The written-off marker reaches every post-exit field and every
        counterfactual variant (SPEC §3.5); a plain null (history too short) has
        no marker.
        """
        if self.not_applicable and field in _FWD_FIELDS:
            return NOT_APPLICABLE
        return None


def compute_post_exit(
    bars: Sequence[Bar], final_exit_date: str, exit_avg_price: Optional[float]
) -> Optional[PostExit]:
    """The window over the 20 trading days after ``final_exit_date``, or ``None``.

    ``bars`` is the symbol's series, oldest first. The baseline ``C_x`` is the
    close of the last bar **at or before** ``final_exit_date`` (the final exit
    day's own close). The window is the next ``WINDOW`` bars — traded days, since
    zero-volume rows never reach the cache (ADR 0005). Returns ``None`` while the
    window is **incomplete** (fewer than ``WINDOW`` bars after the exit, or no bar
    to baseline on): the fields are null until the window completes, and that is
    the freeze trigger (SPEC §7.5). No second clock.
    """
    upto = [b for b in bars if b.date <= final_exit_date]
    after = [b for b in bars if b.date > final_exit_date]
    if not upto or len(after) < WINDOW:
        return None

    cx = upto[-1].close
    window = after[:WINDOW]
    c20 = window[-1].close

    # Earliest date wins a tie: *when* the extreme first arrived is the finding
    # (SPEC §7.5), and ``window`` is oldest-first so a strict comparison keeps it.
    hi = window[0]
    lo = window[0]
    for b in window[1:]:
        if b.high > hi.high:
            hi = b
        if b.low < lo.low:
            lo = b

    return PostExit(
        final_exit_date=final_exit_date,
        cx=cx,
        exit_avg_price=exit_avg_price,
        fwd_return_20d=(c20 / cx - 1) * 100 if cx else None,
        fwd_close_20d=c20,
        fwd_high=hi.high,
        fwd_high_date=hi.date,
        fwd_low=lo.low,
        fwd_low_date=lo.date,
    )


def written_off_post_exit(
    final_exit_date: str, exit_avg_price: Optional[float]
) -> PostExit:
    """The ``not_applicable`` post-exit for a ``written_off`` Trade (SPEC §3.5).

    The window is meaningless for a symbol with no further trading days, so every
    field records ``not_applicable`` rather than a value or a history-null. The
    ``exit_avg_price`` still carries through — a write-off is hand-entered because
    a delisting can pay a residual and is not reliably total.
    """
    return PostExit(
        final_exit_date=final_exit_date,
        cx=None,
        exit_avg_price=exit_avg_price,
        fwd_return_20d=None,
        fwd_close_20d=None,
        fwd_high=None,
        fwd_high_date=None,
        fwd_low=None,
        fwd_low_date=None,
        not_applicable=True,
    )


def is_window_complete(bars: Sequence[Bar], final_exit_date: str) -> bool:
    """True once ``WINDOW`` trading days sit after ``final_exit_date``."""
    return remaining_fuse(bars, final_exit_date) == 0


def remaining_fuse(bars: Sequence[Bar], final_exit_date: str) -> int:
    """Trading days left before the window completes — the review-banner fuse.

    Counts **bars** after the exit (traded days, SPEC §3.6), never calendar days:
    a suspension arrives as missing bars, so it stretches the fuse in calendar
    time rather than burning it. Clamped at zero once the window has landed.
    """
    after = sum(1 for b in bars if b.date > final_exit_date)
    return max(0, WINDOW - after)


# ---------------------------------------------------------------------------
# Drift — a fact from outside the journal moved (SPEC §3.6).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Drift:
    """A frozen snapshot disagreeing with the world (SPEC §3.6).

    ``cause`` decides what may be done: a :data:`BROKER_RESTATEMENT` is
    ``applicable`` (apply via :meth:`PostExitStore.apply_restatement`, keeping the
    superseded snapshot); a :data:`REVISED_BAR_SERIES` is **acknowledge only** —
    ``applicable`` is ``False`` and there is no write path for it at all.
    """

    cause: str
    applicable: bool
    note: str


def detect_bar_drift(
    stored: PostExit, bars: Sequence[Bar], final_exit_date: str
) -> Optional[Drift]:
    """Surface a **revised bar series** against a frozen snapshot (SPEC §3.6).

    Recomputes the window from the current ``bars`` and compares it to the
    snapshot taken at freeze. A disagreement is drift caused by a revised bar
    series — nothing was wrong at freeze, so it is **acknowledge only** and never
    applied. Returns ``None`` when the bars still agree, or when the snapshot is
    ``not_applicable`` (a written-off Trade has no window to revise).
    """
    if stored.not_applicable:
        return None
    recomputed = compute_post_exit(bars, final_exit_date, stored.exit_avg_price)
    if recomputed is None:
        return None
    if _same_window(stored, recomputed):
        return None
    return Drift(
        cause=REVISED_BAR_SERIES,
        applicable=False,
        note=(
            f"Bars in the post-exit window moved: fwd_return_20d "
            f"{stored.fwd_return_20d} → {recomputed.fwd_return_20d}. Nothing was "
            "wrong at freeze; acknowledge only, never overwrite (SPEC §3.6)."
        ),
    )


def _same_window(a: PostExit, b: PostExit, tol: float = 1e-9) -> bool:
    def near(x: Optional[float], y: Optional[float]) -> bool:
        if x is None or y is None:
            return x is y
        return abs(x - y) <= tol
    return (
        near(a.fwd_return_20d, b.fwd_return_20d)
        and near(a.fwd_close_20d, b.fwd_close_20d)
        and near(a.fwd_high, b.fwd_high)
        and a.fwd_high_date == b.fwd_high_date
        and near(a.fwd_low, b.fwd_low)
        and a.fwd_low_date == b.fwd_low_date
    )


# ---------------------------------------------------------------------------
# Storage — append-only revisions, so a superseded snapshot is kept.
# ---------------------------------------------------------------------------


_COLUMNS = (
    "trade_id", "revision", "final_exit_date", "cx", "exit_avg_price",
    "fwd_return_20d", "fwd_close_20d", "fwd_high", "fwd_high_date",
    "fwd_low", "fwd_low_date", "not_applicable", "cause", "created_at",
)


class PostExitStore:
    """Persist and read :class:`PostExit`, append-only per Trade (SPEC §3.6).

    The freeze snapshot is revision ``1``; a broker restatement adds revision
    ``2``, ``3`` … keeping every superseded snapshot beside the current one — the
    same append-only shape the Fill ledger uses. :meth:`get` returns the highest
    revision; :meth:`history` returns them all, oldest first.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _next_revision(self, trade_id: int) -> int:
        row = self.conn.execute(
            "SELECT MAX(revision) AS r FROM trade_post_exit WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        return (row["r"] or 0) + 1

    def _insert(self, trade_id: int, pe: PostExit, cause: Optional[str]) -> None:
        self.conn.execute(
            f"INSERT INTO trade_post_exit ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
            (
                trade_id, self._next_revision(trade_id), pe.final_exit_date, pe.cx,
                pe.exit_avg_price, pe.fwd_return_20d, pe.fwd_close_20d, pe.fwd_high,
                pe.fwd_high_date, pe.fwd_low, pe.fwd_low_date,
                1 if pe.not_applicable else 0, cause, _now_iso(),
            ),
        )
        self.conn.commit()

    def snapshot(self, trade_id: int, pe: PostExit) -> None:
        """Write the freeze snapshot (revision 1). Its ``cause`` is ``None``."""
        self._insert(trade_id, pe, None)

    def apply_restatement(self, trade_id: int, corrected: PostExit) -> None:
        """Write a corrected snapshot, keeping the superseded one (SPEC §3.6).

        A broker restated the fact under the snapshot, so it may be applied: this
        appends a new revision tagged :data:`BROKER_RESTATEMENT`. The earlier
        revisions stay on record — the record of what was believed at freeze is
        never destroyed.
        """
        self._insert(trade_id, corrected, BROKER_RESTATEMENT)

    def get(self, trade_id: int) -> Optional[PostExit]:
        row = self.conn.execute(
            "SELECT * FROM trade_post_exit WHERE trade_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (trade_id,),
        ).fetchone()
        return _row_to_post_exit(row)

    def history(self, trade_id: int) -> List[PostExit]:
        rows = self.conn.execute(
            "SELECT * FROM trade_post_exit WHERE trade_id = ? ORDER BY revision ASC",
            (trade_id,),
        ).fetchall()
        return [_row_to_post_exit(r) for r in rows]


def _row_to_post_exit(row) -> Optional[PostExit]:
    if row is None:
        return None
    return PostExit(
        final_exit_date=row["final_exit_date"],
        cx=row["cx"],
        exit_avg_price=row["exit_avg_price"],
        fwd_return_20d=row["fwd_return_20d"],
        fwd_close_20d=row["fwd_close_20d"],
        fwd_high=row["fwd_high"],
        fwd_high_date=row["fwd_high_date"],
        fwd_low=row["fwd_low"],
        fwd_low_date=row["fwd_low_date"],
        not_applicable=bool(row["not_applicable"]),
        cause=row["cause"],
    )


# ---------------------------------------------------------------------------
# Settle — compute the window and, when it lands, freeze (SPEC §3.5/§7.5).
# ---------------------------------------------------------------------------


def settle(
    conn: sqlite3.Connection,
    trade_id: int,
    bars: Sequence[Bar],
    store: Optional[PostExitStore] = None,
) -> bool:
    """Snapshot the post-exit window and freeze the Trade if it has landed.

    Returns ``True`` when the Trade froze on this call. A ``written_off`` Exit
    freezes immediately with a ``not_applicable`` snapshot (SPEC §3.5). Otherwise
    the window must be complete — 20 trading days after the final exit — before the
    snapshot is written and :func:`journal.stops.freeze` fires; an incomplete
    window leaves the fields null and returns ``False``. Idempotent on an
    already-frozen Trade (no second snapshot).
    """
    store = store or PostExitStore(conn)
    final = _final_exit(conn, trade_id)
    if final is None:
        return False
    final_exit_date, exit_avg_price, reason = final

    if _is_frozen(conn, trade_id):
        return False

    if reason == WRITTEN_OFF:
        store.snapshot(trade_id, written_off_post_exit(final_exit_date, exit_avg_price))
        stops.freeze(conn, trade_id)
        return True

    pe = compute_post_exit(bars, final_exit_date, exit_avg_price)
    if pe is None:
        return False
    store.snapshot(trade_id, pe)
    stops.freeze(conn, trade_id)
    return True


def freeze_sweep(conn: sqlite3.Connection) -> List[int]:
    """Settle every closed, not-yet-frozen Trade; return the ids that froze.

    The seam the daily job calls (#38): each closed Trade whose post-exit window
    has landed (or whose Exit was ``written_off``) freezes on this pass, the rest
    wait for a later run as their windows fill. Reads the symbol's bars straight
    from the cache — the freeze fuse counts the traded days already stored, so a
    suspended symbol simply has fewer bars and waits (SPEC §3.6). Idempotent: an
    already-frozen Trade is skipped, so re-running never double-snapshots.
    """
    store = PostExitStore(conn)
    rows = conn.execute(
        "SELECT id, book, symbol, entry_date FROM trade "
        "WHERE status = 'closed' AND frozen = 0"
    ).fetchall()
    froze: List[int] = []
    for r in rows:
        bars = _read_bars(conn, r["book"], r["symbol"], r["entry_date"])
        if settle(conn, r["id"], bars, store):
            froze.append(r["id"])
    return froze


def _read_bars(
    conn: sqlite3.Connection, book: str, symbol: str, start: str
) -> List[Bar]:
    """Cached trading-day bars for a symbol from ``start`` onward, oldest first."""
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume, dividend FROM bar "
        "WHERE book = ? AND symbol = ? AND date >= ? ORDER BY date",
        (book, symbol, start),
    ).fetchall()
    return [
        Bar(
            date=r["date"], open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r["volume"], dividend=r["dividend"],
        )
        for r in rows
    ]


def _is_frozen(conn: sqlite3.Connection, trade_id: int) -> bool:
    row = conn.execute(
        "SELECT frozen FROM trade WHERE id = ?", (trade_id,)
    ).fetchone()
    return bool(row and row["frozen"])


def _final_exit(conn: sqlite3.Connection, trade_id: int):
    """``(final_exit_date, exit_avg_price, reason)`` for a Trade, or ``None``.

    The final exit date is the latest ``trade_exit`` date; ``exit_avg_price`` is
    the quantity-weighted mean of *all* exit fills (SPEC §7.4, the same average
    exit geometry stores beside ``C_x``). ``reason`` is that of the final-dated
    Exit, so a ``written_off`` terminal Exit is recognised. ``None`` when the
    Trade has no Exit yet.
    """
    rows = conn.execute(
        "SELECT exit_date, quantity, price, reason FROM trade_exit "
        "WHERE trade_id = ?",
        (trade_id,),
    ).fetchall()
    if not rows:
        return None
    final_exit_date = max(r["exit_date"] for r in rows)
    qty = sum(r["quantity"] for r in rows)
    avg = sum(r["quantity"] * r["price"] for r in rows) / qty if qty else None
    # A write-off is terminal: if any exit is written_off treat the Trade as such.
    reasons = {r["reason"] for r in rows}
    if WRITTEN_OFF in reasons:
        reason: Optional[str] = WRITTEN_OFF
    else:
        final_reasons = [r["reason"] for r in rows if r["exit_date"] == final_exit_date]
        reason = final_reasons[-1] if final_reasons else None
    return final_exit_date, avg, reason


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
