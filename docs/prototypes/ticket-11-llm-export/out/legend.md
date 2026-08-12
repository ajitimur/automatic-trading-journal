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
