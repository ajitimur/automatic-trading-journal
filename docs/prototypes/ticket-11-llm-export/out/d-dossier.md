# Trading journal export — field legend

Units. `_adr` fields are multiples of the symbol's Average Daily Range (its typical
daily percent range), so a $400 US name and an IDR 7,200 IDX name are comparable.
`_r` fields are multiples of the risk taken (entry to stop). `_pct` fields are
percent. Prices are in the book's own currency and never converted.

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

Adherence. Every trade is scored against all six mechanical variants
(trail {MA10, MA20} x partial {none, day 3, day 5}); `best_fit_variant` is the one
the trade's behaviour most resembled, derived — not something the trader declared.
`nominal_variant` is what the ruleset in force on the entry date called for.

# Baseline context (computed over this export, so you need not re-derive it)

US book: 2 trades, 1 win, avg R +0.71, avg hold 10.5 trading days.
IDX book: 1 trade, 1 win, avg R +1.69, avg hold 14 trading days.
R aggregates exclude 1 trade with a reconstructed stop (of 3 total).
Ruleset in force throughout: v1 (partial 1/3 on days 3-5, then trail MA10).

## AAOI — US — entered 2026-04-20, exited 2026-05-15 (19 trading days)

**Outcome** +2.50R (24.78%). Setup `base_breakout`, stop recorded.

**Sizing** risked 0.72% of book equity, exposure 7.24%. Entry sat 2.14 ADR above the stop — chased: yes.

**Entry context** market strong_uptrend; price 1.18 ADR over MA10, 3.58 over MA50, 5.99 over MA200; stack aligned_up; up 41.20% over 63d, -3.10% off the 52-week high; RS vs QQQ 29.60; entry-day volume 3.10x the 50-day average. ADR 4.62%.

**Excursion** best +3.63R on 2026-05-12; worst -0.39R on 2026-04-21.

**Exits**

- 2026-04-23 — 400 @ 28.1 (+1.32R) — `scheduled_partial`
- 2026-05-06 — 300 @ 30.4 (+2.25R) — `discretionary_trim`
- 2026-05-15 — 500 @ 33.02 (+3.32R) — `close_below_ma10`

**Exit context** market uptrend; 1.44 ADR over MA10 at the exit close.

**Counterfactual** 20 days after the exit: -8.20% from the exit close; best it reached was +2.94R.

**Adherence (ruleset v1, nominal `trail_ma10__partial_day3`)** partial: taken_in_band; trail exit 0 days off; behaviour best matched `trail_ma10__partial_day3`.

| variant | outcome | R | days from actual |
| --- | --- | --- | --- |
| `trail_ma10__partial_day3` | resolved | +2.50R | 0 |
| `trail_ma10__partial_day5` | resolved | +2.65R | 2 |
| `trail_ma10__partial_none` | resolved | +2.90R | 3 |
| `trail_ma20__partial_day3` | resolved | +2.00R | 6 |
| `trail_ma20__partial_day5` | resolved | +2.12R | 7 |
| `trail_ma20__partial_none` | resolved | +2.25R | 8 |

**Note** _Trailed it properly for once. Nearly trimmed the last third on the 12th when it went vertical — glad I didn't, the MA10 close came three days later._

## NVDA — US — entered 2026-06-02, exited 2026-06-03 (2 trading days)

**Outcome** -1.09R (-3.95%). Setup `high_tight_flag`, stop recorded.

**Sizing** risked 0.90% of book equity, exposure 24.99%. Entry sat 1.25 ADR above the stop — chased: yes.

**Entry context** market uptrend; price 0.74 ADR over MA10, 2.64 over MA50, 6.68 over MA200; stack aligned_up; up 22.40% over 63d, -1.20% off the 52-week high; RS vs QQQ 11.80; entry-day volume 1.40x the 50-day average. ADR 2.90%.

**Excursion** best +0.30R on 2026-06-02; worst -1.30R on 2026-06-03.

**Exits**

- 2026-06-03 — 600 @ 167.42 (-1.09R) — `stop_hit`

**Exit context** market uptrend; -0.31 ADR over MA10 at the exit close.

**Counterfactual** 20 days after the exit: 14.60% from the exit close; best it reached was +3.19R.

**Adherence (ruleset v1, nominal `trail_ma10__partial_day3`)** partial: not_applicable; trail exit null days off; behaviour best matched `null`.

| variant | outcome | R | days from actual |
| --- | --- | --- | --- |
| `trail_ma10__partial_day3` | resolved | -1.09R | 0 |
| `trail_ma10__partial_day5` | resolved | -1.09R | 0 |
| `trail_ma10__partial_none` | resolved | -1.09R | 0 |
| `trail_ma20__partial_day3` | resolved | -1.09R | 0 |
| `trail_ma20__partial_day5` | resolved | -1.09R | 0 |
| `trail_ma20__partial_none` | resolved | -1.09R | 0 |

**Note** _Bought the flag on day two of the pullback instead of waiting for the range to tighten. Stop was under the low, it just went._

## BREN.JK — IDX — entered 2026-07-07, exited 2026-07-24 (14 trading days)

**Outcome** +1.69R (7.94%). Setup `other`, stop reconstructed.

**Sizing** risked 0.74% of book equity, exposure 15.65%. Entry sat 1.24 ADR above the stop — chased: null.

**Entry context** market uptrend; price 1.04 ADR over MA10, 3.41 over MA50, null over MA200; stack null; up 26.90% over 63d, null off the 52-week high; RS vs ^JKSE 21.40; entry-day volume 2.60x the 50-day average. ADR 3.80%.

**Excursion** best +2.38R on 2026-07-22; worst -0.68R on 2026-07-09.

**Exits**

- 2026-07-24 — 40000 @ 7815.0 (+1.69R) — `close_below_ma10`

**Exit context** market neutral; 1.61 ADR over MA10 at the exit close.

**Counterfactual** 20 days after the exit: null from the exit close; best it reached was null.

**Adherence (ruleset v1, nominal `trail_ma10__partial_day3`)** partial: missed; trail exit 0 days off; behaviour best matched `trail_ma10__partial_none`.

| variant | outcome | R | days from actual |
| --- | --- | --- | --- |
| `trail_ma10__partial_day3` | resolved | +1.28R | 3 |
| `trail_ma10__partial_day5` | resolved | +1.41R | 2 |
| `trail_ma10__partial_none` | resolved | +1.69R | 0 |
| `trail_ma20__partial_day3` | capped | null | null |
| `trail_ma20__partial_day5` | capped | null | null |
| `trail_ma20__partial_none` | capped | null | null |

**Note** _No stop written down at the time — reconstructed from where I remember putting it, under the 6900 shelf._

**Insufficient history** — null, not missing: `ma_100`, `ma_200`, `stack_state`, `pct_off_52w_high`, `prior_move_126d`
