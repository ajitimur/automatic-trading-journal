# The analytical record starts 18 August 2026

Every Trade entered before **18 August 2026** stays in the journal and is excluded from every aggregate and from the LLM export. This supersedes SPEC §2's *"Backdating starts July 2026"* cap, which governed how far back hand-entry would reach and never governed what the brokers' own exports swept in.

The decision was forced by what the record actually contained. Across 207 Trades the journal held **zero stops** — not one `recorded`, not one `reconstructed`, on either book. 116 of those Trades had already frozen without one, making their Risk Percentage and Realized R permanently unavailable. The remaining stop-less Trades could still be filled, but only by inferring the levels from the charts after the fact, which is not what a **Stop** is (see [ADR 0009](0009-stops-are-never-backfilled.md)).

That left a choice between a large record that cannot answer the questions it was built for and a small one that can. The journal exists to grade trades against a mechanical strategy, and every grade — Realized R, Deviation Cost, chase analysis — runs through `entry_avg_price − stop`. A history with no stops in it is not a smaller version of the intended record; it is a different record that happens to share its schema.

## Considered alternatives

**Deleting the pre-boundary Trades** was rejected on [ADR 0003](0003-fills-are-append-only.md) grounds. Fills are append-only and a broker cannot reissue them; destroying 200 real executions to tidy a date range is exactly what that decision exists to prevent. The boundary achieves the identical clean slate — nothing before 18 August appears in any number — and differs only in what stays recoverable.

**Moving the boundary to July 2026** (the SPEC's stated cap) was the starting position and was abandoned once it became clear the July Trades were in the same condition as the rest: stop-less, closed, and reconstructable only by inference.

## Consequences

**Scope Start is per-book, not global.** Nothing in this journal is aggregated across books, and a single global date would be the first thing that is. Both books start at 2026-08-18 today; they move independently.

**The US book started empty.** Every US Trade at the time predated the boundary, so the per-book strip in SPEC §11.2 rendered an empty US column and the export shipped IDX-only rows. This was accepted rather than overlooked, and it resolved as soon as US trading resumed — the first unattended Flex fetch after the boundary brought six in-scope US Trades.

**`book_drawdown_r_at_entry` reads `insufficient_history` on both books for months.** It needs 20 closed Trades with recorded stops (SPEC §7.9); the record restarts at a handful.

**Inclusion is governed by entry date, permanently.** A Trade entered before the boundary that closes after it never enters the aggregates, even though its outcome falls inside the journal's life. Risk %, Exposure %, Ruleset Version and Book History all resolve as-of entry; letting exit date govern inclusion would put one field meaning "as of entry" and another meaning "as of exit" inside the same aggregate.

**Scope Start governs the whole surface, not only the aggregates.** This reverses the narrower reading this ADR was first written with, which bounded the counts and left the lists alone on SPEC §11.3's *"a list is not an aggregate"*. That rule is about not letting a mixed-book list imply a combined number; it was never a licence to show a record that has been deliberately restarted alongside the stretch it was restarted to leave behind.

Opening the review surface settled it. The boundary was live, the counts were correct, and the page still carried **197 closed pre-boundary Trades and a banner of ~220 items** about them. A reader meeting that page does not meet a record beginning on 18 August; they meet the old record with a correct number buried in it. The counts being right is not the same as the surface being right.

**One exception: a Trade still open stays visible however old it is.** It can never *count* — inclusion is judged on entry date, permanently, per the rule above — but it is live money, and the review surface is where a position gets managed. Three IDX Trades entered before the boundary (MDIA, PTRO, VERN) are still held; hiding them would be the one way this boundary could do real harm rather than merely hide history. SPEC §11.3 already keeps open Trades out of the week's counts, so the exception cannot leak one into a number.

Pre-boundary Trades also stay **in the journal** regardless of visibility, so their exit fills have somewhere to allocate under §3.4. Three index ETFs (IWDA, EIMI, EQAC) that the broker export swept in were never momentum swing trades and leave regardless.

**The review week clamps forward when it closes before the record opens.** The weekly cadence takes the last *completed* Mon–Fri, which on the day the boundary was set ended four days before Scope Start — a window that could only ever report "no Trades closed this week". It now advances to the week the record begins in, labelled as partial. A cadence that cannot reach the record is not a cadence, it is an empty box that looks like a fault.
