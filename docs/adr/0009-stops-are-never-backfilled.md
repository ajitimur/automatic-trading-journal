# Stops are never backfilled, and a three-day grace window governs provenance

A **Stop** is entered while the Trade is live or not at all. There is no bulk-backfill surface, no reconstruct-from-chart tool, and no path that makes an old stop-less Trade gradeable. A stop set within **three trading days of the entry date** derives `recorded` even if an Exit is already on record; past that window it derives `reconstructed` and is barred from adherence and chase scoring as before.

This amends the provenance rule in [ADR 0002](0002-no-plan-only-executed-trades.md), which keyed `recorded` purely on whether the stop preceded the Trade's first Exit.

## Why no backfill

The journal accumulated 207 Trades without a single stop, so the obvious response was a fast bulk-entry surface for the 91 that could still take one. Interrogating that revealed the levels were not recoverable from memory — they would be read off the charts, after the outcome.

A value inferred from daily bars is not a Stop. CONTEXT.md defines one as *"the price at which the trader was working to abandon the Trade,"* explicitly *"not derivable from bars,"* and ADR 0002 rests the entire case for hand-entry on that same sentence. Backfilling would have produced a different concept wearing the same name and flowing undetected into `realized_r`.

The journal already decided this question once, in the opposite corner. **Staleness Bound** nulls Risk Percentage rather than compute it against an equity level that no longer describes the Book, reasoning that *"a wrong denominator is worse than a missing one, because it still passes the test it should fail."* An inferred stop is the same denominator failing the same way, with hindsight added. Ruling backfill out is that principle applied consistently.

## Why a grace window, and what it costs

The strict rule permanently holes every Trade that opens and closes within a few days — on the IDX book, most of them. Three of the seven Trades surviving the boundary in [ADR 0008](0008-the-analytical-record-starts-18-august-2026.md) closed within two days of entry, so under the strict rule a record deliberately restarted for the sake of having stops would have begun 43% holed.

Three trading days, because the strategy's earliest planned decision is `planned_partial_day3`; a wider window would certify stops set after the trader had already acted on the Trade. Anchored to the **entry date** rather than to the first Exit, because only the entry date bounds "before I knew" honestly — anchoring to the Exit would certify a stop typed months into a Trade that happened to run.

**The cost is real and was accepted with the alternative in view.** Inside the window a stop may be entered on an already-closed Trade with its outcome fully visible and still read `recorded`. SPEC §10.6's tier table can therefore no longer promise that `recorded` means uncontaminated — it means *set inside the window*, which is a weaker claim. The narrower alternative (the window applies only while the Trade is still open) was considered and rejected because it returns exactly the Trades the window exists for to `reconstructed`, buying nothing.

## Consequences

**`reconstructed` remains reachable and still means what it meant.** A stop set past the window on an unfrozen Trade still lands, still computes Risk % and Realized R, and is still excluded from adherence and chase. What is gone is any workflow that produces them in bulk.

**Freezing without a stop stays permanent**, and is now the expected end state for anything not attended to within days rather than a failure of the weekly nag. SPEC §11.4 recorded this as a live risk — *"if a stop hole ever freezes shut in practice, this is the decision to revisit"* — and it fired 116 times before being revisited here.

**The counting is in trading days, off the symbol's own cached bars**, so a suspension stretches the window in calendar time rather than filling it with a day that did not happen. Where no bars are cached the window cannot be counted and the strict rule applies unchanged.
