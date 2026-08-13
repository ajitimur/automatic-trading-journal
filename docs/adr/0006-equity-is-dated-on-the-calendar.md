# Equity is dated on the calendar

An Equity Snapshot is dated on the calendar. A daily bar is dated on a Trading Day. These are two different axes, and the journal never converts between them by counting.

Snapshots may be looked up by date, and the distance between two of them may be measured in calendar days. What may never happen is counting rows on one axis to answer a question about the other: the number of snapshots between two dates says nothing about how many days a trader could have acted on, and the number of trading days between two dates says nothing about how many snapshots exist.

## Why

The two series disagree about which dates exist, and they disagree in both directions.

Equity has rows where there is no bar. The US broker emits an account value on every weekday, including every exchange holiday in the year — days on which no symbol has a bar at all. Bars have dates where there is no equity row: the Indonesian book records its level roughly monthly, so hundreds of trading days sit between consecutive snapshots.

Both disagreements are correct, and neither is a data problem. An account genuinely has a value on a day nothing traded. A suspended symbol genuinely has no bar. The error is only ever in code that assumes one axis is the other.

There is also no trading-day axis for equity to live on even if one were wanted. A trading day is a property of a *symbol* — that is what makes a suspension a missing day rather than a flat one — while equity is a property of a *Book*. A book has no suspensions, so there is no ordinal to count.

## Alternatives considered

**Filter equity onto the trading-day calendar, so one discipline governs everything.** Tempting, because discarding zero-volume rows is already a rule the journal applies without exception. But that rule's reasoning does not carry: a zero-volume row is discarded because it corrupts *windows* — it deflates a range, shortens a moving average in real terms, lets a trailing signal fire on a day nothing traded. An equity level is not a window over anything. Filtering it would delete values that are true, and open gaps that the staleness rule then reads as neglect: an account looks unrecorded on precisely the days it was recorded.

**Interpolate equity onto the trading-day calendar.** This invents numbers for a field whose entire job is to be a stated fact, and it buys nothing, because the lookup rule — the most recent snapshot at or before the Trade's entry date — has never needed an evenly spaced series.

**Store a trading-day ordinal alongside the calendar date.** There is nothing to store, for the reason above: the ordinal would have to be per symbol, and the snapshot is per Book.

## Consequences

**The staleness bound is stated in calendar days.** It follows directly rather than being chosen: a bound on an axis is measured in that axis's units.

**A holiday row is kept, and it is useful.** It carries the level across a day nothing traded, so the lookup finds a value instead of reaching further back.

**A date with a snapshot and no bar is normal.** It is not a fetch failure, and must not be routed to the repair path that exists for a series failing to cover a Trade's own dates, nor confused with the null that marks genuinely insufficient history.

**No count of equity rows may measure anything.** The post-exit window, the freeze fuse and the bound on a simulated run are all counted in trading days and stay that way. Equity cannot contribute to any of them.

**A sparse series is not a defect of this axis.** One snapshot a month is a legitimate calendar-dated series. What guards a denominator is the staleness bound, not density — and conflating the two would demand daily equity from a book that cannot produce it.
