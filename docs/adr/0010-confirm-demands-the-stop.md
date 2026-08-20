# Confirm demands the stop, and the decline is recorded

`journal confirm` will not commit a new Trade until its stop is answered — either `--stop SYMBOL=PRICE`, or `--no-stop SYMBOL`, which commits without one and records that the trader was asked and accepted the permanent hole. This reverses SPEC §5.5's chaseable path, where the stop was never demanded and the nag lived in the weekly banner.

The reversal is not a change of mind about the trade-off; it is the trade-off having been measured. §5.5 chose chaseability because *"on a busy day, demanding a stop is friction at exactly the wrong moment,"* and expected the 20-trading-day fuse to get "roughly three looks" at a weekly cadence. **Over 207 Trades it produced zero stops and froze 116 holes shut.** A step that is always deferrable was always deferred.

## What is actually demanded

Not a stop — an *answer*. Declining is one flag and always available, so the friction §5.5 worried about is bounded at a keystroke. What is no longer available is committing while saying nothing, because that is the path that ran to zero.

A Trade whose demand goes unanswered is **held, not rejected**: its Fills are untouched in the ledger, and it re-proposes on the next confirm. SPEC §5.7's *"missing bars never block the commit — the fills are facts"* is intact; the ledger still accepts everything, and only the derived Trade waits for a decision that is one flag away.

## Considered alternatives

**Hard block with no decline** was rejected: it holds real executions hostage to a discretionary field, and on a day the stop genuinely is not decided yet it leaves no honest way through.

**A louder nag** was rejected because it is what already exists. The banner has stated the missing-stop fact every week for the life of the journal.

## Consequences

**The demand lives at the door, not in the primitive.** `trades.confirm()` takes `demand_stop`, off by default; only `journal confirm` turns it on. The gate is a workflow policy — the one place a human commits — while the commit primitive stays mechanical, so cohort, FIFO and restatement behaviour remain testable without every case answering a question it is not about. Nothing reaches the store from a person except through the gated door.

**Declining is a decision about this moment, never a door that locks.** `stop_declined` clears the instant a stop arrives, and the review surface's *add stop* is unchanged. Combined with [ADR 0009](0009-stops-are-never-backfilled.md)'s grace window, a Trade declined on a busy Monday and answered on Tuesday still reads `recorded`.

**A declined Trade is not nagged.** The missing-stop nag skips `stop_declined` rows: the question was asked and answered, and a banner that keeps raising settled matters is one the trader learns to skim. The freeze fuse still applies, and the hole still becomes permanent — that outcome is now something chosen on the record rather than an omission nobody sees until it is too late.

**Every fixture that commits a Trade now answers.** That is the intended blast radius: it makes the demand visible everywhere a Trade is created, including in the tests.
