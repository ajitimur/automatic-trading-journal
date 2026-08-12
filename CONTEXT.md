# Automatic Trading Journal

A personal journal for momentum swing trades across two markets — US equities via IBKR and Indonesian equities via Stockbit. It records what was executed, enriches it from daily bars, and grades the trades against a mechanical strategy.

## Language

### The record

**Trade**:
The journal's unit of analysis: one symbol, on one book, entered on one day. Same-day entry fills merge into a single Trade; entries on a different day form a different Trade.
_Avoid_: Position (means something else here), fill, execution

**Fill**:
A single execution reported by a broker. The source of truth from which a Trade is derived.
_Avoid_: Trade (IBKR's own exports call fills "trades"), execution, transaction

**Exit**:
An allocation of exit fills to a particular Trade, carrying its own date, quantity, and price. A Trade may have many.
_Avoid_: Sell, close

**Position**:
The broker-level net quantity held in a symbol on a book at a point in time. May span several Trades. Used for reconciliation only — it is never the thing journaled.
_Avoid_: Holding, trade

**Book**:
One of the two markets the journal covers, US or IDX, carrying its currency, benchmark, broker, and lot convention. Nothing is ever aggregated across books.
_Avoid_: Account, portfolio, market

**Equity Snapshot**:
The account equity on a book as of a given date. The most recent snapshot at or before a Trade's entry date is what its risk percentage is measured against.
_Avoid_: Balance, capital, NAV

### Trade properties

**Stop**:
The price at which the trader was working to abandon the Trade. Entered by hand — it is not derivable from bars — and immutable once set.
_Avoid_: Initial stop, stop loss, trailing stop (the journal does not track stop adjustments)

**Setup**:
The chart pattern a Trade was taken on: base breakout, high tight flag, or other. The only judgement the trader records that no data source can supply.
_Avoid_: Pattern, strategy, signal

**Exit Reason**:
Why a particular Exit happened, drawn from a fixed vocabulary. Proposed by enrichment from the daily bars, then confirmed or overridden by the trader.
_Avoid_: Exit type, sell reason

**Stop Provenance**:
Whether a Trade's stop was recorded before its first Exit, or reconstructed from memory afterwards. Derived from timestamps, never entered.
_Avoid_: Confidence, backdated flag

**Risk Percentage**:
What fraction of book equity the Trade put at risk, measured from the entry price down to the stop. Derived.
_Avoid_: Risk, position risk

**Exposure Percentage**:
What fraction of book equity the Trade's cost represented, regardless of stop. Derived.
_Avoid_: Size, allocation

**Realized R**:
A Trade's outcome expressed in multiples of the risk it took. Derived.
_Avoid_: R multiple, RR, reward ratio

### Lifecycle

**Confirm-and-Enrich**:
The step every Trade passes through before it commits, where parsed fills and proposed derived values are shown to the trader for acceptance, and the stop and setup are supplied.
_Avoid_: Import, review, approval

**Frozen**:
The state a Trade reaches 20 trading days after its final Exit, at which its hand-entered fields lock and its derived values are snapshotted.
_Avoid_: Closed, archived, locked

**Drift**:
A disagreement between a frozen Trade's snapshotted derived values and what those values recompute to today. Surfaced for acknowledgement rather than silently applied.
_Avoid_: Discrepancy, staleness

**Revision**:
A superseding version of a Fill issued when a broker restates it. Fills are never edited; the latest revision is live and earlier ones are retained.
_Avoid_: Correction, amendment, update
