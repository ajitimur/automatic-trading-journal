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

**Scope Start**:
The date from which a Book's Trades count. Stated per Book, because the two books' records begin at different places for different reasons and a single date would be the first thing ever shared between them. Trades entered before it stay in the journal and stay readable — their Fills are facts, and their Exits still need somewhere to allocate — but they enter no count, no aggregate, and no export. It marks where the record became answerable rather than where trading began.
_Avoid_: Cutoff, epoch, since date, inception

**Orphan Exit**:
A sell Fill with no journalled entry to come out of, because the position was built before the broker's export window reached back. It parks rather than guessing: nothing is inferred about a Trade the journal never saw. Distinct from a Trade that closed normally, and distinct from the remainder of a sell that allocated as far as the open Trades allowed — that remainder becomes one of these once the part that fit has landed.
_Avoid_: Unmatched sell, dangling exit, orphan trade

**Equity Snapshot**:
What a Book was worth on a given date, marked to market. The most recent snapshot at or before a Trade's entry date is what its Risk Percentage and Exposure Percentage are measured against. Dated on the calendar rather than in Trading Days, and captured from the source that reported it rather than derived on demand — a broker window that only reaches back a year would otherwise take the journal's own history with it when it moves. Each snapshot states where its number came from and keeps the components it was read from, so the choice of which figure counts as equity stays revisable.
_Avoid_: Balance, capital, NAV, deposited capital

**Equity NAB**:
The account equity figure the Indonesian broker prints on its monthly statement: the valued portfolio plus the settled ledger balance. Excludes a separate cash pool the statement carries but does not explain, which is why the components are kept beside it.
_Avoid_: NAV, net asset value (the US broker's term for its own figure is different), balance

**Staleness Bound**:
How far back a lookup may reach for an Equity Snapshot before the journal refuses to answer. Stated per Book and in calendar days, because the two books record equity at incomparable cadences. Past it, Risk Percentage and Exposure Percentage are null with a marker rather than computed against a level that no longer describes the Book — a wrong denominator is worse than a missing one, because it still passes the test it should fail.
_Avoid_: Timeout, expiry, TTL, freshness

### Trade properties

**Stop**:
The price at which the trader was working to abandon the Trade. Entered by hand — it is not derivable from bars — and immutable once set. Entered while the Trade is live or not at all: a level read off the chart after the fact is a different thing wearing the same name, and the journal would rather carry a hole than a denominator it invented. A Trade that freezes without one has no Risk Percentage and no Realized R, ever.
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

**Declined Stop**:
A Trade committed without a stop after the trader was asked for one and said no. The distinction it draws is between a hole that was chosen and a hole nobody noticed — the second is what the journal filled up with when the question was never put. It settles nothing about the Trade's outcome and buys no forgiveness: the R is still absent and still becomes permanent at freeze. It only means the cost was known at the time. Reversible until freeze, and a stop arriving un-declines it.
_Avoid_: Skipped, waived, no-stop flag, opt-out

**Stop Provenance**:
Whether a Trade's stop was set early enough to be believed, or late enough to be suspect. Derived from timestamps, never entered. A stop set before the Trade's first Exit is recorded, and so is one set within a few trading days of entry even if an Exit has landed — a Trade that opens and closes inside a week would otherwise be unjudgeable no matter how promptly its stop arrived. Past that window it is reconstructed, and excluded from the judgements hindsight would flatter. The window is why recorded means *set while it was still early*, not *set before the outcome was known*.
_Avoid_: Confidence, backdated flag

**Risk Percentage**:
What fraction of book equity the Trade put at risk, measured from the entry price down to the stop. Derived, and undefined where no Equity Snapshot sits within the Staleness Bound of the entry date. Never available on the day of entry — every source reports equity in arrears.
_Avoid_: Risk, position risk

**Exposure Percentage**:
What fraction of book equity the Trade's cost represented, regardless of stop. Derived, and reads the same Equity Snapshot as the Risk Percentage under the same conditions — one denominator asked a second question, never a second denominator.
_Avoid_: Size, allocation

**Realized R**:
A Trade's outcome expressed in multiples of the risk it took. Derived.
_Avoid_: R multiple, RR, reward ratio

**Dividend Drag**:
How much of a Trade's Realized R is owed to a distribution paid during it rather than to the price move. The journal measures price and not total return, so a Trade spanning an ex-date carries a loss it never really took; Dividend Drag states the size of that gap without correcting for it. Has no meaning where the Trade spanned no distribution, which is not the same as a Drag of zero.
_Avoid_: Dividend, yield, total return, adjustment

**Chasing**:
Entering so far above the Trade's stop that the gap between the two exceeds a typical day's range — the symbol must travel more than an average day against the Trade merely to stop it out. Derived, and not meaningful where the stop's provenance is reconstructed rather than recorded.
_Avoid_: Extension, late entry, overextended

### Enrichment

**Trading Day**:
A date on which a symbol actually traded. A date the exchange was open but on which the symbol was suspended is not one of its trading days, however the data source reports it. Every window the journal measures is counted in trading days, so a suspension stretches a window in calendar time rather than filling it with a day that did not happen.
_Avoid_: Session, bar, calendar day, business day

**Limit Locked**:
A day on which a symbol traded at a single price because it moved as far as the exchange permits in one day, so an order at any other price could not have been filled. Noted against a Counterfactual's exit leg, where it means the simulated fill was unobtainable and any cost derived from it should not be read as real.
_Avoid_: Halt (nothing trades at all then — see Trading Day), circuit breaker, auto rejection

**Average Daily Range**:
The share of its own price a symbol typically travels in a day, averaged over a recent window. The journal's unit of comparison: chart distances and excursions are expressed as multiples of it, so trades in either book at any price level can be read side by side.
_Avoid_: ATR, volatility, range, ADR%

**Excursion**:
How far in favour of or against a Trade the price reached within a window, measured from the extremes of the daily bars rather than their closes. Distinct from the Trade's outcome, which is settled by its Exits.
_Avoid_: MFE, MAE, drawdown (that is the Book's, not a Trade's — see Book Drawdown), runup

**Book History**:
What a Book's own record says about the moment a Trade was entered: what preceded it, how many Trades were live beside it, how deep the Book Drawdown was. Computed from the current Trade set whenever it is read, never stored on the Trade and never snapshotted — its inputs are the journal's own records, so there is no outside restatement to defend against.
_Avoid_: Sequence fields, context fields, state at entry

**Book Drawdown**:
How far a Book's cumulative Realized R sits below its own high-water mark. A property of a Book on a date, which a Trade reads rather than owns. Measured in R off closed Trades, so deposits and withdrawals cannot move it and Trades with no recorded stop do not contribute; it has no meaning until the Book has enough closed Trades to have established a high-water mark. Never combined across Books — two separately funded pots have separate marks.
_Avoid_: Drawdown (unqualified — Excursion is the Trade-level one), equity drawdown, peak-to-trough

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

**Write-Off**:
The end of a Trade whose holding can no longer be sold — delisted, suspended for good, or otherwise terminal. Recorded as an Exit like any other, at whatever the trader recovered, and it Freezes the Trade at once: there is no post-exit window to wait out when the symbol has no further Trading Days.
_Avoid_: Delisting, abandonment, cancellation, total loss

**Drift**:
A disagreement between a frozen Trade's snapshotted derived values and what those values recompute to today. Surfaced for acknowledgement rather than silently applied.
_Avoid_: Discrepancy, staleness

**Revision**:
A superseding version of a Fill issued when a broker restates it. Fills are never edited; the latest revision is live and earlier ones are retained.
_Avoid_: Correction, amendment, update
