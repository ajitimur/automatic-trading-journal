# A bound declaration commits the Trade

When a **Stop Declaration** binds to a clean `new-trade` proposal, one that passed every gate and has no other open question, the Trade commits without `journal confirm`, carrying the declared Stop and, if given, the Setup. A declaration of `no sl` binds as a **Declined Stop** and commits the same way. Every other proposal kind stays in the queue exactly as before. This amends SPEC §5.8's *"New Trades stay one at a time"* and moves [ADR 0010](0010-confirm-demands-the-stop.md)'s demand from the confirm door to the declaration.

## Why the human step was removed

Once the stop and setup arrive with the declaration ([ADR 0011](0011-stops-are-declared-at-entry-through-a-telegram-relay.md)), confirm has one remaining job: a human glance at the interpreted Trade before it becomes hard to undo. Every risk that glance was meant to catch is already caught by something else:

| Risk | Caught by |
| --- | --- |
| IDX lot/quantity column shift, the silent 100× error | Fee-identity gate, which quarantines the whole document before any fill lands |
| IDX wrong price or row | Fee identity, since totals are recomputed from rows |
| IBKR misparse | Machine-generated XML |
| Wrong symbol mapping | Symbol rules (§5.4), which also repair committed Trades |
| Orphan exit, restatement, drift | Their own proposal kinds, which park and never auto-commit |

What remained was a deferrable step on the same path ADR 0010 measured: a step that can always be put off was always put off. Keeping it would leave the trader running a command every evening to approve numbers the gates had already checked.

## What still waits

- A `new-trade` with no bound declaration is held and nagged through the bot, with the trading days left in the grace window, exactly as ADR 0010 holds an unanswered Trade.
- A declaration whose stop sits at or above the entry average does not bind. The bot says so, and a new declaration replaces it.
- An entry filled across two days is two Trades ([ADR 0001](0001-trade-is-an-entry-day-cohort.md)). The declaration binds to the first, and the bot asks for the second, offering the first's stop as a one-tap answer.
- Exits stay on the weekly bulk exit-reason confirm.

## Consequences

**The gates are now the only thing guarding a new Trade.** A committed Trade has no un-commit; a wrong quantity is caught in the weekly review instead of at commit, and fixed by the same quantity correction as before. A parser change that weakens a gate weakens the commit, and should be reviewed as such.

**Auto-committed Trades are marked as such**, so the weekly review can show what landed without the trader looking at it.

**Stop Provenance needs no change.** The declaration's timestamp is when the stop was set, so a stop declared at entry derives `recorded` on its own.
