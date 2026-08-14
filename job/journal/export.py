"""The LLM export — curated JSONL, a legend that always ships, and aggregates
(SPEC §12, #41).

One JSON object per Trade per line, normalized to R and ADR so the two books sit
in one file and compare directly (SPEC §12.1). This is the **only** surface where
setup selection, sizing and regime can be answered at all (SPEC §11.2.1), so it
is load-bearing, not complementary — it is not a diagnostic dump of every field.

The curation is the point (SPEC §12.2):

* **Exactly two price levels ship** — ``entry_avg_price`` and ``stop``. Enough to
  answer a price question and to let the model check its own R arithmetic; not
  enough to tempt it into cross-book currency maths. The **equity level never
  ships**; Risk % and Exposure % do.
* **The six-variant adherence table does not ship.** Five curated fields carry it:
  ``best_fit_variant``, ``best_variant_r``, ``partial_state``,
  ``trail_exit_delta_days`` and ``deviation_cost_r``. Shipping all six buys
  narration, not insight, and is the largest per-Trade line item.
* **``capture_ratio`` is null unless the Trade both went in favour and finished in
  profit** — a stopped-out Trade computes to a large negative and indicts a correct
  *exit* for what was an entry mistake. A ratio that indicts the wrong decision is
  worse than no ratio.
* **``dividend_drag_r`` is omitted entirely when null**, with no percentile.
* **Five within-export percentiles**, each rendered immediately after the field it
  ranks — ``_pctile`` fields rank *within this export*, not against any absolute
  scale (SPEC §12.3).
* **The free-text ``note`` stays on the row** — segregating it breaks the
  one-object-per-Trade property that makes the export filterable.

``seq`` and ``book_drawdown_r_at_entry`` come from
:mod:`journal.book_history`: both are **absolute**, computed against the book's
**complete** history, unlike the export-relative percentiles (SPEC §12.3). ``seq``
gaps in a sliced export **stay unfilled and uncompacted** — a model that can see
rows are missing will hedge, which is correct.

**One book per export** (SPEC §12.4): a two-book export would put two incomparable
drawdown curves in one column. ``book`` stays on every row, so a deliberate
normalized cross-book read stays legal, but this door does one book at a time.
"""

from __future__ import annotations

import io
import json
import sqlite3
from typing import Dict, List, Optional, Sequence, Tuple

from . import book_history, books
from .counterfactual import RESOLVED, CounterfactualStore, realized_r as cf_realized_r
from .enrichment import EnrichmentStore
from .exit_enrichment import ExcursionStore, ExitGeometryStore
from .post_exit import PostExitStore
from .regime import RegimeStore
from . import risk as risk_mod

# stop_distance_adr > 1.0 is "chased" (SPEC §5.5); gradeable only on a *recorded*
# stop — a stop remembered after the outcome cannot indict the entry.
CHASE_ADR = 1.0

# The five within-export percentiles (SPEC §12.2). Each is rendered immediately
# after the field it ranks; the legend states they are export-relative.
PCTILE_FIELDS: Tuple[str, ...] = (
    "stop_distance_adr",
    "entry_ma_dist_10_adr",
    "entry_move_63d_pct",
    "exposure_pct_of_equity",
    "days_held",
)


LEGEND = """\
# Trading journal export — field legend (always ships; the only place the caveats live).

Units. `_adr` fields are multiples of the symbol's Average Daily Range (its typical
daily percent range), so a $400 US name and an IDR 7,200 IDX name are comparable.
`_r` fields are multiples of the risk taken (entry to stop). `_pct` fields are
percent. Prices are in the book's own currency and never converted. Only two price
levels ship — `entry_avg_price` and `stop`; the equity level never ships, but Risk %
and Exposure % do.

Percentiles. The five `_pctile` fields rank a trade against THE OTHER TRADES IN THIS
EXPORT, not against any absolute scale and not against the full history. Slice the
export differently and the same trade gets a different percentile. Use them to say
"this entry was extended for this trader", never "this entry was extended".

Absolute numbers. Everything that is NOT a `_pctile` is absolute — computed against
the book's complete history and unmoved by how the export is sliced. In particular
`book_drawdown_r_at_entry` (peak-to-here of the book's realized-R curve, read at the
entry day) is absolute; do not read it as export-relative the way a percentile is.

Sequence. `seq` is the 1-based ordinal of the trade on its book by entry date, over
the book's COMPLETE history. In a sliced export its values have GAPS — a gap means
rows are missing, not that nothing was traded; rows adjacent in the file are not
adjacent in time. Gaps are left uncompacted on purpose. Sequence questions
(prior-trade outcome, streaks, trades-open-at-entry) are answerable by ordering on
`seq` within `book`; there are NO precomputed prior-trade fields, by design, and any
you derive should be stated as derived.

Anchors. Entry-dated geometry is as of the PRIOR trading day's close — the last bar
that existed when the trade was decided. Exit geometry is as of the exit day's own
close, because the exit rule is triggered by a close. This asymmetry is deliberate.

Nulls. `null` means the value could not exist (not enough price history — a recent
listing has no MA200), never that data is missing. `not_applicable` on an adherence
field means the rule never got a chance to fire (stopped out before the partial
window), which is NOT the same as a deviation.

Caveats you must respect.
- Trades with `stop_provenance: reconstructed` have a stop remembered after the
  outcome was known. Exclude them from any conclusion about chasing, risk sizing,
  or R. They are marked, not hidden.
- The two books never aggregate. No FX, no combined equity curve. Compare within a
  book, or compare normalized (`_adr`, `_r`) values across books.
- There is no recorded plan. `setup` and `stop` are the only judgements the trader
  entered; everything else is measured after the fact. Do not infer intent.
- This journal records only trades that were TAKEN. There is no record of setups
  passed on. Every conclusion about setup selection is therefore conditional on the
  trader's own filter — you can say which of the taken setups worked, never which
  setups work. Do not present the former as the latter.
- `book_drawdown_r_at_entry` is `insufficient_history` (shipped as null) below the
  minimum closed stop-bearing trade count — that is NOT a drawdown of zero. The
  curve also understates any stretch that contains no-stop trades, since a no-stop
  trade carries no R; the count excluded from each book's curve is in the header.
- `dividend_drag_r` is OMITTED (not null) on the ~88% of rows whose window crossed
  no ex-date; where present it is dividend drag in R, sitting beside R, never folded
  in. `deviation_cost_r` is null when the nominal variant was capped and when a
  limit-locked leg or a mismatched ex-date crossing makes the cost unrecoverable.
- Sample sizes are given as `n` in the baseline block. Do not report a finding on a
  subgroup without stating its `n`, and treat anything under ~20 as anecdote.

Adherence. Every trade is scored against all six mechanical variants
(trail {MA10, MA20} x partial {none, day 3, day 5}); `best_fit_variant` is the one
the trade's behaviour most resembled, derived — not something the trader declared.
`nominal_variant` is what the ruleset in force on the entry date called for.
"""


# ── small read-side derivations (SPEC §6.4) ──


def _r_level(level: Optional[float], entry: float, stop: Optional[float]) -> Optional[float]:
    """A price level expressed in R, off entry and the recorded stop.

    ``None`` without a stop, without the level, or on a zero denominator — the
    same rule the excursion and counterfactual R forms use.
    """
    if level is None or stop is None:
        return None
    denom = entry - stop
    if denom == 0:
        return None
    return (level - entry) / denom


def _adr_level(level: Optional[float], entry: float, adr_pct: Optional[float]) -> Optional[float]:
    """A price *distance* from entry expressed in ADR multiples."""
    if level is None or adr_pct is None or adr_pct == 0 or entry == 0:
        return None
    return (level - entry) / entry * 100 / adr_pct


def _trading_days(conn: sqlite3.Connection, book: str, symbol: str,
                  start: str, end: Optional[str]) -> Optional[int]:
    """Trading days held: cached bars in ``[start, end]`` inclusive.

    ``None`` when nothing is cached (days-held cannot be stated honestly without
    the trading-day axis). ``end`` is the final exit for a closed Trade, else the
    last cached bar for an open one.
    """
    if end is None:
        return None
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM bar WHERE book=? AND symbol=? AND date>=? AND date<=?",
        (book, symbol, start, end),
    ).fetchone()["n"]
    return n or None


def _capture_ratio(realized: Optional[float], mfe_r: Optional[float]) -> Optional[float]:
    """``realized_r / mfe_r``, null unless the Trade both went in favour AND
    finished in profit (SPEC §12.2).

    The null is the point: a Trade with a little available and a loss taken
    computes to a large negative that reads as a catastrophic *exit* when the
    mistake was the entry.
    """
    if mfe_r is None or mfe_r <= 0:
        return None
    if realized is None or realized <= 0:
        return None
    return realized / mfe_r


def _best_variant_r(tc, entry: float, stop: Optional[float]) -> Optional[float]:
    """The best R any of the six variants would have realized.

    Each resolved variant's proceeds-per-share is ``sum(fraction × price)`` over
    its priced legs (a resolved variant's fractions sum to the whole position); a
    capped variant has an unpriced residual and cannot be scored, so it drops.
    ``None`` without a stop — R has no denominator (SPEC §10.6).
    """
    best: Optional[float] = None
    for v in tc.variants:
        if v.status != RESOLVED:
            continue
        pps = sum(leg.fraction * leg.price for leg in v.legs if leg.price is not None)
        r = cf_realized_r(entry, pps, stop)
        if r is None:
            continue
        best = r if best is None else max(best, r)
    return best


# ── the per-Trade row ──


class _Stores:
    """The read-side stores, opened once per export rather than per Trade."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.enrich = EnrichmentStore(conn)
        self.geom = ExitGeometryStore(conn)
        self.exc = ExcursionStore(conn)
        self.post = PostExitStore(conn)
        self.cf = CounterfactualStore(conn)
        self.regime = RegimeStore(conn)


def _exit_rows(conn: sqlite3.Connection, trade_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT id, exit_date, quantity, price, reason FROM trade_exit "
        "WHERE trade_id=? ORDER BY exit_date, id",
        (trade_id,),
    ).fetchall()


def _row(conn: sqlite3.Connection, t: sqlite3.Row, s: _Stores,
         bh: book_history.BookHistoryRow) -> Dict:
    """Build one Trade's curated object (before percentiles and rounding)."""
    tid = t["id"]
    book = t["book"]
    symbol = t["symbol"]
    entry = t["entry_avg_price"]
    stop = t["stop"]

    exits = _exit_rows(conn, tid)
    final_exit_date = exits[-1]["exit_date"] if exits else None

    # Exit-side average price → Realized R (SPEC §10.6). Prefer the stored
    # quantity-weighted mean; fall back to weighting the raw exit fills.
    geom = s.geom.get(tid)
    exit_avg = geom.exit_avg_price if geom else None
    if exit_avg is None and exits:
        qty = sum(e["quantity"] for e in exits)
        exit_avg = sum(e["quantity"] * e["price"] for e in exits) / qty if qty else None

    realized_r = cf_realized_r(entry, exit_avg, stop)
    realized_pct = (exit_avg / entry - 1) * 100 if exit_avg is not None and entry else None

    enr = s.enrich.get(tid)
    adr_pct = enr.adr_pct if enr else None

    # stop_distance_adr and chased — no stored column; derived here (SPEC §5.5).
    stop_distance_adr = (
        _adr_level(stop, entry, adr_pct) if stop is not None else None
    )
    if stop_distance_adr is not None:
        # (entry − stop)/entry is positive; _adr_level of the stop is negative, so
        # the distance is its magnitude.
        stop_distance_adr = -stop_distance_adr
    chased: Optional[bool] = None
    if stop_distance_adr is not None and t["stop_provenance"] == "recorded":
        chased = stop_distance_adr > CHASE_ADR

    re = risk_mod.compute_for_trade(conn, tid)

    entry_regime_snap = s.regime.get(book, t["entry_date"])
    entry_regime = entry_regime_snap.label if entry_regime_snap else None
    exit_regime = None
    if final_exit_date is not None:
        exit_regime_snap = s.regime.get(book, final_exit_date)
        exit_regime = exit_regime_snap.label if exit_regime_snap else None

    exc = s.exc.get_trade(tid)
    mfe_r = exc.mfe_r(entry, stop) if exc else None
    mae_r = exc.mae_r(entry, stop) if exc else None
    mfe_day = exc.mfe_date if exc else None
    mae_day = exc.mae_date if exc else None

    post = s.post.get(tid)
    fwd_20d_pct = post.fwd_return_20d if post else None
    fwd_high_r = _r_level(post.fwd_high, entry, stop) if post else None

    tc = s.cf.get(tid)
    if tc is not None:
        nominal_variant = tc.nominal_variant
        best_fit_variant = tc.best_fit()
        partial_state = tc.partial_state
        trail_exit_delta_days = tc.trail_exit_delta
        deviation_cost_r = tc.deviation_cost_r()
        best_variant_r = _best_variant_r(tc, entry, stop)
        dividend_drag_r = tc.dividend_drag_r
    else:
        nominal_variant = best_fit_variant = partial_state = None
        trail_exit_delta_days = deviation_cost_r = best_variant_r = None
        dividend_drag_r = None

    days_held = _trading_days(
        conn, book, symbol, t["entry_date"],
        final_exit_date or _last_bar(conn, book, symbol, t["entry_date"]),
    )

    return {
        "id": tid,
        "book": book,
        "symbol": symbol,
        "seq": bh.seq,
        "entry_date": t["entry_date"],
        "exit_date": final_exit_date,
        "days_held": days_held,
        "setup": t["setup"],
        "stop_provenance": t["stop_provenance"],
        "book_drawdown_r_at_entry": bh.book_drawdown_r_at_entry,
        # price levels — the only two.
        "entry_avg_price": entry,
        "stop": stop,
        # outcome
        "realized_r": realized_r,
        "realized_pct": realized_pct,
        # sizing — the equity level never ships, but these two do.
        "risk_pct_of_equity": re.risk_percentage,
        "exposure_pct_of_equity": re.exposure_percentage,
        "stop_distance_adr": stop_distance_adr,
        "chased": chased,
        # entry context, normalized
        "entry_regime": entry_regime,
        "entry_ma_dist_10_adr": enr.ma_dist_10 if enr else None,
        "entry_ma_dist_50_adr": enr.ma_dist_50 if enr else None,
        "entry_ma_dist_200_adr": enr.ma_dist_200 if enr else None,
        "entry_stack": enr.stack_state if enr else None,
        "entry_move_63d_pct": enr.prior_move_63d if enr else None,
        "entry_off_52w_high_pct": enr.pct_off_52w_high if enr else None,
        "entry_rs_63d": enr.rs_63d if enr else None,
        "entry_volume_ratio": enr.volume_ratio if enr else None,
        "adr_pct": adr_pct,
        # exit context, normalized
        "exit_regime": exit_regime,
        "exit_ma_dist_10_adr": geom.ma_dist_10_at_exit if geom else None,
        "exit_ma_dist_50_adr": geom.ma_dist_50_at_exit if geom else None,
        # excursion, normalized
        "mfe_r": mfe_r,
        "mfe_day": mfe_day,
        "mae_r": mae_r,
        "mae_day": mae_day,
        "capture_ratio": _capture_ratio(realized_r, mfe_r),
        # forward window
        "fwd_20d_pct": fwd_20d_pct,
        "fwd_high_r": fwd_high_r,
        # adherence — five curated fields, never the six-variant table.
        "nominal_variant": nominal_variant,
        "best_fit_variant": best_fit_variant,
        "partial_state": partial_state,
        "trail_exit_delta_days": trail_exit_delta_days,
        "deviation_cost_r": deviation_cost_r,
        "best_variant_r": best_variant_r,
        # dividend_drag_r sits here but is OMITTED entirely when null (below).
        "dividend_drag_r": dividend_drag_r,
        # exits, compact — a variable-length array, one per leg.
        "exits": [
            {"d": e["exit_date"], "q": e["quantity"], "reason": e["reason"],
             "r": _r_level(e["price"], entry, stop)}
            for e in exits
        ],
        # qualitative — the free text stays on the row.
        "note": t["note"],
    }


def _last_bar(conn: sqlite3.Connection, book: str, symbol: str, start: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM bar WHERE book=? AND symbol=? AND date>=?",
        (book, symbol, start),
    ).fetchone()
    return row["d"] if row else None


# ── percentiles, rounding and emission ──


def _percentiles(records: Sequence[Dict]) -> List[Dict[str, Optional[int]]]:
    """Within-export rank 0-100 per record for each :data:`PCTILE_FIELDS`.

    Nulls do not rank and stay null; below two ranked values a percentile has no
    meaning and is null.
    """
    ranks: List[Dict[str, Optional[int]]] = [dict() for _ in records]
    for field in PCTILE_FIELDS:
        vals = sorted(r[field] for r in records if r.get(field) is not None)
        for i, r in enumerate(records):
            v = r.get(field)
            if v is None or len(vals) < 2:
                ranks[i][field] = None
            else:
                below = sum(1 for x in vals if x < v)
                ranks[i][field] = round(below / (len(vals) - 1) * 100)
    return ranks


def _round_floats(o):
    """Round every float to 2dp for a stable, readable line — ints untouched."""
    if isinstance(o, bool) or o is None:
        return o
    if isinstance(o, float):
        return round(o, 2)
    if isinstance(o, dict):
        return {k: _round_floats(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_round_floats(v) for v in o]
    return o


def _emit(record: Dict, ranks: Dict[str, Optional[int]]) -> Dict:
    """Interleave each `_pctile` after the field it ranks and drop a null
    `dividend_drag_r` entirely (SPEC §12.2)."""
    out: Dict = {}
    for k, v in record.items():
        if k == "dividend_drag_r" and v is None:
            continue
        out[k] = v
        if k in PCTILE_FIELDS:
            out[f"{k}_pctile"] = ranks[k]
    return _round_floats(out)


# ── aggregates ──


def _aggregate_block(records: Sequence[Dict], per_book: Dict[str, book_history.BookHistory],
                     book_order: Sequence[str], date_from: str, date_to: str) -> str:
    """The baseline block — every figure with its `n`, plus each book's
    drawdown-curve exclusion count (SPEC §12.2)."""
    lines = [
        "# Baseline context (computed over this export, so you need not re-derive it).",
        "# Every figure carries its n. Treat n < 20 as anecdote.",
        "",
    ]
    scope = book_order[0] + " book" if len(book_order) == 1 else "books " + ", ".join(book_order)
    lines.append(f"Scope: {scope}, {date_from} to {date_to}. n={len(records)}.")

    for book in book_order:
        rows = [r for r in records if r["book"] == book]
        if not rows:
            continue
        r_vals = [r["realized_r"] for r in rows if r["realized_r"] is not None]
        no_stop = sum(1 for r in rows if r["stop"] is None)
        recon = sum(1 for r in rows
                    if r["stop"] is not None and r["stop_provenance"] == "reconstructed")
        wins = sum(1 for v in r_vals if v > 0)
        holds = [r["days_held"] for r in rows if r["days_held"] is not None]
        avg_r = f"{sum(r_vals) / len(r_vals):+.2f} (n={len(r_vals)})" if r_vals else "n/a (n=0)"
        avg_hold = (
            f"{sum(holds) / len(holds):.1f} trading days (n={len(holds)})" if holds else "n/a (n=0)"
        )
        lines.append(
            f"{book} book: n={len(rows)}. Avg R {avg_r}. Avg hold {avg_hold}. "
            f"Wins {wins}/{len(r_vals)} of the R-bearing trades."
        )
        setups: Dict[str, int] = {}
        for r in rows:
            setups[r["setup"] or "unlabelled"] = setups.get(r["setup"] or "unlabelled", 0) + 1
        by_setup = ", ".join(f"{k} n={v}" for k, v in sorted(setups.items()))
        lines.append(f"{book} by setup: {by_setup}.")
        lines.append(
            f"{book} R excludes n={no_stop} no-stop and n={recon} reconstructed-stop "
            f"trades from any R conclusion (of n={len(rows)})."
        )
        bh = per_book[book]
        lines.append(
            f"{book} drawdown curve excludes n={bh.excluded_no_stop} closed no-stop "
            f"trade(s); insufficient_history is not a drawdown of zero."
        )
    return "\n".join(lines) + "\n"


# ── the door ──


def export(conn: sqlite3.Connection, *, book: str,
           date_from: Optional[str] = None, date_to: Optional[str] = None) -> str:
    """The whole export for one book: legend, aggregates, then one JSON line per
    Trade (SPEC §12).

    ``seq`` and ``book_drawdown_r_at_entry`` are computed over the book's COMPLETE
    history, then the rows are sliced to ``[date_from, date_to]`` by entry date —
    so a sliced export keeps its ``seq`` gaps (SPEC §12.3). Percentiles rank
    within the sliced set.
    """
    return export_books(conn, book_list=(book,), date_from=date_from, date_to=date_to)


def export_books(conn: sqlite3.Connection, *, book_list: Sequence[str],
                 date_from: Optional[str] = None, date_to: Optional[str] = None) -> str:
    """As :func:`export`, but over an explicit list of books. Default callers pass
    a single book (SPEC §12.4); the multi-book form keeps the drawdown curves
    per-book and stamps ``book`` on every row."""
    s = _Stores(conn)
    records: List[Dict] = []
    per_book: Dict[str, book_history.BookHistory] = {}

    for book in book_list:
        bh = book_history.project(conn, book)
        per_book[book] = bh
        by_id = {r.trade_id: r for r in bh.rows}
        trades = conn.execute(
            "SELECT * FROM trade WHERE book=? ORDER BY entry_date, id", (book,)
        ).fetchall()
        for t in trades:
            if date_from is not None and t["entry_date"] < date_from:
                continue
            if date_to is not None and t["entry_date"] > date_to:
                continue
            records.append(_row(conn, t, s, by_id[t["id"]]))

    records.sort(key=lambda r: (r["entry_date"], r["book"], r["id"]))
    ranks = _percentiles(records)

    entries = [r["entry_date"] for r in records]
    lo = date_from or (min(entries) if entries else "—")
    hi = date_to or (max(entries) if entries else "—")

    parts = [
        LEGEND,
        "",
        _aggregate_block(records, per_book, list(book_list), lo, hi),
        "# One JSON object per trade. See legend above for units.",
    ]
    for rec, rank in zip(records, ranks):
        parts.append(json.dumps(_emit(rec, rank), ensure_ascii=False))
    return "\n".join(parts) + "\n"
