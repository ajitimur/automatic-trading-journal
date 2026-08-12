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
| C curated JSONL | 954 | ~64,000 |
| D Markdown dossier | 1,696 | ~79,000 |

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

## Open for the human

1. Is C's field selection right — anything cut that should ship, anything shipped that
   is noise? Specifically: does the six-variant table need to be in the export at all,
   or is `best_fit_variant` + `best_variant_r` enough?
2. Is the free-text note in the right place (a `note` key on the row) or should it be
   segregated so the numeric rows stay uniform?
3. Scope: whole history, date range, or one book per export? C carries `book` on every
   row, so a mixed export is legal — but is that ever wanted, given nothing aggregates?
4. Should the aggregates block ship, given the model can derive it from the rows?
