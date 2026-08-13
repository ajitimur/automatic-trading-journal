"""The counterfactual and adherence engine — six variants (SPEC §10, issue #35).

Identified during charting as the highest-value part of the project. **Adherence
is inverted** (§10.1): the obvious design has the trader declare a rule and the
engine check obedience, but with no Plan (ADR 0002) a declared rule would be a
third hand-entered field *and* intent reconstructed after the outcome. Instead
the engine scores every closed Trade against **all six variants** and reports
which it best fits — *"which rule did this follow"* is derived, never asserted.

**No verdicts, ever.** Signed deltas only, never a boolean ``adhered``. There is
no way to separate a considered override from a mistake, because that information
does not exist in the data.

**The six variants** are ``trail {ma10, ma20}`` × ``partial {none, day3, day5}``,
plus ``actual`` as the reference row. MA50 is dropped; the partial fraction is
fixed at ``1/3`` so timing is the only thing varying. The entry day is **day 1**,
so day 3 is ``entry + 2`` trading days and the band days 3–5 spans day 3 → day 5
inclusive.

**Every variant carries the recorded stop as a hard leg** (§10.5) — without it a
counterfactual on a losing trade rides straight through the price the trader
would have been stopped out at, and becomes fiction. Pricing is deliberately
asymmetric and **must not be "fixed"**:

* **Trail signal** (a close below MA_N) prices at the **next trading day's open** —
  the signal is not knowable until after the bell, and the bias is one-directional.
* **Scheduled partial** (day 3 / day 5) prices at **that day's close** — its
  trigger is a calendar date, known in advance.
* **Stop hit** (``Low ≤ stop``) fills at the stop; **gapped** (``Open < stop``) at
  the open. The stop leg wins over a same-day trail signal.

The **actual side is never repriced** — those are real fills.

**Bound** at the trail signal or **60 trading days**, recorded ``resolved`` /
``capped``. The cap **never substitutes a pseudo-exit** (§10.8): the remaining
position is recorded as a ``cap`` leg with a *null* price so no fabricated number
can enter an aggregate.

**Absence is not deviation** (§10.7): a Trade stopped out before the band gets
``partial_state = not_applicable`` and a null timing delta — never a number.

Per variant the **raw exit legs** are stored and units derived on read; a
``deviation_cost`` stored *as* R would leave no-stop Trades with no cost at all.
**Fit is scored by behaviour in trading days, not outcome** — the full six-way
distance vector is stored and best-fit derived at read time.

Runs in the daily job on **closed Trades only, never at confirm** (§10.9); a
variant resolving after freeze fills in without counting as drift.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .bars import Bar

# --------------------------------------------------------------------------- #
# The variant axes (§10.3). MA50 is dropped; the partial fraction is fixed.
# --------------------------------------------------------------------------- #

TRAILS = ("ma10", "ma20")
PARTIALS = ("none", "day3", "day5")
TRAIL_MA = {"ma10": 10, "ma20": 20}
# The 1-indexed trading day a scheduled partial fires on (entry = day 1, §10.4).
PARTIAL_DAY = {"day3": 3, "day5": 5}

VARIANTS: Tuple[str, ...] = tuple(f"{t}/{p}" for t in TRAILS for p in PARTIALS)

# The fixed partial fraction, a constant in the versioned ruleset (§10.3).
PARTIAL_FRACTION = 1.0 / 3.0
# The band days 3–5 inclusive (§10.4); the entry day is day 1.
BAND = (3, 5)
# The simulation bound in trading days (§10.5).
CAP_TRADING_DAYS = 60

# counterfactual_status (§10.5). ``pending`` is internal only — a variant that
# has neither resolved nor reached the cap because the bar series does not yet
# extend far enough; the daily job re-runs it until it lands resolved | capped.
RESOLVED = "resolved"
CAPPED = "capped"
PENDING = "pending"

# Leg triggers (§10.7).
TRIGGER_PARTIAL = "partial"
TRIGGER_TRAIL = "trail"
TRIGGER_STOP = "stop"
TRIGGER_CAP = "cap"

# partial_state values (§10.7).
IN_BAND = "in_band"
EARLY = "early"
LATE = "late"
NONE = "none"
NOT_APPLICABLE = "not_applicable"

# exit_path values (§10.7), derived from confirmed exit reasons.
EXIT_STOP_HIT = "stop_hit"
EXIT_TRAIL = "trail"
EXIT_DISCRETIONARY = "discretionary"
EXIT_OTHER = "other"


# --------------------------------------------------------------------------- #
# The ruleset (§10.2). One global, versioned ruleset with effective dates. A
# version names which variant is nominal; the other five compute regardless. A
# rule change mints v2 and never edits v1.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RulesetVersion:
    """A dated statement of the mechanical strategy, naming the nominal variant.

    ``nominal_partial`` is the near edge of the 3–5 band (``day3``): the band is
    what grades the *actual* trade's ``partial_state``, but a concrete simulation
    of the nominal outcome needs one timing, and day 3 is the first day the rule
    is satisfiable. A Trade is graded against whichever version was live on its
    entry date; a version is superseded, never edited (§10.2).
    """

    version: str
    effective_from: str
    nominal_trail: str          # 'ma10'
    nominal_partial: str        # 'day3' — the band's near edge
    partial_fraction: float
    cap_trading_days: int

    @property
    def nominal_variant(self) -> str:
        return f"{self.nominal_trail}/{self.nominal_partial}"


# ``ruleset_v1`` = partial 1/3 on days 3–5, then trail MA10. Effective from the
# July 2026 backdating floor, so all current history grades against v1 (§10.2).
RULESET_V1 = RulesetVersion(
    version="ruleset_v1",
    effective_from="2026-07-01",
    nominal_trail="ma10",
    nominal_partial="day3",
    partial_fraction=PARTIAL_FRACTION,
    cap_trading_days=CAP_TRADING_DAYS,
)

# Ordered oldest-first; a rule change appends a new version, never edits one.
RULESETS: Tuple[RulesetVersion, ...] = (RULESET_V1,)


def ruleset_for(entry_date: str) -> Optional[RulesetVersion]:
    """The ruleset version live on ``entry_date`` — the latest at or before it.

    ``None`` for a Trade entered before the earliest version's effective date
    (before the backdating floor); such a Trade has no rule to grade against.
    """
    live = [r for r in RULESETS if r.effective_from <= entry_date]
    if not live:
        return None
    return max(live, key=lambda r: r.effective_from)


# --------------------------------------------------------------------------- #
# Simulation (§10.5). One variant → its raw exit legs.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Leg:
    """One simulated exit leg. Units derive on read from ``price`` × ``fraction``.

    ``fraction`` is the fraction of the *original* entry position this leg exits.
    ``price`` is ``None`` on a ``cap`` leg — the remaining position that never
    signalled an exit, recorded so the fraction is accountable but never priced
    (§10.8, never a pseudo-exit). ``limit_locked`` marks a bar whose
    ``open == high == low == close`` with volume — an unobtainable fill (§10.5).
    """

    date: str
    price: Optional[float]
    fraction: float
    trigger: str
    limit_locked: bool


@dataclass(frozen=True)
class VariantResult:
    """One variant's simulation: its trail/partial axes, status and exit legs."""

    variant: str
    trail: str
    partial: str
    status: str
    stopless: bool
    legs: Tuple[Leg, ...]

    @property
    def final_leg(self) -> Optional[Leg]:
        return self.legs[-1] if self.legs else None

    def leg(self, trigger: str) -> Optional[Leg]:
        for leg in self.legs:
            if leg.trigger == trigger:
                return leg
        return None


def _limit_locked(bar: Bar) -> bool:
    """``open == high == low == close`` with volume — a limit lock (§10.5).

    On IDX essentially only a limit lock produces that signature, so this needs
    no band table, no prior close and no IPO exceptions.
    """
    return (
        bar.open == bar.high == bar.low == bar.close
        and bar.volume > 0
    )


def _ma_at(closes: Sequence[float], idx: int, n: int) -> Optional[float]:
    """SMA of the ``n`` closes ending at global index ``idx`` inclusive.

    ``None`` when fewer than ``n`` completed bars are available — a trail signal
    cannot fire against a moving average that does not yet exist.
    """
    if idx + 1 < n:
        return None
    return sum(closes[idx + 1 - n:idx + 1]) / n


def simulate(
    bars: Sequence[Bar],
    *,
    entry_date: str,
    stop: Optional[float],
    trail: str,
    partial: str,
    ruleset: RulesetVersion = RULESET_V1,
) -> VariantResult:
    """Simulate one variant over ``bars`` from ``entry_date``, returning its legs.

    ``bars`` is the symbol's trading-day series oldest first, spanning enough
    pre-entry history to seat the trail MA plus the forward window. The walk is
    day-by-day from the entry bar (day 1):

    * a **pending trail** signal from the prior close fills at this day's open;
    * the **stop** is checked intraday and wins over a same-day trail signal;
    * a **scheduled partial** fills at this day's close on its band day;
    * a **close below MA_N** arms the trail for the next open;
    * the **60-trading-day cap** records the remaining position as a null-priced
      ``cap`` leg (never a fabricated exit).
    """
    closes = [b.close for b in bars]
    entry_idx = next((i for i, b in enumerate(bars) if b.date >= entry_date), None)
    stopless = stop is None
    n = TRAIL_MA[trail]
    partial_day = PARTIAL_DAY.get(partial)
    cap = ruleset.cap_trading_days

    legs: List[Leg] = []
    remaining = 1.0
    partial_taken = False
    pending_trail = False
    status = PENDING

    if entry_idx is None:
        return VariantResult(f"{trail}/{partial}", trail, partial, PENDING,
                             stopless, ())

    for idx in range(entry_idx, len(bars)):
        bar = bars[idx]
        day = idx - entry_idx + 1

        # A trail signal armed by the prior close fills at this open (§10.5) — the
        # earliest transactable price, and it is honoured even past the cap day
        # because the *signal* fired within the window.
        if pending_trail:
            legs.append(Leg(bar.date, bar.open, remaining, TRIGGER_TRAIL,
                            _limit_locked(bar)))
            status = RESOLVED
            break

        # Sixty trading days elapsed with nothing signalled: cap, never a
        # pseudo-exit (§10.8). The remaining fraction is recorded at a null price.
        if day > cap:
            legs.append(Leg(bars[idx - 1].date, None, remaining, TRIGGER_CAP,
                            False))
            status = CAPPED
            break

        # The stop is a hard leg on every variant (§10.5). Gapped through → the
        # open; pierced intraday → the stop price. It wins over a same-day trail.
        if stop is not None:
            if bar.open < stop:
                legs.append(Leg(bar.date, bar.open, remaining, TRIGGER_STOP,
                                _limit_locked(bar)))
                status = RESOLVED
                break
            if bar.low <= stop:
                legs.append(Leg(bar.date, stop, remaining, TRIGGER_STOP,
                                _limit_locked(bar)))
                status = RESOLVED
                break

        # A scheduled partial fills at the close of its band day (§10.5).
        if (partial_day is not None and day == partial_day and not partial_taken):
            frac = min(ruleset.partial_fraction, remaining)
            legs.append(Leg(bar.date, bar.close, frac, TRIGGER_PARTIAL,
                            _limit_locked(bar)))
            remaining -= frac
            partial_taken = True

        # A close below MA_N arms the trail for the next open (§10.5).
        ma = _ma_at(closes, idx, n)
        if ma is not None and bar.close < ma:
            pending_trail = True

    return VariantResult(f"{trail}/{partial}", trail, partial, status, stopless,
                         tuple(legs))


def simulate_all(
    bars: Sequence[Bar],
    *,
    entry_date: str,
    stop: Optional[float],
    ruleset: RulesetVersion = RULESET_V1,
) -> List[VariantResult]:
    """Simulate all six variants; every one carries the recorded stop (§10.5)."""
    return [
        simulate(bars, entry_date=entry_date, stop=stop, trail=t, partial=p,
                 ruleset=ruleset)
        for t in TRAILS
        for p in PARTIALS
    ]


# --------------------------------------------------------------------------- #
# The actual side (§10.7). Never repriced — real fills read off ``trade_exit``.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ActualExit:
    exit_date: str
    quantity: float
    price: float
    reason: Optional[str]


def _day_number(bars: Sequence[Bar], entry_date: str, target: str) -> Optional[int]:
    """Trading-day number of ``target`` counting the entry day as day 1.

    ``None`` when ``target`` falls before the entry or on no trading day in the
    series. Used to place the actual exits on the same trading-day axis the
    variants are simulated on.
    """
    if target < entry_date:
        return None
    window = [b.date for b in bars if entry_date <= b.date <= target]
    return len(window) or None


def _exit_path(final_reason: Optional[str]) -> str:
    """Map the final exit's confirmed reason to an ``exit_path`` (§10.7)."""
    if final_reason == "stop_hit":
        return EXIT_STOP_HIT
    if final_reason in ("close_below_ma10", "close_below_ma20"):
        return EXIT_TRAIL
    if final_reason == "discretionary":
        return EXIT_DISCRETIONARY
    return EXIT_OTHER


# --------------------------------------------------------------------------- #
# The Trade-level result (§10.7): deltas against the nominal variant, the
# actual-side descriptors, and the full six-way fit vector.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TradeCounterfactual:
    """Everything one closed Trade carries after the engine runs (§10.7).

    Deltas are signed and measured against the nominal variant; the fit vector
    ranges over all six. ``deviation_cost`` is derived on read from the nominal
    variant's raw legs rather than stored as R, so a no-stop Trade still reads a
    cost in cash (and % / ADR downstream).
    """

    trade_id: Optional[int]
    book: str
    symbol: str
    entry_date: str
    entry_qty: float
    entry_avg_price: float
    stop: Optional[float]
    stop_provenance: Optional[str]
    ruleset_version: Optional[str]
    nominal_variant: str
    stopless: bool
    partial_state: str
    partial_timing_delta: Optional[int]
    actual_partial_fraction: Optional[float]
    exit_path: str
    trail_exit_delta: Optional[int]
    nominal_status: str
    fit_vector: Dict[str, int]
    variants: Tuple[VariantResult, ...]
    actual_exits: Tuple[ActualExit, ...]

    @property
    def excluded_from_cross_trade_aggregate(self) -> bool:
        """No-stop Trades stay out of cross-Trade aggregates, as they do from R (§10.5)."""
        return self.stopless

    def _nominal(self) -> Optional[VariantResult]:
        for v in self.variants:
            if v.variant == self.nominal_variant:
                return v
        return None

    def _actual_proceeds_per_share(self) -> Optional[float]:
        if not self.actual_exits or not self.entry_qty:
            return None
        return sum(e.quantity * e.price for e in self.actual_exits) / self.entry_qty

    def deviation_cost(self) -> Optional[float]:
        """Nominal variant's outcome − actual outcome, in cash (§10.7).

        Null when the nominal variant is ``capped`` (§10.8) — a synthetic number
        inside an aggregate is not recoverable. The shared entry cost cancels in
        the difference, so this is the per-share proceeds gap times ``entry_qty``.
        """
        nominal = self._nominal()
        if nominal is None or nominal.status == CAPPED:
            return None
        actual_pps = self._actual_proceeds_per_share()
        if actual_pps is None:
            return None
        nominal_pps = sum(
            leg.fraction * leg.price
            for leg in nominal.legs
            if leg.price is not None
        )
        return (nominal_pps - actual_pps) * self.entry_qty

    def deviation_cost_r(self) -> Optional[float]:
        """The R form of :meth:`deviation_cost`, off the recorded stop (§10.8).

        Null in three further cases beyond the cap: no stop (no R denominator), a
        **limit-locked** nominal leg (a fill nobody could have obtained), or a
        **mismatched ex-date crossing** between the actual and nominal windows (the
        rule would read as outperforming when it merely dodged a dividend). A flag
        can be read past; nulling cannot (§10.8).
        """
        cost = self.deviation_cost()
        if cost is None or self.stop is None:
            return None
        denom = self.entry_avg_price - self.stop
        if denom == 0:
            return None
        nominal = self._nominal()
        if nominal is not None and any(leg.limit_locked for leg in nominal.legs):
            return None
        if self._ex_date_mismatch():
            return None
        return cost / denom / self.entry_qty

    def _ex_date_mismatch(self) -> bool:
        """True when the nominal and actual windows cross different ex-dates (§10.8)."""
        nominal = self._nominal()
        if nominal is None or nominal.final_leg is None:
            return False
        if not self.actual_exits:
            return False
        actual_end = max(e.exit_date for e in self.actual_exits)
        nominal_end = nominal.final_leg.date
        return self._dividends_in(actual_end) != self._dividends_in(nominal_end)

    # ``_dividend_dates`` is populated by :func:`compute_trade` from the bars so
    # the ex-date comparison needs no second read.
    _dividend_dates: Tuple[str, ...] = ()

    def _dividends_in(self, end_date: str) -> frozenset:
        return frozenset(
            d for d in self._dividend_dates if self.entry_date < d <= end_date
        )

    def best_fit(self) -> Optional[str]:
        """The variant with the smallest trading-day distance; ties → nominal (§10.7)."""
        if not self.fit_vector:
            return None
        best = min(
            self.fit_vector,
            key=lambda v: (self.fit_vector[v], v != self.nominal_variant),
        )
        return best


def _partial_state(
    bars: Sequence[Bar],
    entry_date: str,
    actual_exits: Sequence[ActualExit],
) -> Tuple[str, Optional[int]]:
    """Grade the actual trade's partial timing against the band (§10.7).

    **Absence is not deviation** — a Trade whose final exit precedes the band
    (day 3) is ``not_applicable`` with a null delta, never a number. A partial
    inside days 3–5 is ``in_band``; before it ``early``, after it ``late``, with
    the signed distance to the nearer band edge; a single-exit Trade that reached
    the band is ``none``.
    """
    if not actual_exits:
        return NOT_APPLICABLE, None
    ordered = sorted(actual_exits, key=lambda e: e.exit_date)
    final_day = _day_number(bars, entry_date, ordered[-1].exit_date)
    if final_day is None or final_day < BAND[0]:
        # Stopped out before ever reaching the partial window (§10.7).
        return NOT_APPLICABLE, None

    # A partial is any exit that is not the final closing one — the trader scaled.
    partials = ordered[:-1]
    if not partials:
        return NONE, None
    first_day = _day_number(bars, entry_date, partials[0].exit_date)
    if first_day is None:
        return NONE, None
    if BAND[0] <= first_day <= BAND[1]:
        return IN_BAND, None
    if first_day < BAND[0]:
        return EARLY, first_day - BAND[0]      # negative: days before the near edge
    return LATE, first_day - BAND[1]           # positive: days after the far edge


def _actual_partial_fraction(
    actual_exits: Sequence[ActualExit], entry_qty: float
) -> Optional[float]:
    """Fraction of the position scaled out before the final exit (§10.7).

    Descriptive, carried without a delta — the real band is wide and size
    deviation is second-order next to timing. ``None`` with no quantity to divide.
    """
    if not actual_exits or not entry_qty:
        return None
    ordered = sorted(actual_exits, key=lambda e: e.exit_date)
    scaled = sum(e.quantity for e in ordered[:-1])
    return scaled / entry_qty


def _fit_vector(
    variants: Sequence[VariantResult],
    bars: Sequence[Bar],
    entry_date: str,
    actual_exits: Sequence[ActualExit],
) -> Dict[str, int]:
    """Sum the absolute trading-day distance per variant — partial and final (§10.7).

    Fit is scored by *behaviour in trading days, not outcome*: unlike rules
    routinely coincide on P&L. For each variant the distance is the trading-day
    gap between its final leg and the actual final exit, plus the partial gap when
    **both** sides took a partial (timing is what the fixed-fraction band isolates).
    The full vector across all six is stored; best-fit derives at read time.
    """
    if not actual_exits:
        return {v.variant: 0 for v in variants}
    ordered = sorted(actual_exits, key=lambda e: e.exit_date)
    actual_final_day = _day_number(bars, entry_date, ordered[-1].exit_date)
    actual_partials = ordered[:-1]
    actual_partial_day = (
        _day_number(bars, entry_date, actual_partials[0].exit_date)
        if actual_partials else None
    )

    out: Dict[str, int] = {}
    for v in variants:
        final = v.final_leg
        v_final_day = (
            _day_number(bars, entry_date, final.date) if final else None
        )
        distance = 0
        if v_final_day is not None and actual_final_day is not None:
            distance += abs(v_final_day - actual_final_day)
        v_partial = v.leg(TRIGGER_PARTIAL)
        v_partial_day = PARTIAL_DAY.get(v.partial) if v_partial else None
        if v_partial_day is not None and actual_partial_day is not None:
            distance += abs(v_partial_day - actual_partial_day)
        out[v.variant] = distance
    return out


def compute_book(
    conn: sqlite3.Connection, book: str
) -> List[TradeCounterfactual]:
    """Run the engine over every **closed** Trade on ``book`` (SPEC §10.9).

    Closed Trades only, never open ones — a counterfactual against an unfinished
    position has no actual to compare against. Each Trade's symbol series is read
    whole from the bar cache so the trail MA is seated and the forward window
    reaches the 60-trading-day cap; a Trade whose bars are not cached yet is
    skipped, to be picked up on a later daily run once the cache is filled.
    """
    rows = conn.execute(
        "SELECT id, symbol FROM trade WHERE book = ? AND status = 'closed' "
        "ORDER BY entry_date, id",
        (book,),
    ).fetchall()
    out: List[TradeCounterfactual] = []
    for row in rows:
        bars = _read_bars(conn, book, row["symbol"])
        if not bars:
            continue
        out.append(compute_trade(conn, row["id"], bars))
    return out


def _read_bars(conn: sqlite3.Connection, book: str, symbol: str) -> List[Bar]:
    """The symbol's whole cached trading-day series, oldest first (§4.4).

    Reads the ``bar`` cache directly rather than through :class:`~journal.bars.
    BarCache` — the engine only ever *reads* here (the daily job fills the cache
    upstream), so no fetcher is needed.
    """
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume, dividend FROM bar "
        "WHERE book = ? AND symbol = ? ORDER BY date",
        (book, symbol),
    ).fetchall()
    return [
        Bar(date=r["date"], open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r["volume"], dividend=r["dividend"])
        for r in rows
    ]


def compute_trade(
    conn: sqlite3.Connection, trade_id: int, bars: Sequence[Bar]
) -> TradeCounterfactual:
    """Run the engine for one closed Trade against its symbol's ``bars``.

    Reads the Trade row and its confirmed exits, simulates all six variants with
    the recorded stop as a hard leg, grades the actual side against the band and
    builds the six-way fit vector. Never reprices the actual fills.
    """
    trade = conn.execute(
        "SELECT id, book, symbol, entry_date, entry_qty, entry_avg_price, stop, "
        "stop_provenance FROM trade WHERE id = ?",
        (trade_id,),
    ).fetchone()
    if trade is None:
        raise ValueError(f"no Trade with id {trade_id}")
    rows = conn.execute(
        "SELECT exit_date, quantity, price, reason FROM trade_exit "
        "WHERE trade_id = ? ORDER BY exit_date, id",
        (trade_id,),
    ).fetchall()
    actual_exits = tuple(
        ActualExit(r["exit_date"], r["quantity"], r["price"], r["reason"])
        for r in rows
    )
    return _assemble(
        trade_id=trade["id"], book=trade["book"], symbol=trade["symbol"],
        entry_date=trade["entry_date"], entry_qty=trade["entry_qty"],
        entry_avg_price=trade["entry_avg_price"], stop=trade["stop"],
        stop_provenance=trade["stop_provenance"], bars=bars,
        actual_exits=actual_exits,
    )


def _assemble(
    *, trade_id, book, symbol, entry_date, entry_qty, entry_avg_price, stop,
    stop_provenance, bars, actual_exits,
) -> TradeCounterfactual:
    ruleset = ruleset_for(entry_date)
    active = ruleset or RULESET_V1
    variants = simulate_all(bars, entry_date=entry_date, stop=stop, ruleset=active)
    nominal_variant = active.nominal_variant
    nominal = next((v for v in variants if v.variant == nominal_variant), None)
    nominal_status = nominal.status if nominal else PENDING

    partial_state, timing_delta = _partial_state(bars, entry_date, actual_exits)

    # trail_exit_delta: actual final exit − nominal variant's exit, in trading
    # days (negative = exited early). Null when the nominal variant capped — no
    # exit was ever signalled to compare against (§10.8).
    trail_exit_delta: Optional[int] = None
    if nominal is not None and nominal.status == RESOLVED and actual_exits:
        ordered = sorted(actual_exits, key=lambda e: e.exit_date)
        actual_final_day = _day_number(bars, entry_date, ordered[-1].exit_date)
        nominal_final = nominal.final_leg
        nominal_final_day = (
            _day_number(bars, entry_date, nominal_final.date)
            if nominal_final else None
        )
        if actual_final_day is not None and nominal_final_day is not None:
            trail_exit_delta = actual_final_day - nominal_final_day

    fit_vector = _fit_vector(variants, bars, entry_date, actual_exits)
    final_reason = (
        sorted(actual_exits, key=lambda e: e.exit_date)[-1].reason
        if actual_exits else None
    )

    tc = TradeCounterfactual(
        trade_id=trade_id, book=book, symbol=symbol, entry_date=entry_date,
        entry_qty=entry_qty, entry_avg_price=entry_avg_price, stop=stop,
        stop_provenance=stop_provenance,
        ruleset_version=ruleset.version if ruleset else None,
        nominal_variant=nominal_variant, stopless=stop is None,
        partial_state=partial_state, partial_timing_delta=timing_delta,
        actual_partial_fraction=_actual_partial_fraction(actual_exits, entry_qty),
        exit_path=_exit_path(final_reason),
        trail_exit_delta=trail_exit_delta, nominal_status=nominal_status,
        fit_vector=fit_vector, variants=tuple(variants), actual_exits=actual_exits,
    )
    object.__setattr__(
        tc, "_dividend_dates",
        tuple(b.date for b in bars if getattr(b, "dividend", 0)),
    )
    return tc


# --------------------------------------------------------------------------- #
# R and the three tiers (§10.6). Every aggregate reports its excluded count.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TradeR:
    """One Trade's realized R and the two inputs that tier it (§10.6)."""

    realized_r: Optional[float]
    stop: Optional[float]
    provenance: Optional[str]      # 'recorded' | 'reconstructed' | None


def realized_r(
    entry_avg_price: float, exit_avg_price: Optional[float], stop: Optional[float]
) -> Optional[float]:
    """``(exit − entry) / (entry − stop)`` off the stop as recorded (§10.6).

    ``None`` without a stop (no denominator) or without an exit price.
    """
    if stop is None or exit_avg_price is None:
        return None
    denom = entry_avg_price - stop
    if denom == 0:
        return None
    return (exit_avg_price - entry_avg_price) / denom


@dataclass(frozen=True)
class RAggregate:
    """An R aggregate with every excluded count reported beside it (§10.6).

    ``scope='r'`` includes both recorded and ``reconstructed`` stops; the
    ``adherence`` scope additionally excludes ``reconstructed`` — a stop entered
    after an exit is contaminated by hindsight and cannot score discipline. Either
    way an absent stop excludes the Trade. Nothing drops silently.
    """

    scope: str
    values: Tuple[float, ...]
    included: int
    excluded_no_stop: int
    excluded_reconstructed: int

    @property
    def mean(self) -> Optional[float]:
        return sum(self.values) / len(self.values) if self.values else None

    @property
    def excluded_total(self) -> int:
        return self.excluded_no_stop + self.excluded_reconstructed


def r_aggregate(trades: Sequence[TradeR], *, scope: str = "r") -> RAggregate:
    """Fold per-Trade R into one aggregate under the three tiers (§10.6).

    ``scope='r'``: absent → excluded, ``reconstructed`` → included, recorded →
    included. ``scope='adherence'``: absent → excluded, ``reconstructed`` →
    excluded, recorded → included. Each excluded bucket is counted; the caller
    must never report the mean without them.
    """
    if scope not in ("r", "adherence"):
        raise ValueError(f"unknown scope {scope!r}")
    values: List[float] = []
    no_stop = reconstructed = 0
    for t in trades:
        if t.stop is None or t.realized_r is None:
            no_stop += 1
        elif scope == "adherence" and t.provenance == "reconstructed":
            reconstructed += 1
        else:
            values.append(t.realized_r)
    return RAggregate(
        scope=scope, values=tuple(values), included=len(values),
        excluded_no_stop=no_stop, excluded_reconstructed=reconstructed,
    )


# --------------------------------------------------------------------------- #
# Storage. The Trade-level deltas in one table; the per-variant raw legs in a
# second (one row per variant), legs serialised so units derive on read (§10.7).
# --------------------------------------------------------------------------- #


_TRADE_COLUMNS = (
    "trade_id", "book", "symbol", "entry_date", "entry_qty", "entry_avg_price",
    "stop", "stop_provenance", "ruleset_version", "nominal_variant", "stopless",
    "partial_state", "partial_timing_delta", "actual_partial_fraction",
    "exit_path", "trail_exit_delta", "nominal_status", "fit_vector",
)

_TRADE_UPDATE = tuple(c for c in _TRADE_COLUMNS if c != "trade_id")

_TRADE_UPSERT = f"""
INSERT INTO trade_counterfactual ({", ".join(_TRADE_COLUMNS)})
VALUES ({", ".join("?" for _ in _TRADE_COLUMNS)})
ON CONFLICT(trade_id) DO UPDATE SET
{", ".join(f"{c}=excluded.{c}" for c in _TRADE_UPDATE)}
"""

_VARIANT_UPSERT = """
INSERT INTO counterfactual_variant
    (trade_id, variant, trail, partial, status, stopless, legs)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(trade_id, variant) DO UPDATE SET
    trail=excluded.trail, partial=excluded.partial, status=excluded.status,
    stopless=excluded.stopless, legs=excluded.legs
"""


def _legs_to_json(legs: Sequence[Leg]) -> str:
    return json.dumps([
        {"date": l.date, "price": l.price, "fraction": l.fraction,
         "trigger": l.trigger, "limit_locked": l.limit_locked}
        for l in legs
    ])


def _legs_from_json(packed: str) -> Tuple[Leg, ...]:
    return tuple(
        Leg(d["date"], d["price"], d["fraction"], d["trigger"],
            bool(d["limit_locked"]))
        for d in json.loads(packed)
    )


class CounterfactualStore:
    """Persist and read a :class:`TradeCounterfactual` (§10.7, §10.9).

    Idempotent upserts recompute-in-place, so the daily job re-runs a closed
    Trade until every variant is ``resolved`` or ``capped`` without ever
    duplicating a row. A variant resolving after freeze fills in here without
    counting as drift (§10.9). The actual fills are not stored — they live on
    ``trade_exit`` and are never repriced — so :meth:`get` reads them back from
    there via :func:`compute_trade`'s companion loader when needed.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, trade_id: int, tc: TradeCounterfactual) -> None:
        self.conn.execute(
            _TRADE_UPSERT,
            (
                trade_id, tc.book, tc.symbol, tc.entry_date, tc.entry_qty,
                tc.entry_avg_price, tc.stop, tc.stop_provenance,
                tc.ruleset_version, tc.nominal_variant, 1 if tc.stopless else 0,
                tc.partial_state, tc.partial_timing_delta,
                tc.actual_partial_fraction, tc.exit_path, tc.trail_exit_delta,
                tc.nominal_status, json.dumps(tc.fit_vector),
            ),
        )
        for v in tc.variants:
            self.conn.execute(
                _VARIANT_UPSERT,
                (trade_id, v.variant, v.trail, v.partial, v.status,
                 1 if v.stopless else 0, _legs_to_json(v.legs)),
            )
        self.conn.commit()

    def get(self, trade_id: int) -> Optional[TradeCounterfactual]:
        row = self.conn.execute(
            "SELECT * FROM trade_counterfactual WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        if row is None:
            return None
        vrows = self.conn.execute(
            "SELECT * FROM counterfactual_variant WHERE trade_id = ? "
            "ORDER BY variant", (trade_id,)
        ).fetchall()
        variants = tuple(
            VariantResult(
                vr["variant"], vr["trail"], vr["partial"], vr["status"],
                bool(vr["stopless"]), _legs_from_json(vr["legs"]),
            )
            for vr in vrows
        )
        erows = self.conn.execute(
            "SELECT exit_date, quantity, price, reason FROM trade_exit "
            "WHERE trade_id = ? ORDER BY exit_date, id", (trade_id,)
        ).fetchall()
        actual_exits = tuple(
            ActualExit(e["exit_date"], e["quantity"], e["price"], e["reason"])
            for e in erows
        )
        return TradeCounterfactual(
            trade_id=trade_id, book=row["book"], symbol=row["symbol"],
            entry_date=row["entry_date"], entry_qty=row["entry_qty"],
            entry_avg_price=row["entry_avg_price"], stop=row["stop"],
            stop_provenance=row["stop_provenance"],
            ruleset_version=row["ruleset_version"],
            nominal_variant=row["nominal_variant"], stopless=bool(row["stopless"]),
            partial_state=row["partial_state"],
            partial_timing_delta=row["partial_timing_delta"],
            actual_partial_fraction=row["actual_partial_fraction"],
            exit_path=row["exit_path"], trail_exit_delta=row["trail_exit_delta"],
            nominal_status=row["nominal_status"],
            fit_vector=json.loads(row["fit_vector"]),
            variants=variants, actual_exits=actual_exits,
        )
