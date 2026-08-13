"""Exit geometry (section D) and in-trade excursion (section E) — SPEC
§7.4–§7.5, issue #33.

Two things live here, both about the *held* Trade rather than the entry decision:

**D. Exit geometry — full symmetry with section B, anchored on ``C_x``.** The
exact section-B formulas (:mod:`journal.enrichment`) recomputed at a second date:
the close of the **final exit day**. Entry anchors on the prior close ``P₋₁``;
exit anchors on its *own* close ``C_x``. **This asymmetry is deliberate and must
not be "fixed"** (SPEC §6.3): entry is a discretionary intraday act, so the last
information the trader had was the prior completed bar and the entry day's close
would leak; the exit rule is *triggered by a close*, so the exit day's close is a
decision input, not a leak. The stored fields are the exit twins of section B —
``adr_pct_at_exit``, ``ma_dist_*_at_exit``, ``stack_state_at_exit``,
``rs_63d_at_exit`` — plus ``exit_avg_price`` stored beside ``C_x`` so the gap
between *what was got* and *what the day was worth* stays derivable (SPEC §6.3).

**E. Excursion — raw primitives, two scopes, units derived on read.** The maximum
``High`` and minimum ``Low`` in a window, *with the dates they occurred on*
(SPEC §7.5). R, ADR and % forms are **derived on read** (SPEC §6.4), never stored,
so the primitives stay usable as prices for a no-stop Trade. Two scopes are both
stored — the answer to the partial-exit problem:

* **Trade-level** — entry date → **final** exit date, position-agnostic. Measures
  *the move*, which is what setup selection asks about.
* **Per-Exit** — each Exit over entry date → **that Exit's** date. This is the
  exit-quality grading unit; *"was the day-3 partial early?"* is a question only
  this scope can answer.

**Quantity-weighting was rejected** — it blends the two scopes into one number
that answers neither. **Dates are stored, not just levels**: *"the high came on
day 2 and I held nineteen more"* is independently a finding, and the review
timeline is drawn from these dates later.

Post-exit (section F) is a separate window with its own freeze semantics and
belongs to #34; nothing here rolls or freezes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Sequence

from .bars import Bar
from .enrichment import (
    INSUFFICIENT_HISTORY,
    MA_WINDOWS,
    RS_WINDOW,
    _adr_pct,
    _pack_markers,
    _sma,
    _stack_state,
    _unpack_markers,
)

__all__ = [
    "ExitGeometry",
    "Excursion",
    "compute_exit_geometry",
    "compute_excursion",
    "ExitGeometryStore",
    "ExcursionStore",
    "INSUFFICIENT_HISTORY",
]


# ---------------------------------------------------------------------------
# D. Exit geometry — section B recomputed at C_x.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitGeometry:
    """Section B for one Trade, anchored on the final exit day's close ``C_x``.

    The exit twin of :class:`journal.enrichment.EntryEnrichment`, carrying only
    the fields section D reuses (SPEC §7.4) plus ``exit_avg_price``. Numeric
    fields are ``None`` when history is too short; the nulled field names live in
    :attr:`insufficient_history`, kept distinct from a span-check failure (which
    never reaches here). ``stack_state_at_exit`` is the one stored categorical;
    booleans and units derive on read (SPEC §6.4).
    """

    book: str
    symbol: str
    exit_date: str                 # the final exit date — the C_x date
    bar_date: str                  # the bar actually used for C_x (== exit_date)
    exit_avg_price: Optional[float]  # quantity-weighted mean of all exit fills

    adr_pct_at_exit: Optional[float]
    ma_10_at_exit: Optional[float]
    ma_20_at_exit: Optional[float]
    ma_50_at_exit: Optional[float]
    ma_100_at_exit: Optional[float]
    ma_200_at_exit: Optional[float]
    ma_dist_10_at_exit: Optional[float]
    ma_dist_20_at_exit: Optional[float]
    ma_dist_50_at_exit: Optional[float]
    ma_dist_100_at_exit: Optional[float]
    ma_dist_200_at_exit: Optional[float]
    stack_state_at_exit: Optional[str]
    rs_63d_at_exit: Optional[float]

    insufficient_history: FrozenSet[str]

    def marker(self, field: str) -> Optional[str]:
        """:data:`INSUFFICIENT_HISTORY` if ``field`` was nulled for want of history."""
        return INSUFFICIENT_HISTORY if field in self.insufficient_history else None


def compute_exit_geometry(
    book: str,
    symbol: str,
    exit_date: str,
    exit_avg_price: Optional[float],
    bars: Sequence[Bar],
    benchmark_bars: Sequence[Bar],
) -> Optional[ExitGeometry]:
    """Recompute section B anchored on ``C_x``, the final exit day's close.

    ``bars`` is the symbol's series and ``benchmark_bars`` the book's benchmark,
    both oldest first. The anchor is the last bar **at or before** ``exit_date``
    — the deliberate mirror of entry's *strictly before* anchor (SPEC §6.3): the
    exit day's own close is a decision input, so it is included in every window.
    ``exit_avg_price`` is passed through unchanged (the caller derives it from the
    Trade's exit fills). Returns ``None`` when no bar is at or before ``exit_date``.
    """
    upto = [b for b in bars if b.date <= exit_date]
    if not upto:
        return None

    closes = [b.close for b in upto]
    cx = closes[-1]                # C_x, the final exit day's close
    bar_date = upto[-1].date
    insufficient: set[str] = set()

    adr_pct = _adr_pct(upto)
    if adr_pct is None:
        insufficient.add("adr_pct_at_exit")

    mas = {n: _sma(closes, n) for n in MA_WINDOWS}
    for n in MA_WINDOWS:
        if mas[n] is None:
            insufficient.add(f"ma_{n}_at_exit")

    ma_dist: Dict[int, Optional[float]] = {}
    for n in MA_WINDOWS:
        if mas[n] is None or adr_pct is None:
            ma_dist[n] = None
            insufficient.add(f"ma_dist_{n}_at_exit")
        elif adr_pct == 0:
            ma_dist[n] = None          # degenerate, not a history problem
        else:
            ma_dist[n] = (cx - mas[n]) / cx * 100 / adr_pct

    stack_state = _stack_state(mas)
    if stack_state is None:
        insufficient.add("stack_state_at_exit")

    symbol_move = _move_to(bars, exit_date, RS_WINDOW)
    benchmark_move = _move_to(benchmark_bars, exit_date, RS_WINDOW)
    if symbol_move is None or benchmark_move is None:
        rs_63d = None
        insufficient.add("rs_63d_at_exit")
    else:
        rs_63d = symbol_move - benchmark_move

    return ExitGeometry(
        book=book,
        symbol=symbol,
        exit_date=exit_date,
        bar_date=bar_date,
        exit_avg_price=exit_avg_price,
        adr_pct_at_exit=adr_pct,
        ma_10_at_exit=mas[10], ma_20_at_exit=mas[20], ma_50_at_exit=mas[50],
        ma_100_at_exit=mas[100], ma_200_at_exit=mas[200],
        ma_dist_10_at_exit=ma_dist[10], ma_dist_20_at_exit=ma_dist[20],
        ma_dist_50_at_exit=ma_dist[50], ma_dist_100_at_exit=ma_dist[100],
        ma_dist_200_at_exit=ma_dist[200],
        stack_state_at_exit=stack_state,
        rs_63d_at_exit=rs_63d,
        insufficient_history=frozenset(insufficient),
    )


def _move_to(bars: Sequence[Bar], as_of: str, n: int) -> Optional[float]:
    """``(C / C₋ₙ − 1) × 100`` close-to-close over ``n`` trading days to ``as_of``.

    The exit-anchor mirror of :func:`journal.enrichment._prior_move`: the window
    ends **at or before** ``as_of`` inclusive, so ``C`` is ``C_x`` itself. Null
    below ``n + 1`` completed bars up to ``as_of``.
    """
    upto = [b for b in bars if b.date <= as_of]
    if len(upto) < n + 1:
        return None
    closes = [b.close for b in upto]
    return (closes[-1] / closes[-1 - n] - 1) * 100


# ---------------------------------------------------------------------------
# E. Excursion — raw primitives, units derived on read.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Excursion:
    """Raw favourable/adverse excursion over one window (SPEC §7.5).

    Stores levels *and* their dates; R, ADR and % forms derive on read (SPEC
    §6.4). The same window class serves both scopes — Trade-level and per-Exit —
    the only difference being ``end_date``. All four primitives are ``None`` when
    the window contains no bars.
    """

    start_date: str
    end_date: str
    mfe_high: Optional[float]       # maximum High in the window
    mfe_date: Optional[str]         # the date it occurred (earliest on a tie)
    mae_low: Optional[float]        # minimum Low in the window
    mae_date: Optional[str]

    def mfe_r(self, entry_avg_price: float, stop: Optional[float]) -> Optional[float]:
        return _in_r(self.mfe_high, entry_avg_price, stop)

    def mae_r(self, entry_avg_price: float, stop: Optional[float]) -> Optional[float]:
        return _in_r(self.mae_low, entry_avg_price, stop)

    def mfe_adr(self, entry_avg_price: float, adr_pct: Optional[float]) -> Optional[float]:
        return _in_adr(self.mfe_high, entry_avg_price, adr_pct)

    def mae_adr(self, entry_avg_price: float, adr_pct: Optional[float]) -> Optional[float]:
        return _in_adr(self.mae_low, entry_avg_price, adr_pct)

    def mfe_pct(self, entry_avg_price: float) -> Optional[float]:
        return _in_pct(self.mfe_high, entry_avg_price)

    def mae_pct(self, entry_avg_price: float) -> Optional[float]:
        return _in_pct(self.mae_low, entry_avg_price)


def _in_r(level: Optional[float], entry_avg_price: float, stop: Optional[float]) -> Optional[float]:
    """``(level − entry) / (entry − stop)`` — null without a stop (SPEC §6.4)."""
    if level is None or stop is None:
        return None
    denom = entry_avg_price - stop
    if denom == 0:
        return None
    return (level - entry_avg_price) / denom


def _in_adr(level: Optional[float], entry_avg_price: float, adr_pct: Optional[float]) -> Optional[float]:
    """``(level − entry) / entry × 100 ÷ adr_pct`` — null without ADR%."""
    if level is None or adr_pct is None or adr_pct == 0 or entry_avg_price == 0:
        return None
    return (level - entry_avg_price) / entry_avg_price * 100 / adr_pct


def _in_pct(level: Optional[float], entry_avg_price: float) -> Optional[float]:
    """``(level / entry − 1) × 100`` — needs no stop or ADR%."""
    if level is None or entry_avg_price == 0:
        return None
    return (level / entry_avg_price - 1) * 100


def compute_excursion(
    bars: Sequence[Bar], start_date: str, end_date: str
) -> Excursion:
    """Maximum High and minimum Low over ``[start_date, end_date]``, with dates.

    Both endpoints are inclusive — the window is the whole holding period,
    entry-day and exit-day bars included. On a tie the **earliest** date wins,
    because *when* the extreme first arrived is the finding. An empty window
    yields all-``None`` primitives (no bars is not zero).
    """
    window = [b for b in bars if start_date <= b.date <= end_date]
    if not window:
        return Excursion(start_date, end_date, None, None, None, None)

    # ``window`` is oldest-first; a strict comparison keeps the *first* bar to
    # reach each extreme, so the earliest date wins a tie (SPEC §7.5 — when the
    # extreme first arrived is the finding).
    mfe_bar = window[0]
    mae_bar = window[0]
    for b in window[1:]:
        if b.high > mfe_bar.high:
            mfe_bar = b
        if b.low < mae_bar.low:
            mae_bar = b
    return Excursion(
        start_date=start_date,
        end_date=end_date,
        mfe_high=mfe_bar.high,
        mfe_date=mfe_bar.date,
        mae_low=mae_bar.low,
        mae_date=mae_bar.date,
    )


# ---------------------------------------------------------------------------
# Storage.
# ---------------------------------------------------------------------------


_GEOMETRY_COLUMNS = (
    "trade_id", "book", "symbol", "exit_date", "bar_date", "exit_avg_price",
    "adr_pct_at_exit",
    "ma_10_at_exit", "ma_20_at_exit", "ma_50_at_exit",
    "ma_100_at_exit", "ma_200_at_exit",
    "ma_dist_10_at_exit", "ma_dist_20_at_exit", "ma_dist_50_at_exit",
    "ma_dist_100_at_exit", "ma_dist_200_at_exit",
    "stack_state_at_exit", "rs_63d_at_exit",
    "insufficient_history",
)

_GEOMETRY_UPDATE = tuple(c for c in _GEOMETRY_COLUMNS if c != "trade_id")

_GEOMETRY_UPSERT = f"""
INSERT INTO trade_exit_geometry ({", ".join(_GEOMETRY_COLUMNS)})
VALUES ({", ".join("?" for _ in _GEOMETRY_COLUMNS)})
ON CONFLICT(trade_id) DO UPDATE SET
{", ".join(f"{c}=excluded.{c}" for c in _GEOMETRY_UPDATE)}
"""


class ExitGeometryStore:
    """Persist and read :class:`ExitGeometry` keyed by ``trade_id`` (one per Trade).

    Upsert recomputes-in-place so re-enriching a closed Trade is idempotent. The
    ``insufficient_history`` set round-trips as a comma-joined text column so each
    null's *reason* survives storage.
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    def upsert(self, trade_id: int, geom: ExitGeometry) -> None:
        self.conn.execute(
            _GEOMETRY_UPSERT,
            (
                trade_id,
                geom.book, geom.symbol, geom.exit_date, geom.bar_date,
                geom.exit_avg_price,
                geom.adr_pct_at_exit,
                geom.ma_10_at_exit, geom.ma_20_at_exit, geom.ma_50_at_exit,
                geom.ma_100_at_exit, geom.ma_200_at_exit,
                geom.ma_dist_10_at_exit, geom.ma_dist_20_at_exit,
                geom.ma_dist_50_at_exit, geom.ma_dist_100_at_exit,
                geom.ma_dist_200_at_exit,
                geom.stack_state_at_exit, geom.rs_63d_at_exit,
                _pack_markers(geom.insufficient_history),
            ),
        )
        self.conn.commit()

    def get(self, trade_id: int) -> Optional[ExitGeometry]:
        row = self.conn.execute(
            "SELECT * FROM trade_exit_geometry WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        if row is None:
            return None
        return ExitGeometry(
            book=row["book"], symbol=row["symbol"],
            exit_date=row["exit_date"], bar_date=row["bar_date"],
            exit_avg_price=row["exit_avg_price"],
            adr_pct_at_exit=row["adr_pct_at_exit"],
            ma_10_at_exit=row["ma_10_at_exit"], ma_20_at_exit=row["ma_20_at_exit"],
            ma_50_at_exit=row["ma_50_at_exit"], ma_100_at_exit=row["ma_100_at_exit"],
            ma_200_at_exit=row["ma_200_at_exit"],
            ma_dist_10_at_exit=row["ma_dist_10_at_exit"],
            ma_dist_20_at_exit=row["ma_dist_20_at_exit"],
            ma_dist_50_at_exit=row["ma_dist_50_at_exit"],
            ma_dist_100_at_exit=row["ma_dist_100_at_exit"],
            ma_dist_200_at_exit=row["ma_dist_200_at_exit"],
            stack_state_at_exit=row["stack_state_at_exit"],
            rs_63d_at_exit=row["rs_63d_at_exit"],
            insufficient_history=_unpack_markers(row["insufficient_history"]),
        )


class ExcursionStore:
    """Persist and read :class:`Excursion` at both scopes (SPEC §7.5).

    The **Trade-level** excursion is keyed by ``trade_id`` (one per Trade, entry →
    final exit); the **per-Exit** excursion is keyed by ``exit_id`` (one per Exit,
    entry → that Exit's date). Both upserts recompute-in-place. Kept two tables,
    not one with a scope column, because the keys differ — a per-Exit row hangs
    off a ``trade_exit`` id that the Trade-level row has no equivalent of.
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    def upsert_trade(self, trade_id: int, exc: Excursion) -> None:
        self.conn.execute(
            """
            INSERT INTO trade_excursion
                (trade_id, start_date, end_date, mfe_high, mfe_date, mae_low, mae_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_id) DO UPDATE SET
                start_date=excluded.start_date, end_date=excluded.end_date,
                mfe_high=excluded.mfe_high, mfe_date=excluded.mfe_date,
                mae_low=excluded.mae_low, mae_date=excluded.mae_date
            """,
            (trade_id, exc.start_date, exc.end_date, exc.mfe_high, exc.mfe_date,
             exc.mae_low, exc.mae_date),
        )
        self.conn.commit()

    def get_trade(self, trade_id: int) -> Optional[Excursion]:
        row = self.conn.execute(
            "SELECT * FROM trade_excursion WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        return _row_to_excursion(row)

    def upsert_exit(self, exit_id: int, trade_id: int, exc: Excursion) -> None:
        self.conn.execute(
            """
            INSERT INTO exit_excursion
                (exit_id, trade_id, start_date, end_date, mfe_high, mfe_date,
                 mae_low, mae_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(exit_id) DO UPDATE SET
                trade_id=excluded.trade_id,
                start_date=excluded.start_date, end_date=excluded.end_date,
                mfe_high=excluded.mfe_high, mfe_date=excluded.mfe_date,
                mae_low=excluded.mae_low, mae_date=excluded.mae_date
            """,
            (exit_id, trade_id, exc.start_date, exc.end_date, exc.mfe_high,
             exc.mfe_date, exc.mae_low, exc.mae_date),
        )
        self.conn.commit()

    def get_exit(self, exit_id: int) -> Optional[Excursion]:
        row = self.conn.execute(
            "SELECT * FROM exit_excursion WHERE exit_id = ?", (exit_id,)
        ).fetchone()
        return _row_to_excursion(row)


def _row_to_excursion(row) -> Optional[Excursion]:
    if row is None:
        return None
    return Excursion(
        start_date=row["start_date"], end_date=row["end_date"],
        mfe_high=row["mfe_high"], mfe_date=row["mfe_date"],
        mae_low=row["mae_low"], mae_date=row["mae_date"],
    )
