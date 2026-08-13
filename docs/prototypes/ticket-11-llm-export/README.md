# PROTOTYPE — the LLM export format (#11)

Throwaway. Answers one question: **what shape should the LLM export be?**

```
python3 render.py       # writes out/, prints the cost table
```

Three Trades, chosen to stress the export rather than flatter it: a clean multi-exit
winner (AAOI, US), a trade stopped out before the partial band so every counterfactual
must come back `not_applicable` (NVDA, US), and a recent IDX listing with a
reconstructed stop and five fields nulled for insufficient history (BREN).

Four shapes, same data, in `out/`:

| | file | what it is |
| --- | --- | --- |
| **A** | `a-wide.csv` | one wide row per trade, everything, positional |
| **B** | `b-full.jsonl` | every field, nested exits and variants, raw units |
| **C** | `c-curated.jsonl` | ~40 keys, normalized to R and ADR, legend + aggregates header |
| **D** | `d-dossier.md` | Markdown prose section per trade |

`c-curated-no-legend.jsonl`, `legend.md` and `aggregates.md` are split out so the
legend's cost can be judged on its own.

## What the bytes showed

Token counts are `chars / 4` — comparative only, not a budget.

| shape | 3 trades | extrapolated to 200 trades |
| --- | --- | --- |
| A wide CSV | 1,090 | ~73,000 |
| B full JSONL | 2,255 | ~151,000 |
| C curated JSONL | 1,142 | ~77,000 |
| D Markdown dossier | 1,933 | ~79,000 |

> **Note.** C was ~64,000 when first measured, before the decisions in
> [Locked](#locked) added price levels, `capture_ratio`, `deviation_cost_r` and five
> within-export percentiles. Those cost ~20%, and they push C slightly *above* the
> wide CSV. C is no longer the cheapest shape — it is the most useful per token, which
> is the thing actually being optimized. The percentiles alone are ~8,000 tokens a
> year and are the first thing to cut if the budget ever binds.

**CSV's cost advantage does not survive contact with the real record.** The intuition
that positional columns beat repeated keys assumes a flat record. This one is not flat:
flattening three exits and six adherence variants blows the header to **117 columns**,
and the padding is severe — the single-exit NVDA trade leaves 21 empty cells, BREN 41.
It ends up *more* expensive per trade than the curated JSONL while losing nesting,
losing the ability to carry a variable number of exits at all (the renderer truncates
at three), and being unreadable to the model without constant header counting.

**Shipping every field is the expensive mistake, not the key repetition.** B is 2.4x C.
The single largest line item per trade is the six-variant adherence block; C collapses
it to four fields (`best_fit_variant`, `best_variant_r`, `partial_state`,
`trail_exit_delta_days`) and that one curation decision accounts for most of the gap.

**The legend is close to free and pulls its weight.** 437 tokens, paid once — 0.5% of a
200-trade export. It is also the only place the export can state the caveats that stop
the model producing confidently wrong analysis: that `reconstructed` stops must be
excluded from R and chase conclusions, that `not_applicable` is not a deviation, that
the two books never aggregate, and that there is no recorded plan so intent cannot be
inferred. Without it a model will cheerfully average R across a reconstructed stop.

**Normalization is what makes the two books one export.** In C the IDX row (IDR 7,240
entry) and the US row ($174.30 entry) sit side by side and compare directly, because
every distance is in ADR and every level in R. In A and B they do not — the raw prices
are three orders of magnitude apart and the model has to do currency-aware arithmetic
it will sometimes get wrong.

**Markdown reads better and costs 22% more.** D is the most pleasant to read and the
worst to slice: rows cannot be filtered, and the per-trade variant table is the bulk of
its extra cost.

## Locked

`out/c-curated.jsonl` is the settled shape. Decided in
[#11](https://github.com/ajitimur/automatic-trading-journal/issues/11):

- **JSONL, one object per Trade per line**, normalized to R and ADR, legend header
  always present.
- **`entry_avg_price` and `stop` are the only price levels.** Enough to answer a price
  question and to let the model check its own R arithmetic; not enough to invite
  cross-book currency maths.
- **`capture_ratio`** = `realized_r / mfe_r`, **null unless the Trade both went in
  favour and finished in profit**. NVDA is why: 0.30R available, 1.09R lost, which
  computes to −3.63 and reads as a catastrophic *exit*. The exit was correct and
  immediate; the entry was the mistake. A ratio that indicts the wrong decision is
  worse than no ratio.
- **`deviation_cost_r`** — #8's deviation cost, normalized out of price into R.
- **The six-variant table does not ship.** `best_fit_variant`, `best_variant_r`,
  `partial_state`, `trail_exit_delta_days`, `deviation_cost_r` carry it.
- **Five within-export percentiles**, rendered immediately after the field each ranks.
  The legend states they are ranks *within this export* — slice differently and the
  same Trade ranks differently.
- **The note stays on the row.** Segregating it would break the one-object-per-Trade
  property that makes the export filterable.
- **One book per export by default**; `book` stays on every row so a deliberate
  normalized cross-book export is still legal.
- **Aggregates ship, with `n` on every figure**, plus a legend caveat to treat n < 20
  as anecdote.
