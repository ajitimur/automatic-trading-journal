"""Market regime as a property of a market on a date (SPEC §8, issue #30).

Regime is not a property of a trade. It lives in its own :class:`RegimeSnapshot`
keyed ``(book, date)``, computed once per book per day from the book's benchmark
series (QQQ for US, ``^JKSE`` for IDX — see :data:`journal.books.BENCHMARKS`).
A Trade holds two *references* into it — entry and exit — and **never copies the
values**, so the cross-market question stays answerable by joining on date and a
correlation assumption is never baked into a stored label.

**Six primitives, all stored regardless of the label** (§8.2): the close above
or below MA10/MA20/MA50 (three booleans) and each MA's slope — the *sign* of its
percent change over 5 trading days, with no flat-zone threshold (§6.2). Storing
them raw lets the label be re-cut retroactively with no refetch and no
re-derivation.

**The label** (§8.3) is a 5-level named ordinal evaluated top-down over
``above`` (how many of MA10/20/50 the close is above) and ``rising`` (how many
slopes are positive). No tunable parameters; the two "strong" bands are strict
so they stay rare. **Both books use this rule identically, untuned.**

**Two extras, outside the label** (§8.4): the index's distance from its 52-week
high and its 20-day realized volatility. Neither feeds the label; they exist
because "was I trading in a high-vol tape" cannot be reconstructed once a Trade
is frozen.

**Stamping** (§8.5): as of the prior trading day's close — the weather
observable when the decision was made, so the reference bar is the last one
*strictly before* the key date. On a missing bar (holiday, suspension, or a
calendar mismatch on a backdated entry) that naturally falls back to the last
available close before it, and ``bar_date`` records which bar was actually used
so the as-of date stays honest rather than silently sliding.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .bars import Bar

# The three MAs regime measures (§8.2). Deliberately shorter than the
# conventional toolkit — no MA200 — because it mirrors the horizon traded.
MA_WINDOWS = (10, 20, 50)

# Slope is the percent change over this many trading days, sign only (§6.2).
SLOPE_LOOKBACK = 5

# The 5-level named ordinal (§8.3).
STRONG_UPTREND = "strong_uptrend"
UPTREND = "uptrend"
STRONG_DOWNTREND = "strong_downtrend"
DOWNTREND = "downtrend"
NEUTRAL = "neutral"


@dataclass(frozen=True)
class RegimeSnapshot:
    """One book's regime on one date, keyed ``(book, date)``.

    ``label`` and the six primitives are ``None`` when the benchmark has too
    little history to compute them (§7.8); the two extras are ``None`` under the
    same rule. ``bar_date`` is always set — it is the bar used for the stamp.
    """

    book: str
    date: str                              # the (book, date) key — the decision date
    bar_date: str                          # prior trading day's close actually used
    label: Optional[str]
    close_above_ma10: Optional[bool]
    close_above_ma20: Optional[bool]
    close_above_ma50: Optional[bool]
    slope_ma10: Optional[int]              # sign: -1, 0, +1
    slope_ma20: Optional[int]
    slope_ma50: Optional[int]
    pct_off_52w_high: Optional[float]
    realized_vol_20d: Optional[float]

    def relabel(self) -> Optional[str]:
        """Re-cut the label from the stored primitives alone — no refetch (§8.3).

        The whole point of storing the six primitives is that the label is a
        pure function of them, so a re-cut needs neither bars nor re-derivation.
        """
        return _label_from_primitives(
            (self.close_above_ma10, self.close_above_ma20, self.close_above_ma50),
            (self.slope_ma10, self.slope_ma20, self.slope_ma50),
        )


def derive_label(above: int, rising: int) -> str:
    """The 5-level named ordinal, evaluated top-down (§8.3).

    ``above`` and ``rising`` are counts in 0..3. Mutually exclusive by
    evaluation order; no tunable parameter; the two "strong" bands are strict.
    """
    if above == 3 and rising == 3:
        return STRONG_UPTREND
    if above >= 2 and rising >= 2:
        return UPTREND
    if above == 0 and rising == 0:
        return STRONG_DOWNTREND
    if above <= 1 and rising <= 1:
        return DOWNTREND
    return NEUTRAL


def compute_snapshot(
    book: str, key_date: str, bars: Sequence[Bar]
) -> Optional[RegimeSnapshot]:
    """Compute the ``(book, key_date)`` snapshot from a benchmark series.

    ``bars`` is the trading-day benchmark series, oldest first (zero-volume rows
    already filtered at the cache boundary). The reference bar is the last one
    strictly before ``key_date`` — the prior trading day's close — which, on a
    missing bar, is the last available close before it. Returns ``None`` when no
    bar precedes ``key_date`` (nothing to stamp).
    """
    prior = [b for b in bars if b.date < key_date]
    if not prior:
        return None

    closes = [b.close for b in prior]
    close = closes[-1]

    mas = {n: _sma(closes, n) for n in MA_WINDOWS}
    above = {n: (close > mas[n] if mas[n] is not None else None) for n in MA_WINDOWS}
    slopes = {n: _slope_sign(closes, n) for n in MA_WINDOWS}

    label = _label_from_primitives(
        (above[10], above[20], above[50]),
        (slopes[10], slopes[20], slopes[50]),
    )

    return RegimeSnapshot(
        book=book,
        date=key_date,
        bar_date=prior[-1].date,
        label=label,
        close_above_ma10=above[10],
        close_above_ma20=above[20],
        close_above_ma50=above[50],
        slope_ma10=slopes[10],
        slope_ma20=slopes[20],
        slope_ma50=slopes[50],
        pct_off_52w_high=_pct_off_52w_high(prior),
        realized_vol_20d=_realized_vol_20d(closes),
    )


def _label_from_primitives(above_flags, slope_signs) -> Optional[str]:
    """Derive the label from the six primitives, or ``None`` if any is missing.

    The label needs all six; a missing MA50 (insufficient history) leaves it
    null rather than guessed from a short-window substitute (§7.8).
    """
    prims = tuple(above_flags) + tuple(slope_signs)
    if any(p is None for p in prims):
        return None
    above = sum(1 for f in above_flags if f)
    rising = sum(1 for s in slope_signs if s > 0)
    return derive_label(above, rising)


def _sma(closes: Sequence[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _slope_sign(
    closes: Sequence[float], n: int, lookback: int = SLOPE_LOOKBACK
) -> Optional[int]:
    """Sign of MA_n's percent change over ``lookback`` trading days (§6.2).

    The base MA is positive, so the percent change and the raw difference share
    a sign — the sign of ``(now - then)`` is the sign of the percent change. No
    flat-zone threshold: an exactly-flat MA is ``0``. Needs ``n + lookback``
    bars (the MA itself plus the lookback), else ``None``.
    """
    if len(closes) < n + lookback:
        return None
    now = sum(closes[-n:]) / n
    then = sum(closes[-n - lookback:-lookback]) / n
    return _sign(now - then)


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _pct_off_52w_high(bars: Sequence[Bar]) -> Optional[float]:
    """``(close / max(high over last 252 bars) - 1) * 100`` — zero or negative.

    The market-level twin of the per-symbol ``pct_off_52w_high`` (§7.2/§8.4).
    Null below 252 bars (§7.8), never a short-window substitute.
    """
    if len(bars) < 252:
        return None
    high_52w = max(b.high for b in bars[-252:])
    return (bars[-1].close / high_52w - 1) * 100


def _realized_vol_20d(closes: Sequence[float]) -> Optional[float]:
    """20-day realized volatility: the sample std of 20 daily log returns (§8.4).

    Over traded days only — zero-volume rows were filtered before storage, so a
    suspension does not collapse this toward zero. Twenty returns need 21
    closes; below that it is null.
    """
    if len(closes) < 21:
        return None
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(len(closes) - 20, len(closes))
    ]
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(variance)


_COLUMNS = (
    "book", "date", "bar_date", "label",
    "close_above_ma10", "close_above_ma20", "close_above_ma50",
    "slope_ma10", "slope_ma20", "slope_ma50",
    "pct_off_52w_high", "realized_vol_20d",
)

_UPSERT = f"""
INSERT INTO regime_snapshot ({", ".join(_COLUMNS)})
VALUES ({", ".join("?" for _ in _COLUMNS)})
ON CONFLICT(book, date) DO UPDATE SET
    bar_date=excluded.bar_date,
    label=excluded.label,
    close_above_ma10=excluded.close_above_ma10,
    close_above_ma20=excluded.close_above_ma20,
    close_above_ma50=excluded.close_above_ma50,
    slope_ma10=excluded.slope_ma10,
    slope_ma20=excluded.slope_ma20,
    slope_ma50=excluded.slope_ma50,
    pct_off_52w_high=excluded.pct_off_52w_high,
    realized_vol_20d=excluded.realized_vol_20d
"""


class RegimeStore:
    """Persist and read RegimeSnapshots on the one SQLite file.

    Keyed ``(book, date)``: ``upsert`` recomputes-in-place so re-running a day
    is idempotent, and Trades read a snapshot back by its natural key rather
    than copying its values onto themselves.
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    def upsert(self, snap: RegimeSnapshot) -> None:
        self.conn.execute(
            _UPSERT,
            (
                snap.book,
                snap.date,
                snap.bar_date,
                snap.label,
                _as_int(snap.close_above_ma10),
                _as_int(snap.close_above_ma20),
                _as_int(snap.close_above_ma50),
                snap.slope_ma10,
                snap.slope_ma20,
                snap.slope_ma50,
                snap.pct_off_52w_high,
                snap.realized_vol_20d,
            ),
        )
        self.conn.commit()

    def get(self, book: str, date: str) -> Optional[RegimeSnapshot]:
        row = self.conn.execute(
            "SELECT * FROM regime_snapshot WHERE book = ? AND date = ?",
            (book, date),
        ).fetchone()
        if row is None:
            return None
        return RegimeSnapshot(
            book=row["book"],
            date=row["date"],
            bar_date=row["bar_date"],
            label=row["label"],
            close_above_ma10=_as_bool(row["close_above_ma10"]),
            close_above_ma20=_as_bool(row["close_above_ma20"]),
            close_above_ma50=_as_bool(row["close_above_ma50"]),
            slope_ma10=row["slope_ma10"],
            slope_ma20=row["slope_ma20"],
            slope_ma50=row["slope_ma50"],
            pct_off_52w_high=row["pct_off_52w_high"],
            realized_vol_20d=row["realized_vol_20d"],
        )


def _as_int(flag: Optional[bool]) -> Optional[int]:
    return None if flag is None else int(flag)


def _as_bool(value: Optional[int]) -> Optional[bool]:
    return None if value is None else bool(value)
