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

**Entry Average Price**:
The quantity-weighted mean of a Trade's own entry fills. Weighted so that price times quantity equals the cash that actually left the account — risk, exposure, and R all tie back to real money through that property. Its counterpart on the way out is the Exit Average Price.
_Avoid_: VWAP (that means the market's intraday benchmark, not this), average fill, entry price

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

**Chasing**:
Entering so far above the Trade's stop that the gap between the two exceeds a typical day's range — the symbol must travel more than an average day against the Trade merely to stop it out. Derived, and not meaningful where the stop's provenance is reconstructed rather than recorded.
_Avoid_: Extension, late entry, overextended

### Enrichment

**Average Daily Range**:
The share of its own price a symbol typically travels in a day, averaged over a recent window. The journal's unit of comparison: chart distances and excursions are expressed as multiples of it, so trades in either book at any price level can be read side by side.
_Avoid_: ATR, volatility, range, ADR%

**Excursion**:
How far in favour of or against a Trade the price reached within a window, measured from the extremes of the daily bars rather than their closes. Distinct from the Trade's outcome, which is settled by its Exits.
_Avoid_: MFE, MAE, drawdown, runup

### Rule adherence

**Ruleset Version**:
A dated statement of the mechanical strategy, naming which Variant counts as the rule. A Trade is graded against whichever version was live on its entry date; a version is superseded, never edited.
_Avoid_: Strategy, rules, playbook

**Variant**:
One combination of trailing moving average and partial timing that the journal can simulate. Every Trade is run against all of them, whatever the trader did.
_Avoid_: Rule, scenario, strategy

**Nominal Variant**:
The Variant the live Ruleset Version designates as the rule. The one adherence is measured against — the others are still simulated, so drift toward another Variant stays visible.
_Avoid_: The rule, target, baseline

**Counterfactual**:
A simulated run of one Trade under one Variant, carrying the Trade's own recorded stop. Stored as its exit legs rather than as an outcome, so any unit can be derived from it later.
_Avoid_: Backtest, simulation, what-if

**Deviation Cost**:
What following the Nominal Variant would have changed about a Trade's outcome. A measurement, never a judgement — the journal has no record of intent and so cannot tell a considered override from a mistake.
_Avoid_: Penalty, error, adherence score, violation

**Fit**:
How closely a Trade's actual exit dates track a Variant's simulated ones, measured in trading days. Scored on behaviour rather than outcome, because unlike rules routinely coincide on profit.
_Avoid_: Match, accuracy, compliance

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
