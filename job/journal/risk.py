"""Risk % and Exposure %: the staleness bound and provenance tiers (SPEC §9.3/§9.4, #32).

**One denominator asked two questions.** Both Risk % and Exposure % read the
*most recent* :class:`~journal.equity.EquitySnapshot` at or before a Trade's entry
date — one lookup rule, never a second denominator (§9.4). Everything that could
diverge between the two is stated here once and shared, because a divergence would
let a Trade show a live Exposure % against a denominator too stale for Risk % —
two different claims about the same account on the same day.

* **The lookup** is "most recent snapshot at or before ``entry_date``" — a rule,
  not a stored record. Carry-forward is not a third provenance tier (§9.3).
* **The staleness bound is in *calendar* days** (§9.5) and **per book**: IBKR 7,
  IDX 45. A single global bound is useless — it would have to clear IDX's ~31-day
  cadence, blinding it to an IBKR daily series that has silently died. Past the
  bound the level is *wrong*, not merely old, and a wrong denominator silently
  poisons the above-1% test — so both percentages go **null with a marker** (the
  ``insufficient_history`` convention, *not* a span-check error), the marker
  reaches the banner, and the Trade **leaves the risk-% aggregates with its count
  reported**.
* **Provenance tiers do something** (§9.3): an ``estimated`` snapshot still
  computes and still flags, but is **excluded from the aggregates, which report
  their excluded count**. Without that exclusion the tier would be decoration.

**Exposure % is identical, with no exceptions** — same lookup, same bound, same
null-with-marker, same exclusion-with-count, same inherited provenance. The one
allowed divergence is orthogonal: Exposure % needs no stop and is computed the
moment a Trade commits, while Risk % is *held open* until a stop arrives (§5.5) —
a held-open Risk % is a different hole from a stale denominator and is **never**
tagged ``insufficient_history``.

**No same-day Risk % is possible on either book** (§9.4): IBKR lags T-1/T-2,
Stockbit up to a month, so the at-or-before lookup naturally resolves to an
earlier snapshot and nothing here offers a same-day figure.

Everything is computed **at read time** from the current snapshots, never pinned:
a backdated snapshot arriving closer to an entry date simply recomputes a
previously nulled Risk % into a number — **hole-filling, not drift** (ADR 0004;
drift is an *outside* fact moving, which a late internal snapshot is not).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import FrozenSet, List, Optional, Sequence

from .equity import BOOK_IDX, BOOK_US

# The marker both percentages carry when the denominator is missing or past the
# staleness bound — the same ``insufficient_history`` convention the enrichment
# fields use (SPEC §7.8), a fact needing no repair, never a span-check failure.
INSUFFICIENT_HISTORY = "insufficient_history"

# The staleness bound, per book and in **calendar** days (§9.4/§9.5). IBKR is a
# weekday-dense daily series, so 7 days means the job has not run or Flex is
# failing — a fault that should surface. IDX records equity monthly, so its bound
# must clear the ~31-day cadence without going blind to real neglect.
STALENESS_BOUND_DAYS = {BOOK_US: 7, BOOK_IDX: 45}

STATED = "stated"
ESTIMATED = "estimated"


@dataclass(frozen=True)
class RiskExposure:
    """Risk % and Exposure % for one Trade, against one snapshot under one rule.

    Both percentages are ``None`` together when the denominator is missing or
    stale, and :attr:`markers` then carries :data:`INSUFFICIENT_HISTORY`. A
    ``None`` :attr:`risk_percentage` with a *fresh* snapshot is the held-open
    case (no stop yet) — distinguished by the absent marker. :attr:`provenance`
    is inherited from the snapshot so ``estimated`` rides through to the
    aggregate exclusion.
    """

    trade_id: Optional[int]
    book: str
    entry_date: str
    risk_percentage: Optional[float]
    exposure_percentage: Optional[float]
    provenance: Optional[str]          # inherited from the snapshot; None if none found
    snapshot_date: Optional[str]       # the snapshot actually used (at or before entry)
    staleness_days: Optional[int]      # calendar days from snapshot to entry; None if no snapshot
    markers: FrozenSet[str]

    @property
    def stale(self) -> bool:
        """True when the denominator is missing or past the bound — both null."""
        return INSUFFICIENT_HISTORY in self.markers

    @property
    def excluded_from_risk_aggregate(self) -> bool:
        """A Trade leaves the risk-% aggregate when nulled *or* ``estimated`` (§9.3/§9.4)."""
        return self.risk_percentage is None or self.provenance == ESTIMATED

    @property
    def excluded_from_exposure_aggregate(self) -> bool:
        """Identical to the risk exclusion — Exposure % has no exceptions (§9.4)."""
        return self.exposure_percentage is None or self.provenance == ESTIMATED


def snapshot_at_or_before(
    conn: sqlite3.Connection, book: str, entry_date: str
) -> Optional[sqlite3.Row]:
    """The most recent snapshot at or before ``entry_date`` for ``book`` (§9.4).

    The single lookup rule shared by both percentages. A snapshot dated *after*
    the entry is never reached back to; ``None`` when the book has no snapshot at
    or before the entry at all.
    """
    return conn.execute(
        "SELECT * FROM equity_snapshot WHERE book = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 1",
        (book, entry_date),
    ).fetchone()


def _calendar_days(snapshot_date: str, entry_date: str) -> int:
    """Calendar days from the snapshot to the entry — the axis the bound lives on."""
    return (date.fromisoformat(entry_date) - date.fromisoformat(snapshot_date)).days


def compute(
    conn: sqlite3.Connection,
    *,
    trade_id: Optional[int],
    book: str,
    entry_date: str,
    entry_qty: float,
    entry_avg_price: float,
    stop: Optional[float],
) -> RiskExposure:
    """Compute Risk % and Exposure % for one Trade under the shared lookup+bound.

    Exposure % is ``entry_avg_price × entry_qty ÷ equity × 100``; Risk % is
    ``(entry_avg_price − stop) × entry_qty ÷ equity × 100`` — the *same*
    denominator, the stop the only extra input. Missing or stale denominator →
    both null with :data:`INSUFFICIENT_HISTORY`. A fresh snapshot with no stop →
    Exposure % computes, Risk % is held open (no marker).
    """
    snap = snapshot_at_or_before(conn, book, entry_date)
    if snap is None:
        # No denominator at all — the missing-equity nag (§9.7), same null shape.
        return RiskExposure(
            trade_id=trade_id, book=book, entry_date=entry_date,
            risk_percentage=None, exposure_percentage=None,
            provenance=None, snapshot_date=None, staleness_days=None,
            markers=frozenset({INSUFFICIENT_HISTORY}),
        )

    staleness = _calendar_days(snap["date"], entry_date)
    provenance = snap["provenance"]
    bound = STALENESS_BOUND_DAYS[book]
    if staleness > bound:
        # Past the bound the level is *wrong*, not old — null both, mark, banner.
        return RiskExposure(
            trade_id=trade_id, book=book, entry_date=entry_date,
            risk_percentage=None, exposure_percentage=None,
            provenance=provenance, snapshot_date=snap["date"], staleness_days=staleness,
            markers=frozenset({INSUFFICIENT_HISTORY}),
        )

    equity = snap["equity"]
    exposure = entry_avg_price * entry_qty / equity * 100 if equity else None
    if stop is None or equity is None or equity == 0:
        risk = None
    else:
        risk = (entry_avg_price - stop) * entry_qty / equity * 100
    return RiskExposure(
        trade_id=trade_id, book=book, entry_date=entry_date,
        risk_percentage=risk, exposure_percentage=exposure,
        provenance=provenance, snapshot_date=snap["date"], staleness_days=staleness,
        markers=frozenset(),
    )


def compute_for_trade(conn: sqlite3.Connection, trade_id: int) -> RiskExposure:
    """Compute Risk % and Exposure % for a Trade by id (reads the ``trade`` row)."""
    row = conn.execute(
        "SELECT id, book, entry_date, entry_qty, entry_avg_price, stop "
        "FROM trade WHERE id = ?",
        (trade_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no Trade with id {trade_id}")
    return compute(
        conn,
        trade_id=row["id"], book=row["book"], entry_date=row["entry_date"],
        entry_qty=row["entry_qty"], entry_avg_price=row["entry_avg_price"],
        stop=row["stop"],
    )


def compute_book(conn: sqlite3.Connection, book: str) -> List[RiskExposure]:
    """Every Trade on ``book``, entry-ordered — the input to :func:`aggregate`.

    Nothing aggregates across books (SPEC §3.1), so the caller works one book at
    a time and the aggregate never mixes two denominators.
    """
    rows = conn.execute(
        "SELECT id, book, entry_date, entry_qty, entry_avg_price, stop "
        "FROM trade WHERE book = ? ORDER BY entry_date, id",
        (book,),
    ).fetchall()
    return [
        compute(
            conn,
            trade_id=r["id"], book=r["book"], entry_date=r["entry_date"],
            entry_qty=r["entry_qty"], entry_avg_price=r["entry_avg_price"],
            stop=r["stop"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class Aggregate:
    """A risk-% (or exposure-%) aggregate with every excluded count reported.

    The tiers *do something* only if their exclusion is counted, so nothing is
    dropped silently: :attr:`included` are the Trades that contribute a value,
    and the three excluded buckets partition the rest — stale (null denominator),
    ``estimated`` (computed but flagged out), and no-stop (Risk % held open;
    always ``0`` for the exposure metric, which needs no stop).
    """

    book: Optional[str]
    metric: str                        # 'risk' | 'exposure'
    values: tuple                      # the included percentages
    included: int
    excluded_stale: int
    excluded_estimated: int
    excluded_no_stop: int

    @property
    def mean(self) -> Optional[float]:
        return sum(self.values) / len(self.values) if self.values else None

    @property
    def excluded_total(self) -> int:
        return self.excluded_stale + self.excluded_estimated + self.excluded_no_stop


def aggregate(results: Sequence[RiskExposure], *, metric: str = "risk") -> Aggregate:
    """Fold per-Trade results into one aggregate, counting every exclusion (§9.3/§9.4).

    Each result lands in exactly one bucket: a null-with-marker is ``stale``; an
    otherwise-computable value on an ``estimated`` snapshot is ``estimated``; a
    null value with a fresh *stated* snapshot is ``no-stop`` (risk metric only);
    everything else is ``included``. Applied identically to both metrics — the
    only difference is that Exposure % has no no-stop hole.
    """
    if metric not in ("risk", "exposure"):
        raise ValueError(f"unknown metric {metric!r}")

    book = results[0].book if results else None
    values: List[float] = []
    stale = estimated = no_stop = 0
    for r in results:
        value = r.risk_percentage if metric == "risk" else r.exposure_percentage
        if r.stale:
            stale += 1
        elif r.provenance == ESTIMATED:
            estimated += 1
        elif value is None:
            no_stop += 1               # fresh, stated, but Risk % held open (no stop)
        else:
            values.append(value)
    return Aggregate(
        book=book, metric=metric, values=tuple(values), included=len(values),
        excluded_stale=stale, excluded_estimated=estimated, excluded_no_stop=no_stop,
    )


def banner_line(result: RiskExposure) -> Optional[str]:
    """The attention-banner fact for a stale/missing denominator, else ``None``.

    The staleness marker must reach the banner (§9.4), stated as a plain fact the
    way §9.7 states the missing-equity nag (``IDX equity: last snapshot 31 Jul``)
    — never an alarm. Returns ``None`` for a fresh Trade so the banner stays quiet.
    """
    if not result.stale:
        return None
    bound = STALENESS_BOUND_DAYS[result.book]
    if result.snapshot_date is None:
        return (
            f"{result.book} equity: no snapshot at or before entry {result.entry_date} "
            f"— Risk % and Exposure % null"
        )
    return (
        f"{result.book} equity: last snapshot {result.snapshot_date} is "
        f"{result.staleness_days}d before entry {result.entry_date} (bound {bound}d) "
        f"— Risk % and Exposure % null"
    )
