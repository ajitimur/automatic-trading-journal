#!/usr/bin/env python3
"""PROTOTYPE — throwaway. Renders the same three Trades in four candidate export
shapes so the shape question in #11 can be argued over real bytes.

    python3 render.py          # writes ./out/*, prints the cost table

No dependencies. Token counts are an approximation (chars / 4) — good enough to
compare shapes against each other, not to be quoted as a budget.
"""

import csv
import io
import json
import os
from trades import TRADES

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def tok(s):
    """Rough token estimate. Comparative only."""
    return round(len(s) / 4)


# --------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------

LEGEND = """\
# Trading journal export — field legend

Units. `_adr` fields are multiples of the symbol's Average Daily Range (its typical
daily percent range), so a $400 US name and an IDR 7,200 IDX name are comparable.
`_r` fields are multiples of the risk taken (entry to stop). `_pct` fields are
percent. Prices are in the book's own currency and never converted.

Percentiles. `_pctile` fields rank a trade against THE OTHER TRADES IN THIS EXPORT,
not against any absolute scale and not against the full history. Slice the export
differently and the same trade gets a different percentile. Use them to say "this
entry was extended for this trader", never "this entry was extended".

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
- Sample sizes are given as `n` in the baseline block. Do not report a finding on a
  subgroup without stating its `n`, and treat anything under ~20 as anecdote.

Adherence. Every trade is scored against all six mechanical variants
(trail {MA10, MA20} x partial {none, day 3, day 5}); `best_fit_variant` is the one
the trade's behaviour most resembled, derived — not something the trader declared.
`nominal_variant` is what the ruleset in force on the entry date called for.
"""

AGGREGATES = """\
# Baseline context (computed over this export, so you need not re-derive it)
# Every figure carries its n. Treat n < 20 as anecdote.

Scope: both books, 2026-04-20 to 2026-07-24. n=3.
US book: n=2, 1 win, avg R +0.71, avg hold 10.5 trading days.
IDX book: n=1, 1 win, avg R +1.69, avg hold 14 trading days.
R aggregates exclude n=1 trade with a reconstructed stop (of n=3 total).
By setup: base_breakout n=1, high_tight_flag n=1, other n=1.
Ruleset in force throughout: v1 (partial 1/3 on days 3-5, then trail MA10).
"""


def r_units(t, price):
    """Express a price level in R, from entry and stop."""
    if price is None or t["stop"] is None:
        return None
    risk = t["entry_avg_price"] - t["stop"]
    return round((price - t["entry_avg_price"]) / risk, 2)


def adr_units(t, price):
    if price is None or t["adr_pct"] is None:
        return None
    return round((price / t["entry_avg_price"] - 1) * 100 / t["adr_pct"], 2)


# --------------------------------------------------------------------------
# A — wide CSV, everything, positional
# --------------------------------------------------------------------------

def render_a():
    rows = []
    for t in TRADES:
        row = {k: v for k, v in t.items() if k not in ("exits", "variants", "insufficient_history")}
        # exits and variants have to be flattened or dropped; CSV cannot nest.
        for i, e in enumerate(t["exits"][:3], 1):
            row[f"exit{i}_date"] = e["date"]
            row[f"exit{i}_qty"] = e["quantity"]
            row[f"exit{i}_price"] = e["price"]
            row[f"exit{i}_reason"] = e["reason"]
            row[f"exit{i}_mfe_high"] = e["mfe_high"]
            row[f"exit{i}_mfe_date"] = e["mfe_date"]
            row[f"exit{i}_mae_low"] = e["mae_low"]
            row[f"exit{i}_mae_date"] = e["mae_date"]
        for v in t["variants"]:
            row[f"var_{v['variant']}_r"] = v["r"]
            row[f"var_{v['variant']}_outcome"] = v["outcome"]
            row[f"var_{v['variant']}_fit"] = v["fit_distance_days"]
        rows.append(row)

    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    return buf.getvalue(), len(cols)


# --------------------------------------------------------------------------
# B — full JSONL, nested, raw units, self-describing keys
# --------------------------------------------------------------------------

def render_b():
    lines = []
    for t in TRADES:
        lines.append(json.dumps(t, ensure_ascii=False))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# C — curated JSONL, normalized to ADR and R, flat, legend header
# --------------------------------------------------------------------------

CURATED_NOTE = "# One JSON object per trade. See legend above for units.\n"

# Ranked within the export, never against an absolute scale. Five fields only —
# the ones where "is this high for me?" is the actual question being asked.
PCTILE_FIELDS = (
    "stop_distance_adr",
    "entry_ma_dist_10_adr",
    "entry_move_63d_pct",
    "exposure_pct_of_equity",
    "days_held",
)


def percentiles(records):
    """Within-export rank, 0-100. Nulls do not rank and stay null."""
    for field in PCTILE_FIELDS:
        vals = sorted(r[field] for r in records if r.get(field) is not None)
        for r in records:
            v = r.get(field)
            if v is None or len(vals) < 2:
                r[f"{field}_pctile"] = None
            else:
                below = sum(1 for x in vals if x < v)
                r[f"{field}_pctile"] = round(below / (len(vals) - 1) * 100)
    return records


def capture_ratio(t):
    """What share of the available move the Trade actually took.

    Defined only for a Trade that both went in favour AND finished in profit.
    Null otherwise — and the null is the point. NVDA had 0.30R available and
    lost 1.09R, which computes to -3.63 and reads as a catastrophic *exit*.
    It was not: the exit was correct and immediate. The trade was a bad entry.
    A ratio that indicts the wrong decision is worse than no ratio.
    """
    mfe = r_units(t, t["mfe_high"])
    if mfe is None or mfe <= 0:
        return None
    if t["realized_r"] is None or t["realized_r"] <= 0:
        return None
    return round(t["realized_r"] / mfe, 2)


def deviation_cost_r(t):
    """#8's deviation cost, normalized out of price into R."""
    dc = t.get("deviation_cost_price")
    if dc is None or t["stop"] is None:
        return None
    return round(dc / (t["entry_avg_price"] - t["stop"]), 2)


def render_c(trade):
    t = trade
    o = {
        "id": t["trade_id"],
        "book": t["book"],
        "symbol": t["symbol"],
        "entry_date": t["entry_date"],
        "exit_date": t["final_exit_date"],
        "days_held": t["days_held"],
        "setup": t["setup"],
        "stop_provenance": t["stop_provenance"],
        # price levels — the only two. Enough to answer a price question and to
        # let the model check its own R arithmetic; not enough to tempt it into
        # cross-book currency maths.
        "entry_avg_price": t["entry_avg_price"],
        "stop": t["stop"],
        # outcome
        "realized_r": t["realized_r"],
        "realized_pct": t["realized_pct"],
        # sizing
        "risk_pct_of_equity": t["risk_percentage"],
        "exposure_pct_of_equity": t["exposure_percentage"],
        "stop_distance_adr": t["stop_distance_adr"],
        "chased": t["chased"],
        # entry context, normalized
        "entry_regime": t["regime_at_entry"],
        "entry_ma_dist_10_adr": t["ma_dist_10"],
        "entry_ma_dist_50_adr": t["ma_dist_50"],
        "entry_ma_dist_200_adr": t["ma_dist_200"],
        "entry_stack": t["stack_state"],
        "entry_move_63d_pct": t["prior_move_63d"],
        "entry_off_52w_high_pct": t["pct_off_52w_high"],
        "entry_rs_63d": t["rs_63d"],
        "entry_volume_ratio": t["volume_ratio"],
        "adr_pct": t["adr_pct"],
        # exit context, normalized
        "exit_regime": t["regime_at_exit"],
        "exit_ma_dist_10_adr": t["ma_dist_10_at_exit"],
        "exit_ma_dist_50_adr": t["ma_dist_50_at_exit"],
        # excursion, normalized
        "mfe_r": r_units(t, t["mfe_high"]),
        "mfe_day": t["mfe_date"],
        "mae_r": r_units(t, t["mae_low"]),
        "mae_day": t["mae_date"],
        "capture_ratio": capture_ratio(t),
        # counterfactual
        "fwd_20d_pct": t["fwd_return_20d"],
        "fwd_high_r": r_units(t, t["fwd_high"]),
        # adherence
        "nominal_variant": t["nominal_variant"],
        "best_fit_variant": t["best_fit_variant"],
        "partial_state": t["partial_state"],
        "trail_exit_delta_days": t["trail_exit_delta"],
        "deviation_cost_r": deviation_cost_r(t),
        "best_variant_r": max(
            (v["r"] for v in t["variants"] if v["r"] is not None), default=None
        ),
        # exits, compact
        "exits": [
            {"d": e["date"], "q": e["quantity"], "reason": e["reason"], "r": r_units(t, e["price"])}
            for e in t["exits"]
        ],
        # qualitative
        "note": t["exit_note"],
    }
    return o


def render_c_all():
    records = percentiles([render_c(t) for t in TRADES])
    # Put each _pctile immediately after the field it ranks, so the model reads
    # the value and its position together rather than hunting.
    ordered = []
    for r in records:
        o = {}
        for k, v in r.items():
            if k.endswith("_pctile"):
                continue
            o[k] = v
            if k in PCTILE_FIELDS:
                o[f"{k}_pctile"] = r[f"{k}_pctile"]
        ordered.append(o)
    return CURATED_NOTE + "\n".join(
        json.dumps(o, ensure_ascii=False) for o in ordered
    ) + "\n"


# --------------------------------------------------------------------------
# D — Markdown dossier, one section per trade
# --------------------------------------------------------------------------

def fmt(v, suffix=""):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:+.2f}{suffix}" if suffix == "R" else f"{v:.2f}{suffix}"
    return f"{v}{suffix}"


def render_d():
    out = []
    for t in TRADES:
        out.append(f"## {t['symbol']} — {t['book']} — entered {t['entry_date']}, exited {t['final_exit_date']} ({t['days_held']} trading days)\n")
        out.append(
            f"**Outcome** {fmt(t['realized_r'],'R')} ({fmt(t['realized_pct'],'%')}). "
            f"Setup `{t['setup']}`, stop {t['stop_provenance']}.\n"
        )
        out.append(
            f"**Sizing** risked {fmt(t['risk_percentage'],'%')} of book equity, "
            f"exposure {fmt(t['exposure_percentage'],'%')}. Entry sat "
            f"{fmt(t['stop_distance_adr'])} ADR above the stop — chased: {fmt(t['chased'])}.\n"
        )
        out.append(
            f"**Entry context** market {t['regime_at_entry']}; price "
            f"{fmt(t['ma_dist_10'])} ADR over MA10, {fmt(t['ma_dist_50'])} over MA50, "
            f"{fmt(t['ma_dist_200'])} over MA200; stack {t['stack_state'] or 'null'}; "
            f"up {fmt(t['prior_move_63d'],'%')} over 63d, {fmt(t['pct_off_52w_high'],'%')} "
            f"off the 52-week high; RS vs {t['benchmark']} {fmt(t['rs_63d'])}; "
            f"entry-day volume {fmt(t['volume_ratio'])}x the 50-day average. "
            f"ADR {fmt(t['adr_pct'],'%')}.\n"
        )
        out.append(
            f"**Excursion** best {fmt(r_units(t, t['mfe_high']),'R')} on {t['mfe_date']}; "
            f"worst {fmt(r_units(t, t['mae_low']),'R')} on {t['mae_date']}.\n"
        )
        out.append("**Exits**\n")
        for e in t["exits"]:
            out.append(
                f"- {e['date']} — {e['quantity']} @ {e['price']} "
                f"({fmt(r_units(t, e['price']),'R')}) — `{e['reason']}`"
            )
        out.append("")
        out.append(
            f"**Exit context** market {t['regime_at_exit']}; "
            f"{fmt(t['ma_dist_10_at_exit'])} ADR over MA10 at the exit close.\n"
        )
        out.append(
            f"**Counterfactual** 20 days after the exit: {fmt(t['fwd_return_20d'],'%')} "
            f"from the exit close; best it reached was {fmt(r_units(t, t['fwd_high']),'R')}.\n"
        )
        out.append(
            f"**Adherence (ruleset {t['ruleset_version']}, nominal `{t['nominal_variant']}`)** "
            f"partial: {t['partial_state']}; trail exit {fmt(t['trail_exit_delta'])} days off; "
            f"behaviour best matched `{t['best_fit_variant'] or 'null'}`.\n"
        )
        out.append("| variant | outcome | R | days from actual |")
        out.append("| --- | --- | --- | --- |")
        for v in t["variants"]:
            out.append(
                f"| `{v['variant']}` | {v['outcome']} | {fmt(v['r'],'R')} | {fmt(v['fit_distance_days'])} |"
            )
        out.append("")
        out.append(f"**Note** _{t['exit_note']}_\n")
        if t.get("insufficient_history"):
            out.append(
                "**Insufficient history** — null, not missing: "
                + ", ".join(f"`{f}`" for f in t["insufficient_history"])
                + "\n"
            )
    return "\n".join(out)


# --------------------------------------------------------------------------

def main():
    os.makedirs(OUT, exist_ok=True)

    a, ncols = render_a()
    b = render_b()
    c = render_c_all()
    d = render_d()

    files = {
        "a-wide.csv": a,
        "b-full.jsonl": b,
        "c-curated.jsonl": LEGEND + "\n" + AGGREGATES + "\n" + c,
        "c-curated-no-legend.jsonl": c,
        "d-dossier.md": LEGEND + "\n" + AGGREGATES + "\n" + d,
        "legend.md": LEGEND,
        "aggregates.md": AGGREGATES,
    }
    for name, body in files.items():
        with open(os.path.join(OUT, name), "w") as f:
            f.write(body)

    print(f"Wrote {len(files)} files to {OUT}\n")
    print("Approximate tokens for THREE trades (chars/4):\n")
    rows = [
        ("A  wide CSV, everything, positional", a, f"{ncols} columns"),
        ("B  full JSONL, nested, raw units", b, "every field, self-describing"),
        ("C  curated JSONL + legend + aggregates", files["c-curated.jsonl"], "~40 keys/trade"),
        ("C' curated JSONL, rows only", c, "legend amortizes"),
        ("D  Markdown dossier + legend + aggregates", files["d-dossier.md"], "prose framing"),
    ]
    print(f"{'shape':<44} {'tokens':>8} {'per trade':>10}  note")
    print("-" * 92)
    for label, body, note in rows:
        print(f"{label:<44} {tok(body):>8} {tok(body)//len(TRADES):>10}  {note}")
    print()
    print(f"{'legend block alone':<44} {tok(LEGEND):>8}")
    print(f"{'aggregates block alone':<44} {tok(AGGREGATES):>8}")
    print()
    print("Extrapolated to a 200-trade year (rows only, legend counted once):")
    for label, body, per_trade_src in (
        ("A  wide CSV", a, a),
        ("B  full JSONL", b, b),
        ("C  curated JSONL", c, c),
        ("D  Markdown dossier", render_d(), None),
    ):
        per = tok(body) / len(TRADES)
        total = round(per * 200 + tok(LEGEND) + tok(AGGREGATES))
        print(f"  {label:<24} ~{total:>7,} tokens")


if __name__ == "__main__":
    main()
