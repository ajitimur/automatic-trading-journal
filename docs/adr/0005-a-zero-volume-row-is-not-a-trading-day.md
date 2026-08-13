# A zero-volume row is not a trading day

A daily bar reporting **zero volume** is discarded at the boundary where bars enter the journal. No enrichment, no regime calculation and no counterfactual ever sees one. Every window the journal measures — average daily range, the moving averages and their slopes, realized volatility, the post-exit window, the bound on a simulated run — counts the days that remain.

This is uniform across both books. The US side rarely produces such a row; the Indonesian side produces them whenever a symbol is suspended, which is often enough to matter.

## Why

A suspension does not arrive as a gap in the series. It arrives as a row: price flat at the previous close, volume zero. Left in place, that row is not merely useless — it is actively wrong in four different directions at once.

It deflates the **average daily range**, because a day with no range still counts toward the mean. That is the journal's single unit of comparison, so every distance and every excursion expressed in it silently inflates. It collapses **realized volatility** toward zero, which moves a market's regime label. It shortens the **moving averages** in real terms, since ten rows no longer means ten days the trader could have acted on. And it lets a **trailing exit fire on a day nothing traded** — a close below the moving average, on a close that is just yesterday's close repeated.

Discarding the row fixes all four with one rule, and it agrees with what the trader actually sees: a charting platform paints no candle for a suspended session, so the moving averages on screen are already computed this way.

## Alternatives considered

**Carry the row and special-case each consumer.** Every calculation would need to know about suspensions independently, and each would get its own chance to forget. The bug would return the next time a field was added.

**Treat a suspension as missing data.** It is not missing. The data source is reporting the truth — the symbol did not trade. Routing it through the repair path would demand manual attention for a market event that needs none, and would drown the signal that path exists to carry.

**Cap how long a window may stretch in calendar time.** A long suspension leaves a moving average spanning months, which is arithmetically correct and arguably meaningless. But any cap is a threshold, and this project has consistently refused to invent thresholds it cannot justify. It is also unnecessary: derived values already declare the dates they were computed from, so the calendar span of a window is recoverable by anyone who wants to judge it.

## Consequences

**Suspension stops being a special case.** The question of what a simulated run should do when its exit signal lands on a suspended day does not need an answer, because no such day exists to land on. Carry-forward, skip and terminate were all answers to a question the rule deletes.

**A discarded day is not a fetch failure.** The check that every series covers the dates a Trade needs must count a discarded row as present. Conflating the two would turn every suspension into a false alarm demanding repair. The count of discarded days belongs with that check's diagnostics, where a long suspension is visible without being an error.

**Freezing is measured in days that happened.** A Trade whose symbol is suspended does not burn its post-exit window while nothing trades. Where a symbol never trades again, the window is not merely paused but meaningless, which is why a Write-Off freezes its Trade immediately instead of waiting.

**Reversing this later moves every stored number.** Ranges, volatilities, moving averages, simulated exits and the dates things froze on all rest on it. That is why it is written down here rather than left as a detail of the data layer.
