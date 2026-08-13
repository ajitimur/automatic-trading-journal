# A Trade is an entry-day cohort, not a flat-to-flat position

The journal's unit of analysis is a **Trade**: all entry fills for one symbol on one book on one calendar trade date, plus the exits allocated to it. Entering the same symbol again on a later day creates a *second* Trade, even though the trader was never flat in between. This deliberately departs from the standard trading meaning of a position, which runs flat to flat.

The reason is that the journal exists to grade decisions, and a decision is made on a day. Merging a Monday entry and a Wednesday add into one record would average away two distinct judgements — two setups, two stops, two sizes — into a blended row that answers none of the four learning goals honestly.

## Consequences

Two Trades in the same symbol can be open simultaneously against a single pooled broker holding, and the broker does not say which one an exit belongs to. **Exit fills are therefore allocated FIFO** — oldest open Trade first — overridable during confirm-and-enrich. Pro-rata allocation was rejected: splitting one exit across two Trades smears exit-quality analysis, which is the thing the journal most needs to measure.

Because a Trade spans several fills but a Position spans several Trades, the two must never be used interchangeably. `Position` is retained in the language for broker reconciliation only.
