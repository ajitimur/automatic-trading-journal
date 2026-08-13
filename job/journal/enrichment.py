"""Entry-dated setup enrichment: ADR%, the MA stack, setup geometry (SPEC
§7.1–§7.2, issue #29).

Sections A and B of the field list. Everything here is a property of one Trade's
*entry decision*, so it anchors on the **prior close** ``P₋₁`` — the close of the
last completed bar strictly before the entry date (SPEC §6.3). The entry day's
close had not happened when the decision was made; anchoring on it would leak
post-decision information into a measurement of the decision. **One flagged
exception:** ``volume_ratio`` uses the entry day's *volume*, because it describes
the breakout bar rather than the decision.

**ADR% is the single normalizer** (SPEC §6.1). It is scale-free — mean daily
range as a percent — so a $400 US name and an IDR 7,200 IDX name compare with no
currency handling, and every ADR-normalized distance (``ma_dist_*``) is stated
in ADR units. ATR is dropped.

**MAs are SMA throughout, all five of 10/20/50/100/200** (SPEC §6.2), load-bearing
because the mechanical exit rule is "a close below MA10" and an EMA/SMA mismatch
would misgrade every borderline exit.

**Store continuous primitives; derive booleans, orderings and units on read**
(SPEC §6.4). ``ma_dist_N`` carries the signed continuous distance; "above MA50"
is ``ma_dist_50 > 0``. ``stack_state`` is the single stored categorical, earned
because it will be grouped on and it serves **setup selection only** — it is the
*symbol's* own MA ordering, not the benchmark's (that is regime, referenced via
``RegimeSnapshot`` and never copied here).

**Insufficient history nulls the field with an explicit marker and never computes
a short-window substitute** (SPEC §7.8) — a 40-bar "MA200" is silently wrong,
which is worse than absent. The nulls propagate exactly: ``< 20`` completed bars
nulls ``adr_pct`` and every ADR-normalized field downstream; ``< N`` bars nulls
``ma_N`` → ``ma_dist_N`` → ``stack_state``; ``< 252`` bars nulls
``pct_off_52w_high``; a benchmark shorter than 63 bars nulls ``rs_63d``. Each
such null is tagged :data:`INSUFFICIENT_HISTORY` in
:attr:`EntryEnrichment.insufficient_history`, kept **strictly distinct** from a
span-check failure (SPEC §4.4) — that is a corruption risk demanding repair and
arrives as a :class:`journal.bars.SpanCheckError` before compute is ever reached,
never as one of these nulls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Optional, Sequence

from .bars import Bar

# The marker a field carries when it is null because the instrument has too little
# history — a fact about the instrument needing no action, never a fetch failure.
INSUFFICIENT_HISTORY = "insufficient_history"

# Section A: ADR% over 20 completed bars (SPEC §7.1).
ADR_WINDOW = 20

# The five SMAs (SPEC §6.2/§7.2). All load-bearing; the exit rule reads MA10.
MA_WINDOWS = (10, 20, 50, 100, 200)

# prior_move_* windows, in *trading* days not calendar months (SPEC §7.2).
PRIOR_MOVE_WINDOWS = (21, 63, 126)

# pct_off_52w_high looks back this many completed bars (SPEC §7.2).
HIGH_52W_WINDOW = 252

# volume_ratio's denominator: mean volume over this many completed bars (§7.2).
VOLUME_MEAN_WINDOW = 50

# avg_turnover_20d and rs_63d windows (SPEC §7.2).
TURNOVER_WINDOW = 20
RS_WINDOW = 63

# stack_state: the symbol's own MA ordering (SPEC §7.2), setup selection only.
ALIGNED_UP = "aligned_up"
ALIGNED_DOWN = "aligned_down"
MIXED = "mixed"


@dataclass(frozen=True)
class EntryEnrichment:
    """Sections A and B for one Trade, anchored on the prior close ``P₋₁``.

    Every numeric field is ``None`` when the instrument's history is too short to
    compute it honestly; the field names so nulled are listed in
    :attr:`insufficient_history`, so an ``insufficient_history`` null is always
    distinguishable from a span-check failure (which never reaches here). Booleans
    and orderings are derived on read — only ``stack_state`` is a stored
    categorical.
    """

    book: str
    symbol: str
    entry_date: str
    bar_date: str                          # the prior-close bar actually used (P₋₁)

    # A. Volatility base
    adr_pct: Optional[float]

    # B. Setup geometry — as of the prior close
    ma_10: Optional[float]
    ma_20: Optional[float]
    ma_50: Optional[float]
    ma_100: Optional[float]
    ma_200: Optional[float]
    ma_dist_10: Optional[float]
    ma_dist_20: Optional[float]
    ma_dist_50: Optional[float]
    ma_dist_100: Optional[float]
    ma_dist_200: Optional[float]
    stack_state: Optional[str]
    prior_move_21d: Optional[float]
    prior_move_63d: Optional[float]
    prior_move_126d: Optional[float]
    pct_off_52w_high: Optional[float]
    rs_63d: Optional[float]
    volume_ratio: Optional[float]          # the one field on the entry day's volume
    avg_turnover_20d: Optional[float]      # native currency (SPEC §7.2)

    insufficient_history: FrozenSet[str]

    def marker(self, field: str) -> Optional[str]:
        """:data:`INSUFFICIENT_HISTORY` if ``field`` was nulled for want of history.

        The positive assertion of *why* a null is null. A field null for any
        other reason (a degenerate zero ADR%, a missing entry-day bar) is not
        tagged, and a span-check failure never arrives as a value at all.
        """
        return INSUFFICIENT_HISTORY if field in self.insufficient_history else None


def compute_entry_enrichment(
    book: str,
    symbol: str,
    entry_date: str,
    bars: Sequence[Bar],
    benchmark_bars: Sequence[Bar],
) -> Optional[EntryEnrichment]:
    """Compute sections A and B for a Trade's entry from cached trading-day bars.

    ``bars`` is the symbol's series and ``benchmark_bars`` the book's benchmark
    (QQQ for US, ``^JKSE`` for IDX — see :data:`journal.books.BENCHMARKS`), both
    oldest first with zero-volume rows already filtered at the cache boundary. The
    anchor ``P₋₁`` is the last bar strictly before ``entry_date``; ``volume_ratio``
    alone reads the entry day's own bar. Returns ``None`` when no bar precedes
    ``entry_date`` (nothing to anchor on).
    """
    prior = [b for b in bars if b.date < entry_date]
    if not prior:
        return None

    closes = [b.close for b in prior]
    p = closes[-1]                         # P₋₁, the prior close
    bar_date = prior[-1].date
    insufficient: set[str] = set()

    # A. adr_pct — the single normalizer; its absence nulls every ADR-unit field.
    adr_pct = _adr_pct(prior)
    if adr_pct is None:
        insufficient.add("adr_pct")

    # B. The MA stack (SMA). A missing MA_N nulls its distance and stack_state.
    mas = {n: _sma(closes, n) for n in MA_WINDOWS}
    for n in MA_WINDOWS:
        if mas[n] is None:
            insufficient.add(f"ma_{n}")

    ma_dist = {}
    for n in MA_WINDOWS:
        if mas[n] is None or adr_pct is None:
            ma_dist[n] = None
            insufficient.add(f"ma_dist_{n}")
        elif adr_pct == 0:
            ma_dist[n] = None              # degenerate, not a history problem
        else:
            ma_dist[n] = (p - mas[n]) / p * 100 / adr_pct

    stack_state = _stack_state(mas)
    if stack_state is None:
        insufficient.add("stack_state")

    prior_move = {}
    for n in PRIOR_MOVE_WINDOWS:
        if len(closes) >= n + 1:
            prior_move[n] = (p / closes[-1 - n] - 1) * 100
        else:
            prior_move[n] = None
            insufficient.add(f"prior_move_{n}d")

    pct_off_52w_high = _pct_off_52w_high(prior, p)
    if pct_off_52w_high is None:
        insufficient.add("pct_off_52w_high")

    # rs_63d — the symbol's standing within its market, not the market's weather.
    benchmark_move_63 = _prior_move(benchmark_bars, entry_date, RS_WINDOW)
    if prior_move[RS_WINDOW] is None or benchmark_move_63 is None:
        rs_63d = None
        insufficient.add("rs_63d")
    else:
        rs_63d = prior_move[RS_WINDOW] - benchmark_move_63

    volume_ratio = _volume_ratio(bars, prior, entry_date, insufficient)
    avg_turnover_20d = _avg_turnover(prior)
    if avg_turnover_20d is None:
        insufficient.add("avg_turnover_20d")

    return EntryEnrichment(
        book=book,
        symbol=symbol,
        entry_date=entry_date,
        bar_date=bar_date,
        adr_pct=adr_pct,
        ma_10=mas[10], ma_20=mas[20], ma_50=mas[50],
        ma_100=mas[100], ma_200=mas[200],
        ma_dist_10=ma_dist[10], ma_dist_20=ma_dist[20], ma_dist_50=ma_dist[50],
        ma_dist_100=ma_dist[100], ma_dist_200=ma_dist[200],
        stack_state=stack_state,
        prior_move_21d=prior_move[21],
        prior_move_63d=prior_move[63],
        prior_move_126d=prior_move[126],
        pct_off_52w_high=pct_off_52w_high,
        rs_63d=rs_63d,
        volume_ratio=volume_ratio,
        avg_turnover_20d=avg_turnover_20d,
        insufficient_history=frozenset(insufficient),
    )


def _adr_pct(bars: Sequence[Bar]) -> Optional[float]:
    """Mean of ``(High/Low − 1) × 100`` over the last 20 completed bars (§7.1).

    Null below 20 bars (§7.8), never a short-window substitute.
    """
    if len(bars) < ADR_WINDOW:
        return None
    window = bars[-ADR_WINDOW:]
    return sum((b.high / b.low - 1) * 100 for b in window) / ADR_WINDOW


def _sma(closes: Sequence[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _stack_state(mas: dict[int, Optional[float]]) -> Optional[str]:
    """``aligned_up`` / ``aligned_down`` / ``mixed`` from the five MAs (§7.2).

    Null when any MA is null (insufficient history) — the ordering is undefined
    without all five, and a short-window substitute is never used.
    """
    ordered = [mas[n] for n in MA_WINDOWS]
    if any(v is None for v in ordered):
        return None
    if all(a > b for a, b in zip(ordered, ordered[1:])):
        return ALIGNED_UP
    if all(a < b for a, b in zip(ordered, ordered[1:])):
        return ALIGNED_DOWN
    return MIXED


def _prior_move(
    bars: Sequence[Bar], entry_date: str, n: int
) -> Optional[float]:
    """``(P₋₁ / P₋₁₋N − 1) × 100`` over ``n`` trading days, close-to-close (§7.2).

    Used for the benchmark leg of ``rs_63d`` — anchored on the benchmark's own
    prior close before ``entry_date``. Null below ``n + 1`` completed bars.
    """
    prior = [b for b in bars if b.date < entry_date]
    if len(prior) < n + 1:
        return None
    closes = [b.close for b in prior]
    return (closes[-1] / closes[-1 - n] - 1) * 100


def _pct_off_52w_high(bars: Sequence[Bar], p: float) -> Optional[float]:
    """``(P₋₁ / max(High over last 252 bars) − 1) × 100`` — zero or negative (§7.2).

    Null below 252 bars (§7.8).
    """
    if len(bars) < HIGH_52W_WINDOW:
        return None
    high_52w = max(b.high for b in bars[-HIGH_52W_WINDOW:])
    return (p / high_52w - 1) * 100


def _volume_ratio(
    bars: Sequence[Bar],
    prior: Sequence[Bar],
    entry_date: str,
    insufficient: set[str],
) -> Optional[float]:
    """``Volume(entry day) ÷ mean(Volume over 50 completed bars before entry)``.

    The one field departing from the prior-close anchor (SPEC §6.3): the numerator
    is the entry day's own volume, describing the breakout bar. Null with the
    ``insufficient_history`` marker below 50 completed bars; null *without* a
    marker when the entry day itself has no bar (a missing bar, not a history
    fact).
    """
    if len(prior) < VOLUME_MEAN_WINDOW:
        insufficient.add("volume_ratio")
        return None
    entry_bar = next((b for b in bars if b.date == entry_date), None)
    if entry_bar is None:
        return None
    mean_vol = sum(b.volume for b in prior[-VOLUME_MEAN_WINDOW:]) / VOLUME_MEAN_WINDOW
    if mean_vol == 0:
        return None
    return entry_bar.volume / mean_vol


def _avg_turnover(bars: Sequence[Bar]) -> Optional[float]:
    """``mean(Close_i × Volume_i)`` over 20 completed bars, native currency (§7.2).

    Not ADR-normalized — turnover is a native-currency liquidity measure feeding
    sizing. Null below 20 bars.
    """
    if len(bars) < TURNOVER_WINDOW:
        return None
    window = bars[-TURNOVER_WINDOW:]
    return sum(b.close * b.volume for b in window) / TURNOVER_WINDOW


_COLUMNS = (
    "trade_id", "book", "symbol", "entry_date", "bar_date",
    "adr_pct",
    "ma_10", "ma_20", "ma_50", "ma_100", "ma_200",
    "ma_dist_10", "ma_dist_20", "ma_dist_50", "ma_dist_100", "ma_dist_200",
    "stack_state",
    "prior_move_21d", "prior_move_63d", "prior_move_126d",
    "pct_off_52w_high", "rs_63d", "volume_ratio", "avg_turnover_20d",
    "insufficient_history",
)

_UPDATE_COLUMNS = tuple(c for c in _COLUMNS if c != "trade_id")

_UPSERT = f"""
INSERT INTO trade_enrichment ({", ".join(_COLUMNS)})
VALUES ({", ".join("?" for _ in _COLUMNS)})
ON CONFLICT(trade_id) DO UPDATE SET
{", ".join(f"{c}=excluded.{c}" for c in _UPDATE_COLUMNS)}
"""


class EnrichmentStore:
    """Persist and read :class:`EntryEnrichment` keyed by ``trade_id``.

    Upsert recomputes-in-place so re-enriching a Trade is idempotent. The
    ``insufficient_history`` marker set is stored as a comma-joined text column
    (empty string when nothing was nulled for history) so it round-trips exactly
    and a null's *reason* survives storage.
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    def upsert(self, trade_id: int, enr: EntryEnrichment) -> None:
        self.conn.execute(
            _UPSERT,
            (
                trade_id,
                enr.book, enr.symbol, enr.entry_date, enr.bar_date,
                enr.adr_pct,
                enr.ma_10, enr.ma_20, enr.ma_50, enr.ma_100, enr.ma_200,
                enr.ma_dist_10, enr.ma_dist_20, enr.ma_dist_50,
                enr.ma_dist_100, enr.ma_dist_200,
                enr.stack_state,
                enr.prior_move_21d, enr.prior_move_63d, enr.prior_move_126d,
                enr.pct_off_52w_high, enr.rs_63d, enr.volume_ratio,
                enr.avg_turnover_20d,
                _pack_markers(enr.insufficient_history),
            ),
        )
        self.conn.commit()

    def get(self, trade_id: int) -> Optional[EntryEnrichment]:
        row = self.conn.execute(
            "SELECT * FROM trade_enrichment WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        if row is None:
            return None
        return EntryEnrichment(
            book=row["book"], symbol=row["symbol"],
            entry_date=row["entry_date"], bar_date=row["bar_date"],
            adr_pct=row["adr_pct"],
            ma_10=row["ma_10"], ma_20=row["ma_20"], ma_50=row["ma_50"],
            ma_100=row["ma_100"], ma_200=row["ma_200"],
            ma_dist_10=row["ma_dist_10"], ma_dist_20=row["ma_dist_20"],
            ma_dist_50=row["ma_dist_50"], ma_dist_100=row["ma_dist_100"],
            ma_dist_200=row["ma_dist_200"],
            stack_state=row["stack_state"],
            prior_move_21d=row["prior_move_21d"],
            prior_move_63d=row["prior_move_63d"],
            prior_move_126d=row["prior_move_126d"],
            pct_off_52w_high=row["pct_off_52w_high"],
            rs_63d=row["rs_63d"], volume_ratio=row["volume_ratio"],
            avg_turnover_20d=row["avg_turnover_20d"],
            insufficient_history=_unpack_markers(row["insufficient_history"]),
        )


def _pack_markers(markers: FrozenSet[str]) -> str:
    return ",".join(sorted(markers))


def _unpack_markers(packed: Optional[str]) -> FrozenSet[str]:
    if not packed:
        return frozenset()
    return frozenset(packed.split(","))
