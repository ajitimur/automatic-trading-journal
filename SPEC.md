# Automatic Trading Journal — Specification v1

**Status: locked.** This document is the output of the wayfinding effort tracked in [Map: Automatic trading journal spec](https://github.com/ajitimur/automatic-trading-journal/issues/1). Every decision below was reached in a numbered ticket, and each section links the ticket that owns it. Nothing here should be re-litigated during the build; where a decision was deliberately left open, it appears in [§14 Handed to the build effort](#14-handed-to-the-build-effort) and nowhere else.

**How to read it.** This spec is the integrating document, not the only one. Three companions carry detail it deliberately does not duplicate:

| Document | Holds |
| --- | --- |
| [`CONTEXT.md`](CONTEXT.md) | The glossary — the project's ubiquitous language. Every capitalised domain term below is defined there. |
| [`docs/adr/`](docs/adr/) | The six hard-to-reverse decisions, with their reasoning and rejected alternatives. |
| [`docs/research/`](docs/research/), [`docs/samples/`](docs/samples/) | What the data sources actually return, verified against real files. |

Where this spec and a companion disagree, the companion wins on detail and this document wins on scope.

**A note on precision.** Several rules below look like details and are not: they are load-bearing invariants that many other numbers silently depend on. Those are marked **invariant** and carry an ADR.

---

## 1. Purpose

A personal journal for momentum swing trades across two markets — US equities via IBKR and Indonesian equities via Stockbit. It records what was executed, enriches it from daily bars, grades it against a mechanical strategy, and exports it for analysis by an LLM.

It exists to answer four **learning goals**:

1. **Setup selection** — which chart patterns actually pay.
2. **Exit quality** — what was left on the table, and where.
3. **Sizing** — how large the bets were, and whether size tracked outcome.
4. **Regime** — what the market was doing while all of the above happened.

Every field in this spec serves at least one of them. No field was kept for completeness.

**Discipline is limited to stop, size and exit reason — as executed.** There is no recorded intent, so the journal cannot ask "did the trader do what they planned." See [ADR 0002](docs/adr/0002-no-plan-only-executed-trades.md).

---

## 2. Scope

### In

Capture and enrichment for both books; the rule-adherence and counterfactual engine; the weekly review surface; the LLM export; the runtime and daily job.

### Out

Each of these was ruled out deliberately, not overlooked.

| Out of scope | Why |
| --- | --- |
| **Building beyond v1's shape** | This spec hands off to a build effort. |
| **Order execution** | This is a journal, not a trading system. |
| **FX conversion, unified equity curve** | Two books stay native-currency; nothing aggregates across them. |
| **Intraday bars** | Daily is sufficient for every field in play, and intraday would likely make the IDX side infeasible. |
| **Chart images** | For LLM consumption, numbers are strictly more precise and vision on candle charts is lossy. For human review, the workbench timeline covers it. |
| **Excel history migration** | The existing sheet is stale; manual backdated entry covers the need. |
| **Deep historical backdating** | Backdating starts **July 2026**. This single cap dissolves the raw-OHLC problem, the deep-history argument, and the split reconciliation of long-past trades. |
| **EODHD and any paid bar source** | Ruled out in [#12](https://github.com/ajitimur/automatic-trading-journal/issues/12). yfinance is a permanent commitment, not a free tier to graduate from. The generic adapter *seam* stays; a second adapter does not. |
| **Unconditional setup-selection analysis** | The journal records only Trades **taken**, never setups passed on, so "which setups work" is permanently conditional on the trader's own filter. No export shape lifts this; it ships as a legend caveat instead. |
| **The inventory of parser correction rules** | The *mechanism* is specified ([§5.4](#54-corrections-a-fact-once-a-rule-forever)); the class list is not. Only writing the parsers can fill it, and a guessed inventory inside a locked spec would read as decided. |

---

## 3. Domain model

Full definitions in [`CONTEXT.md`](CONTEXT.md). This section states the shape and the rules that bind the entities together.

### 3.1 Entities

| Entity | Identity | Notes |
| --- | --- | --- |
| **Book** | `US` \| `IDX` | A discriminator on one model, not two models. Carries currency, benchmark, broker, lot convention. **Nothing is ever aggregated across books.** |
| **Fill** | `(source, source_ref, revision)` | Append-only, never edited. The source of truth. |
| **Trade** | surrogate | The unit of analysis: an **entry-day cohort** — one symbol, one book, one entry date. Derived from Fills; recomputed, never matched. |
| **Exit** | surrogate | An allocation of exit fills to a Trade, with its own date, quantity, price, reason and excursion. A Trade may have many. |
| **Position** | `(book, symbol, date)` | Broker-level net holding. **Reconciliation only** — never the thing journaled. |
| **EquitySnapshot** | `(book, date)` | What a Book was worth on a date, marked to market. |
| **RegimeSnapshot** | `(book, date)` | The Book's market environment on a date. Computed daily per book; Trades reference it, never copy it. |

`Trade` and `Position` must never be used interchangeably: a Trade spans several Fills, a Position spans several Trades.

### 3.2 Hand-entered fields — exactly two

`stop` and `setup`. Nothing else is typed on the import path.

Both earn their keystrokes because nothing else can supply them. The stop is genuinely discretionary and is not derivable from bars; without it there is no Risk Percentage and no Realized R. The setup is the only input the setup-selection goal has.

- **`setup`** vocabulary: `base_breakout`, `high_tight_flag`, `other`. Accumulating `other` is the signal to name a third setup.
- **`stop`** is immutable once set, and locks at freeze.
- **`stop_provenance`** is **derived, never typed**: `recorded` if the stop arrived before the Trade's first Exit, `reconstructed` if after. No self-reported confidence scale.

### 3.3 Exit reasons — a fixed vocabulary

`planned_partial_day3`, `planned_partial_day5`, `close_below_ma10`, `close_below_ma20`, `stopped_out`, `written_off`, `other`.

**Proposed by enrichment from the bars, confirmed or overridden by the trader.** Because the strategy is mechanical, most exits are inferable, so this is groupable data at near-zero keystrokes — and the gap between the proposed and the confirmed reason is itself a discipline signal.

### 3.4 FIFO allocation

Two Trades in the same symbol on the same book can be open simultaneously against one pooled broker holding, and the broker does not say which one an exit belongs to.

**Exit fills allocate FIFO — oldest open Trade first — overridable at confirm.** An override may never allocate a Trade more than it holds open. Pro-rata was rejected: splitting one exit across two Trades smears exit-quality analysis, which is the thing the journal most needs to measure. See [ADR 0001](docs/adr/0001-trade-is-an-entry-day-cohort.md).

### 3.5 Lifecycle

```
open → closed → enriched → frozen
```

**Freeze fires 20 trading days after the final Exit.** Three mutability classes, not four stage rules:

| Class | Rule |
| --- | --- |
| Fills | Append-only. A broker restatement arrives as a new `revision`; earlier revisions are retained. |
| Hand-entered fields | Editable until frozen, then locked. |
| Derived fields | Recomputable forever, but **snapshotted at freeze**, so a later disagreement surfaces as **drift**. |

**Freezing without a stop makes the hole permanent** — that Trade has no Risk Percentage and no Realized R, ever. This is a real and accepted cost of making the stop chaseable ([§5.5](#55-stop-and-setup-are-chaseable)); the review surface's banner states the remaining fuse per item for exactly this reason.

A `written_off` Exit **freezes the Trade immediately**: the post-exit window is meaningless for a symbol with no further trading days. Its post-exit fields and all six counterfactual variants record `not_applicable`. It stays **in** R aggregates (a real stop, a real outcome) and **out** of exit-quality and adherence (no exit decision was made). Price is hand-entered — a write-off is not reliably total.

### 3.6 Drift

**Drift means one thing and one thing only: a fact from outside the journal moved.** Guarding that single meaning drives several rules that would otherwise look arbitrary.

Drift carries a **cause**, and the cause decides what may be done:

| Cause | Behaviour |
| --- | --- |
| **Broker restatement** | The fact under the snapshot was wrong, so it **may be applied**. The superseded snapshot is kept beside the corrected one — the same append-only shape the Fill ledger uses. |
| **Revised bar series** | **Acknowledge only, never applied.** Nothing was wrong at freeze; overwriting would destroy the record of what was believed then, which is the entire point of freezing. |

**Three things that are *not* drift**, each a hole being filled rather than a number moving:

1. A **book-history value changing** because a backdated Trade landed among its neighbours ([ADR 0004](docs/adr/0004-book-history-values-are-not-pinned.md)).
2. A **counterfactual variant resolving after freeze** — it was always going to land late.
3. An **EquitySnapshot arriving** dated at-or-before a Trade's entry and closer than the one in use, turning a null into a number. A snapshot's *value* being **corrected**, however, **is** drift.

---

## 4. Data sources

### 4.1 Trades — IBKR (US book)

**Flex Web Service, Activity Flex Query at `Level of Detail = Executions`.** Runs unattended: no browser, no 2FA. Fallback is scheduled delivery of the same query. Full findings: [`docs/research/ibkr-trade-export.md`](docs/research/ibkr-trade-export.md), verified against real files in [`docs/samples/ibkr-flex-findings.md`](docs/samples/ibkr-flex-findings.md).

Facts the parser must honour — **two of these correct the documentation**:

- One row per fill.
- **Commission appears on every fill, pro rata** — *not* on the first fill only, as IBKR's documentation states. Sum `ibCommission` across an `ibOrderID`. The documented rule would undercount by roughly 60% on multi-fill orders.
- Trade timestamps are **US Eastern** (settled empirically: zero fills before `093000`).
- Exec ids are `<base>.<seq>`; `source_ref` uses the native execution id.
- Flex errors return **HTTP 200 with an XML error body**. Status-code checking is blind to every Flex failure. An empty series must be treated as an error, never as a value.
- `SendRequest` and `GetStatement` sit on **different hosts** (`ndcdyn` → `gdcdyn`, named in the response). Both are subject to the DNS interception in [§13.3](#133-schedule-and-dns).
- The Flex token maxes at **1 year** and currently expires **2027-07-14**. **Generating a new token invalidates the existing one** — which would kill the trades job if done carelessly.

### 4.2 Trades — Stockbit (IDX book)

**No API, and none permitted by the Terms of Service.** Intake is the daily **Trade Confirmation (TC) PDF**, hand-dropped ([§13.2](#132-intake)). Full findings: [`docs/research/stockbit-trade-export.md`](docs/research/stockbit-trade-export.md), verified in [`docs/samples/export-findings.md`](docs/samples/export-findings.md).

- **The TC preserves individual fills** (fills of one order share a `REF #`). The **Statement of Account collapses them to a weighted average** — lossy and not invertible, which is why the TC is the intake path and the SoA is not.
- Both `Lot` and `Quantity` columns exist, so no 100× inference is needed. Store shares canonically (lot = 100).
- **Fees are itemised per side per day and reconcile exactly**: buy `+0.15% + Rp10,000` stamp duty; sell `−0.15% − 0.10%` tax. This gives the parser a **daily self-check** — see the document-level gate in [§5.6](#56-where-the-brokers-differ).
- Cost attaches at the **day+side** level here, not per fill. Do not force IBKR down to this coarser shape ([§7.0](#70-cost-attribution)).
- `source_ref` is a deterministic content hash over confirmation date, symbol, side, quantity, price and ordinal within the document, which makes re-dropping the same statement idempotent ([ADR 0003](docs/adr/0003-fills-are-append-only.md)).
- TC PDFs carry name, address, NPWP/NIK, phone and account number. The raw archive stays **local or encrypted** and never enters a repo or an unencrypted sync folder.

### 4.3 Daily bars — yfinance, both books

**yfinance for both markets, permanently.** Full comparison: [`docs/research/ohlcv-sources.md`](docs/research/ohlcv-sources.md).

It is the only candidate covering US equities, IDX, and both benchmark families under one symbology. **The Yahoo automated-access ToS breach is accepted knowingly** for a private single-user journal. That decision is *not* symmetric with the Stockbit ToS call, and the asymmetry is worth keeping because the same shape will recur: Stockbit holds money and offered no legitimate automated path at all, whereas Yahoo holds free public quote data, the worst case is an IP block, and commercial alternatives sit behind the same interface.

- Indicators read the **split-adjusted, dividend-unadjusted** series (`auto_adjust=False`). It is the series a chart-reading momentum trader is actually looking at, and it keeps High/Low honest. Never mix `Adj Close` into an OHLC-based indicator.
- Dividends ship in the same call (`actions=True`), which is what makes `dividend_drag_r` cheap ([§7.7](#77-corporate-actions)).
- **yfinance's own session handling (cookie + crumb negotiation) is load-bearing.** A hand-rolled HTTP client against Yahoo returns 429 on the first request. Use the library, with retry and backoff.

### 4.4 The bar pipeline

Four rules, all load-bearing.

**A zero-volume row is not a trading day — invariant.** Discarded at the bar-cache boundary, before any consumer sees it, uniformly across both books. A suspension does not arrive as a gap; it arrives as a row with price flat at the prior close and volume zero. Left in place it is wrong in four directions at once: it deflates `adr_pct` (the journal's single normalizer, so every ADR-denominated number inflates), collapses 20-day realized volatility toward zero, shortens the moving averages in real terms, and lets a trail signal fire on a day nothing traded. One filter fixes all four, and it matches what the trader's chart draws. **Every window the journal measures counts traded days** — ADR%, the MAs and their slopes, realized volatility, the freeze fuse, the post-exit window, the 60-day counterfactual bound. See [ADR 0005](docs/adr/0005-a-zero-volume-row-is-not-a-trading-day.md).

**Span check, as a hard gate.** On every fetch, reject any series that does not span the Trade's own date range. A failure is an **error requiring manual repair** — never "no data, skip enrichment". This catches both failure modes, and the second is the dangerous one: `TWTR` and `SIAP.JK` return empty (a visible loss), but a reused ticker returns rows of a completely unrelated instrument with nothing flagging it (silent corruption — an MA200 from the wrong company).

Three things the span check must **not** confuse with a failure:

- A **filtered zero-volume day** is present, not missing. The filtered count belongs in the check's diagnostics, where a long suspension is visible without being an error.
- **`insufficient_history`** ([§7.8](#78-insufficient-history)) is a fact about the instrument, not a fault in the data. Merge the two and every recent IPO presents as a broken fetch.
- A **date with an EquitySnapshot and no bar** is normal ([§9.5](#95-the-calendar-axis--invariant)).

**Local bar cache — part of the design, not an optimisation.** Bars fetched once are stored; the pipeline reads the cache, the daily job fills it. If Yahoo blocks or breaks, enrichment stops but nothing already computed is lost and the journal still opens. yfinance will not reliably re-serve a delisted name later.

**Source-adapter seam.** Everything above the fetch layer speaks a market-neutral bar interface rather than calling `yfinance` directly. Kept despite EODHD being ruled out — what it protects against is not "we chose to pay" but "Yahoo broke and we need *anything* else by Friday". **No second adapter is written speculatively.**

Every fetch records `fetch_date`, `source`, and its span-check result. A Trade is fetched more than once (entry, daily while open, post-exit window), so this is per-fetch metadata, not per-Trade.

---

## 5. Intake — the confirm queue

Owned by [#10](https://github.com/ajitimur/automatic-trading-journal/issues/10). Interactive prototype: [`docs/prototypes/confirm-and-enrich-flow.prototype.html`](docs/prototypes/confirm-and-enrich-flow.prototype.html) — a pure reducer plus eight walkthroughs, and the part worth lifting into the real module.

### 5.1 One door

**One confirm queue is the only thing in the system that commits.** Broker imports and hand-entered backdated Trades arrive through it identically and produce *proposals*; the daily job enriches but never commits. Backdated entry needs no second surface — it differs only in that stop provenance reads `reconstructed`.

The path: **drop → parse → dedupe → reconcile → propose → confirm.**

### 5.2 Every failure is a queue item

Eight proposal kinds, and **every failure is one of them, never an exception**:

`new-trade` · `add-fills` · `exit-allocation` · `restatement` · `quarantine` · `orphan-exit` · `enrichment-repair` · `drift`

**Blocked items park; they never stall.** A parked item sinks below the confirmable ones and confirm skips it — otherwise one orphan exit halts the week's whole import. Parked items are re-evaluated against the journal as it now stands (`RECHECK`), so entering a missing Trade by hand clears the orphan that was waiting on it, with no re-drop.

### 5.3 Reconciliation and dedupe

- **Buys group by `(book, symbol, entry date)` — that grouping *is* the Trade.** Same day merges into the existing Trade; **a different day is a separate Trade, never an addition**. The proposal says so explicitly, because it is the counter-intuitive case.
- **Sells allocate FIFO across open Trades, overridable**, bounded by what each Trade holds open.
- A sell with no journaled entry is an `orphan-exit` and **parks**. Guessing an entry would poison every exit metric.

**Three kinds of "seen this before":**

| Case | Behaviour |
| --- | --- |
| Same document, another filename | Silent no-op. **Dedupe on extracted content, never filename or message-id** — the July SoA arrived twice under different names. |
| Same fills | Idempotent via `source_ref`. |
| A *restated* fill | New revision; the superseded row is retained. If the Trade already froze, the restatement becomes **drift**, not a rewrite. |

### 5.4 Corrections: a fact once, a rule forever

This is the answer to "am I re-confirming the same misparse every week":

- A wrong **quantity** is a *fact* about one fill → corrected here, **not remembered**.
- A wrong **symbol** is a *rule* about the parser → **remembered**, applied **before anything reaches the queue** again, and it **repairs Trades already committed** under the wrong symbol.

So: you are not re-confirming the same misparse — but only for the class of misparse that is a rule.

### 5.5 Stop and setup are chaseable

**Confirm demands nothing.** A Trade commits with no stop; Exposure Percentage is computed regardless, Risk Percentage and R are held open. On a busy day, demanding a stop is friction at exactly the wrong moment.

**The nag lives in the daily job, not the confirm queue** — the queue is for decisions on new information, not a standing to-do list. It surfaces in the review surface's banner ([§11.4](#114-the-attention-banner)). Provenance falls out of *when* the stop arrives, not anything typed.

### 5.6 Where the brokers differ

**In exactly one visible place.** The queue itself looks identical for both books.

Stockbit's **fee identity is a document-level gate**: recompute the printed total from the parsed rows using the fee formulas in [§4.2](#42-trades--stockbit-idx-book). A shifted column — `Lot` read as `Quantity`, a silent 100× error — **quarantines the whole document before a single fill lands.** IBKR has genuine per-fill cost and no equivalent tripwire.

### 5.7 Enrichment has two clocks

| Clock | Fields | When |
| --- | --- | --- |
| **Entry-dated** | Risk %, Exposure %, all of [§7.2](#72-b-setup-geometry--as-of-the-prior-close) | The moment the Trade commits |
| **Rolling / post-exit** | [§7.6](#76-g-live-while-the-trade-is-open), [§7.5](#75-e-excursion-and-f-post-exit), all counterfactuals | The daily job |

**Missing bars never block the commit** — the fills are facts. The Trade commits and enrichment is *held for repair*, recorded as a repair and **never as "no data"**. The daily job does not blindly retry a held Trade.

### 5.8 Bulk confirm covers exit reasons, and nothing else

A week of statements is mostly agreeing with the exit reasons enrichment proposed from the bars, so those go in one action. Parked items are untouched by it.

**New Trades stay one at a time.** A mis-parsed quantity there poisons everything downstream, and catching it is precisely what confirm exists for.

> **Consequence, and it is a real design constraint:** the exit-reason proposal must be good enough to **accept unread**, because in the weekly rhythm it will be. That accepting-unread guarantees some wrong ones land, which is why the review surface's exit-reason override is structurally required rather than a convenience ([§11.3](#113-scope-and-actions)).

### 5.9 What the human sees

The **interpreted Trade** is the default view — symbol, book, entry date, quantity, average price, notional, and what it will do to existing records — with the **raw broker rows one disclosure away**. Enough to catch a wrong quantity without reading a spreadsheet.

---

## 6. Enrichment — conventions

Owned by [#7](https://github.com/ajitimur/automatic-trading-journal/issues/7), with amendments from [#9](https://github.com/ajitimur/automatic-trading-journal/issues/9), [#14](https://github.com/ajitimur/automatic-trading-journal/issues/14) and [#17](https://github.com/ajitimur/automatic-trading-journal/issues/17). Every field here must be derivable from Fills plus daily bars, or it cannot exist.

### 6.1 The normalizer

**ADR% is the single unit of comparison. ATR is dropped.**

```
adr_pct = mean over the last 20 completed bars of (High_i / Low_i − 1) × 100
```

ADR% is scale-free, so a $400 US name and an IDR 7,200 IDX name compare directly with no per-book currency handling. ATR is absolute and would have needed exactly that, to buy nothing ADR% does not already give. **R survives as an *outcome* unit, never as a normalizer for chart geometry.**

### 6.2 Moving averages

**SMA throughout, all five of 10/20/50/100/200.** This is load-bearing rather than cosmetic: the mechanical exit rule is "a close below MA10", and the adherence engine grades against it. An EMA10 in the journal against an SMA10 on the trader's chart would misgrade every borderline exit. Confirmed against what the trader actually has on screen.

**Slope**, wherever used, is an MA's **percent change over the last 5 trading days, sign only** — no flat-zone threshold. A threshold is a free parameter that would have to be tuned and defended.

### 6.3 Anchors

Four anchors, each with one job. Getting these wrong is the main way this field list produces numbers that look right and mean nothing.

| Anchor | Symbol | What it anchors |
| --- | --- | --- |
| **Prior close** | `P₋₁` — close of the last completed bar before entry date | All entry-dated setup geometry |
| **Entry average price** | `entry_avg_price` — quantity-weighted mean of the Trade's entry fills | Chasing, risk, and all excursion baselines |
| **Exit-day close** | `C_x` — close of the final exit date | Exit geometry, and the post-exit counterfactual baseline |
| **Exit average price** | `exit_avg_price` — quantity-weighted mean of all exit fills | Realized outcome; stored beside `C_x` so the gap stays visible |

**Entry uses the prior close; exit uses its own close. This asymmetry is deliberate and must not be "fixed" later.** Entry is a discretionary intraday act, so the last information the trader actually had was the prior completed bar — anchoring on the entry day's close would leak post-decision information into a measurement of the decision. The exit rule is *triggered by a close*, so the exit day's close is a decision input, not a leak. Encoding the asymmetry describes both sides honestly; forcing symmetry would misdescribe one of them.

**One flagged exception:** `volume_ratio` uses the **entry day's** volume. It describes the breakout bar rather than the decision, and breakout volume is the entire point of the field.

**Quantity-weighting is load-bearing.** `price × quantity` must equal the cash that actually left the account — Risk %, Exposure % and R all tie back to real money through that property. First-fill would flatter R on every scaled entry; a simple mean would let a 10-share fill count as much as a 500-share one.

> **Naming:** these are `entry_avg_price` / `exit_avg_price`, **not** `entry_vwap` / `exit_vwap`. "VWAP" means the market's intraday benchmark to any trader reading this cold; here it means the weighted mean of the Trade's *own* fills.

### 6.4 Store primitives, derive on read

**Booleans, orderings and units are derived, never stored.** `ma_dist_N` carries the continuous signed distance; "above MA50" is `ma_dist_50 > 0`. Excursions store raw highs/lows and their dates; R, ADR and % forms are computed on read.

This is what lets a consumer present whichever form it wants without re-enrichment, and lets thresholds move by query rather than by recompute. `stack_state` is the single exception — one categorical earned by how often it will be grouped on.

The payoff was collected: the review surface ([§11](#11-the-weekly-review-surface)) needed **no new field**, and the counterfactual engine's per-variant legs stay usable for no-stop Trades precisely because they are stored as prices rather than as R.

### 6.5 Pinning

**Every derived field declares an as-of date** — either a fixed date (entry, exit) or rolling-to-now. All of them snapshot at freeze, and a later disagreement surfaces as drift.

**One narrow exception**, and it is narrow by design: **book-history values are computed at read time and are never pinned** ([ADR 0004](docs/adr/0004-book-history-values-are-not-pinned.md)). Pinning defends against an untrusted upstream; these have none, since their only inputs are the journal's own append-only records. A backdated Trade renumbers its successors and moves its neighbours' drawdown — correctly, and silently.

---

## 7. The enrichment field list

### 7.0 Cost attribution

**Cost attaches at fill level where the broker provides it, and only falls back to an allocation where it does not.** IBKR does (pro rata per fill); Stockbit does not (day + side). Do not force both books to the coarser shape.

### 7.1 A. Volatility base

| Field | Formula | As-of | Goal |
| --- | --- | --- | --- |
| `adr_pct` | mean of `(High/Low − 1) × 100` over 20 completed bars | `P₋₁` | normalizer (all) |

### 7.2 B. Setup geometry — as-of the prior close

| Field | Formula | Goal |
| --- | --- | --- |
| `ma_10`…`ma_200` | SMA of close over N completed bars, N ∈ {10, 20, 50, 100, 200} | setup |
| `ma_dist_10`…`ma_dist_200` | `(P₋₁ − ma_N) / P₋₁ × 100 ÷ adr_pct` — signed, in ADR units | setup |
| `stack_state` | `aligned_up` if `ma_10 > ma_20 > ma_50 > ma_100 > ma_200` strictly; `aligned_down` if strictly reversed; else `mixed` | **setup only** |
| `prior_move_21d` / `_63d` / `_126d` | `(P₋₁ / P₋₁₋N − 1) × 100`, close-to-close over N **trading** days | setup |
| `pct_off_52w_high` | `(P₋₁ / max(High over last 252 bars) − 1) × 100` — zero or negative | setup |
| `rs_63d` | `prior_move_63d(symbol) − prior_move_63d(benchmark)`, in percentage points | setup, regime |
| `volume_ratio` | `Volume(entry day) ÷ mean(Volume over 50 completed bars before entry)` | setup |
| `avg_turnover_20d` | `mean(Close_i × Volume_i)` over 20 completed bars, **native currency** | setup, sizing |

**Trading days, not calendar months.** Calendar months hold variable bar counts, and the US and IDX holiday calendars differ, so calendar windows are not comparable across books.

**Benchmarks: QQQ for US, `^JKSE` for IDX** — the same benchmarks regime uses ([§8](#8-market-regime)). The alternative is incoherent: regime would describe one market while a Trade's standing was measured against a different one, even though the regime goal reads the two together.

**`stack_state` serves setup selection only.** It is the *symbol's* own MA ordering. Regime is the *benchmark's* MA state, referenced via `RegimeSnapshot` and never copied onto a Trade. Two different things that would otherwise blur under a shared tag.

**Why `rs_63d` lives here and not in regime:** RS is per-*Trade* — this symbol's standing within its market. Regime is per-*market*. Easy to conflate; not the same field.

### 7.3 C. Execution

| Field | Formula | As-of | Goal |
| --- | --- | --- | --- |
| `entry_avg_price` | `Σ(price × qty) ÷ Σ qty` over the Trade's entry fills | entry fills | all |
| `stop_distance_adr` | `(entry_avg_price − stop) / entry_avg_price × 100 ÷ adr_pct` | entry fills | sizing |
| `chased` | `stop_distance_adr > 1.0` — **derived, not stored** | — | sizing, setup |
| `risk_percentage`, `exposure_percentage` | see [§9](#9-equity-risk-and-exposure) | entry-dated | sizing |

**Chasing is defined against the stop, not against a moving average.** If the distance from entry to stop is wider than a typical day's range, the symbol has to move more than a full average day against the Trade merely to take it out.

This beats extension-from-MA10 for a structural reason: on a breakout the stop sits under the pivot, so entering late does not move the stop — it stretches the distance to it. The stop is the trader's own reference point; MA10 is an imported one. It also **costs nothing new**: it is the *same numerator* as Risk Percentage. `(entry − stop)` normalized by equity answers sizing; normalized by ADR answers chasing. One primitive, two questions.

**It inherits the stop-provenance caveat**: chase analysis excludes Trades whose `stop_provenance` is `reconstructed`, exactly as adherence scoring does.

### 7.4 D. Exit geometry — as-of the final exit day's close

Full symmetry with section B, same formulas, anchored on `C_x`: `adr_pct_at_exit`, `ma_dist_10_at_exit` … `ma_dist_200_at_exit`, `stack_state_at_exit`, `rs_63d_at_exit`, plus `exit_avg_price`.

Exit quality is a whole learning goal and needs the same chart geometry the entry got — *"I exited 6 ADR extended"* is precisely the finding being hunted. Recomputing identical formulas at a second date is nearly free. Individual Exits stay lean: date, quantity, price, reason, and their own excursion.

### 7.5 E. Excursion, and F. Post-exit

**E — in-trade excursion.** Stored as raw primitives, units derived on read:

| Field | Definition |
| --- | --- |
| `mfe_high`, `mfe_date` | maximum `High` in the window, and the date it occurred |
| `mae_low`, `mae_date` | minimum `Low` in the window, and the date it occurred |

```
in R    = (mfe_high − entry_avg_price) / (entry_avg_price − stop)
in ADR  = (mfe_high − entry_avg_price) / entry_avg_price × 100 ÷ adr_pct
in %    = (mfe_high / entry_avg_price − 1) × 100
```

**Two scopes, both stored** — this is the answer to the partial-exit problem:

- **Trade-level**: entry date → **final** exit date, position-agnostic. Measures *the move*, which is what setup selection asks about.
- **Per-Exit**: each Exit carries its own four primitives over entry date → **that Exit's** date. This is **the exit-quality grading unit**.

The strategy sells a fraction on day 3 or 5 and rides the rest, so a Trade has a window in which only part of the position was live. *"Was the day-3 partial early?"* and *"was the ride good?"* are different questions, and only the per-Exit scope can answer the first. **Quantity-weighting was rejected here** — it blends both into a single number that answers neither.

**Excursion dates are stored, not just levels.** *"The high came on day 2 and I held nineteen more"* is independently a finding — and it is what the review surface's timeline is drawn from.

**F — post-exit counterfactual.** Window: **20 trading days beginning the day after the final exit date.** Baseline `C_x`.

| Field | Definition |
| --- | --- |
| `fwd_return_20d` | `(C_20 / C_x − 1) × 100` |
| `fwd_close_20d` | close on the 20th trading day |
| `fwd_high`, `fwd_high_date` | maximum `High` in the window, and its date |
| `fwd_low`, `fwd_low_date` | minimum `Low` in the window, and its date |

**Baselined on `C_x`, not `exit_avg_price`.** The close is execution-noise-free and comparable across every Trade, which is what a counterfactual needs. `exit_avg_price` sits beside it so the gap between *what was got* and *what the day was worth* stays derivable rather than silently baked in.

These are **null until the window completes**, and completing it **is** the freeze trigger. No second clock.

### 7.6 G. Live while the Trade is open

**Only four fields recompute daily:**

| Field | Definition |
| --- | --- |
| `ma_dist_10_live`, `ma_dist_20_live` | section B formula against the latest close |
| `days_held` | trading days since entry date |
| `open_r` | `(latest_close − entry_avg_price) / (entry_avg_price − stop)` |

Everything else is entry-dated and never rolls. A rolling MA200 distance answers no question being asked, and the daily job pays for every field it rolls.

### 7.7 Corporate actions

| Field | Formula | As-of | Goal |
| --- | --- | --- | --- |
| `dividend_drag_r` | `sum(dividend_per_share over the Trade's window) / (entry_avg_price − stop)` | entry-dated, pinned | sizing, exit quality |

**Realized R is a pure price measure — no dividend term, ever.** Every other number on the row is price-derived, so a total-return R would be the one field that does not tie to the others, and `capture_ratio` would compare unlike units. The strategy is a price strategy: the trigger is a close below MA10, not a total return. And the cash is genuinely expensive — IDX dividend cash is absent from the *daily* TC (it appears only in the monthly SoA), IBKR needs a second Flex section, and worst, **a dividend accrues to a Position, not a Trade**, so attributing it would need the same FIFO machinery as exits.

**But attributing the cash is expensive while detecting the drop is free.** yfinance ships dividends in the same call as the bars, and the drag is arithmetic over fields the Trade already stores. It earns its keystroke against the map's standing bias because the magnitude is not noise: an IDX name yielding ~3.5% with a stop ~8% below entry is **~0.44R of phantom loss**, and a ~20-day hold crosses an ex-date roughly **1 time in 8** on IDX. US momentum names mostly pay little, so **the effect is asymmetric across books** — exactly the normalization the export leans on to make two books one file. A legend caveat can say "IDX R is understated somewhere" but not *by how much on this row*, so a reader either ignores it or over-discounts every IDX trade.

Constraints: sits **beside** Realized R, never folded in. **Null, not zero**, where the window crossed no ex-date. Pinned at freeze. **Trade-level only — no per-variant drag.**

**The Counterfactual does not adjust for ex-date gaps.** An ex-date drop is a *real price move*: the stock really opens there, and a real stop really would have filled. The engine's job is the chart the trader actually watched, and that chart is the same dividend-unadjusted series.

### 7.8 Insufficient history

**Null the field with an explicit `insufficient_history` marker. Never compute a short-window substitute** — a 40-bar "MA200" is a silently wrong number, which is worse than an absent one.

Null propagation, explicitly:

- `< 20` completed bars → `adr_pct` null → **every ADR-normalized field null** (all `ma_dist_*`, `stop_distance_adr`, ADR-unit excursions)
- `< N` completed bars → `ma_N` null → `ma_dist_N` null → `stack_state` null
- `< 252` bars → `pct_off_52w_high` null
- benchmark series shorter than 63 bars → `rs_63d` null

**Strictly distinct from a span-check failure** ([§4.4](#44-the-bar-pipeline)). This is a fact about the instrument and needs no action at all; that is a corruption risk demanding manual repair.

### 7.9 Book history

Anchored on the **book's own record** rather than on bars. Not pinned, per [ADR 0004](docs/adr/0004-book-history-values-are-not-pinned.md).

| Field | Formula | As-of | Goal |
| --- | --- | --- | --- |
| `seq` | ordinal of the Trade on its book by `entry_date`, ascending, 1-based | n/a — a fact about the book, not a date | ordering primitive |
| `book_drawdown_r_at_entry` | `peak(cum_realized_r) − cum_realized_r`, over closed Trades on the book with a recorded stop, evaluated at `entry_date` | entry-dated but **live** — recomputed, never pinned | sizing, setup selection |

**Four candidate fields in this class were rejected**, and the reasoning generalises: `prior_trade_r`, `consecutive_losses` and `open_trades_at_entry` are all derivable by the export reader from `seq`, `entry_date`, `exit_date` and `realized_r` already on every row, so shipping them would be denormalized copies that can disagree with the rows they came from. `days_since_last_exit` also **conflates two mechanisms**, firing on a day-3 partial of a Trade still happily held.

**`book_drawdown_r_at_entry` alone survived**, because it needs the book's *complete* history, which a sliced export lacks.

Two consequences worth recording:

- **It is not built on `EquitySnapshot`.** An equity level has no cash-flow term — a tax withdrawal would read as a drawdown never felt — and snapshots are explicitly permitted to be sparse: fine for a denominator, useless for a high-water mark. The R curve is dense at every close and immune to cash flows.
- **It inherits the stop-provenance caveat.** Trades with no recorded stop have no R and are skipped by the curve, which therefore understates any stretch containing them; the excluded count per book ships with the export. Below **20 closed Trades with recorded stops** the field is `insufficient_history` — **not** a drawdown of zero.

### 7.10 Learning goal coverage

| Goal | Fields |
| --- | --- |
| **Setup selection** | Section B in full; Trade-level excursion; `book_drawdown_r_at_entry` |
| **Exit quality** | Section D in full; per-Exit excursion; section F; `dividend_drag_r` |
| **Sizing** | `stop_distance_adr`, `chased`, `risk_percentage`, `exposure_percentage`, `avg_turnover_20d`, `book_drawdown_r_at_entry` |
| **Regime** | `rs_63d`, the `RegimeSnapshot` ([§8](#8-market-regime)) |

---

## 8. Market regime

Owned by [#9](https://github.com/ajitimur/automatic-trading-journal/issues/9).

**Regime is a property of a market on a date, not of a trade.** It lives in its own `RegimeSnapshot`, keyed `(book, date)`, computed once per book per day by the daily job. A Trade holds two *references* into it — entry and exit — and never copies the values.

### 8.1 Benchmarks

| Book | Series |
| --- | --- |
| US | **QQQ** |
| IDX | **`^JKSE`** (IHSG) |

**The two books' regimes are strictly independent** — no US term folds into the IDX label. Nothing aggregates across books, and the cross-market question stays fully answerable at analysis time by joining on date, since both series are cached anyway. Baking a correlation assumption into a stored label would be unrecoverable.

**QQQ over `^NDX`:** the purity case is real but thin. QQQ's dividend and fee drift is ~0.003%/day — ~0.02% against an MA10 — far below daily noise, so a cross would differ by a day at most and rarely. `^NDX`'s deeper history buys nothing against a July-2026 backdating cap. Against that, QQQ is the chart the trader actually watches, and **a regime label that contradicts the trader's memory of the tape is a label they will argue with.**

**`^JKSE` over `^JKLQ45`:** the trader trades beyond the large-cap 45, so the whole-market index is the right weather.

### 8.2 The measure

Six primitives, **all stored**: close above/below **MA10, MA20, MA50** (3 booleans), and each MA's **slope** (3 numbers, per [§6.2](#62-moving-averages)).

The MA set is deliberately shorter than the conventional regime toolkit — no MA200 — because it mirrors the horizon actually traded, and the strategy's own exit rule is a close below MA10/MA20.

**Breadth is ruled out, not deferred.** No feed exists at any tier. Computing it means ~500 daily constituent fetches against an undocumented, 429-prone endpoint, and it needs a constituent-membership history that does not exist here — which makes any backdated breadth number survivorship-biased. LQ45 would be borderline feasible; the US side is not, and a measure existing on only one book defeats cross-book comparability.

### 8.3 The label

A **5-level named ordinal**. Let `above` = how many of MA10/20/50 the close is above (0–3), `rising` = how many slopes are positive (0–3). Evaluated **top-down**:

| Regime | Rule |
| --- | --- |
| `strong_uptrend` | `above` = 3 and `rising` = 3 |
| `uptrend` | `above` ≥ 2 and `rising` ≥ 2 |
| `strong_downtrend` | `above` = 0 and `rising` = 0 |
| `downtrend` | `above` ≤ 1 and `rising` ≤ 1 |
| `neutral` | everything else |

No tunable parameters; mutually exclusive by evaluation order; the two "strong" bands are strict so they stay rare enough to mean something.

**Both books use this rule identically, untuned.** Per-book thresholds would make "I do badly in IDX downtrends" incomparable to the US finding. If IHSG turns out to sit in `neutral` 80% of the time, that is a finding to act on later, not a parameter to pre-tune.

**The primitives are stored regardless of the label**, so the label can be re-cut retroactively with no re-fetch and no re-derivation.

### 8.4 Also stored, outside the label

On the same `RegimeSnapshot`, from the same benchmark series: the index's **distance from its 52-week high**, and its **20-day realized volatility** (over traded days). Neither feeds the label. They exist because *"was I trading in a high-vol tape"* cannot be reconstructed once a Trade is frozen.

These are at the *market* level. `pct_off_52w_high` in [§7.2](#72-b-setup-geometry--as-of-the-prior-close) is the same formula at the *symbol* level — complementary, and both are wanted.

### 8.5 Stamping

**Both stamps are as of the prior trading day's close**, each carrying its as-of date. The entry day's own close is not known at entry, so stamping it would record information the trader did not have. Regime therefore always means *the weather observable when the decision was made*.

> The setup-geometry anchor in [§6.3](#63-anchors) reached the same prior-close rule independently, by a separate route. That convergence is decent evidence the rule is right rather than an artifact of one session's reasoning.

**Missing bar** (holiday, suspension, or a calendar mismatch on a backdated entry): use the **last available index close on or before** the as-of date, and **record the bar date actually used**. The as-of date stays honest rather than silently sliding.

**The path in between is a range scan over snapshots, not stored rows.** *"Regime flipped mid-trade"* is a query, not a field — the index bars are already cached.

---

## 9. Equity, risk and exposure

Owned by [#16](https://github.com/ajitimur/automatic-trading-journal/issues/16), on research from [#15](https://github.com/ajitimur/automatic-trading-journal/issues/15) verified in [#19](https://github.com/ajitimur/automatic-trading-journal/issues/19). Findings: [`docs/research/broker-equity-reporting-ibkr.md`](docs/research/broker-equity-reporting-ibkr.md), [`docs/research/broker-equity-reporting-stockbit.md`](docs/research/broker-equity-reporting-stockbit.md), [`docs/samples/equity-sources-findings.md`](docs/samples/equity-sources-findings.md).

### 9.1 What equity means

**Mark-to-market NAV, not deposited capital.** Deposited capital is not derivable on either book, but the stronger reason is that it is the wrong number: 1% of what the account *has*, not of what was once put in, is what a position-sizer uses. **Under deposited capital a losing streak silently raises real risk while the display still reads 1%.**

This leaves `EquitySnapshot` with **exactly one job — the risk/exposure denominator.** It is not the book's equity curve and nothing should grow toward making it one. (Book Drawdown was deliberately moved off it — see [§7.9](#79-book-history).)

### 9.2 Two creation mechanisms

The books get structurally different answers, and forcing a single mechanism was never on the table.

**IBKR — automatic, and capture is the whole point.** A **second** Activity Flex Query carrying the **NAV Summary in Base** section (`EquitySummaryInBase` is the XML element). Every `reportDate` row becomes a snapshot, written by the daily job, backfilled on first run.

- The denominator field is **`total`** — not `cash`, which can go negative on margin. **`netLiquidation` does not exist in Flex**; that is TWS vocabulary.
- Verified against real data: it **is** a daily series (262 rows, one per `reportDate`, zero gaps). IBKR's *prose* misleads because it describes the PDF layout. `total` equalled `cash + stock` on 223 of 262 rows, the 39 residuals peaking at **0.0061%** — accruals outside the two buckets, and a reason to prefer `total` over reconstructing it.
- **The reachable window is a rolling 365 days.** The July 2026 floor therefore **ages out around July 2027**, so snapshots must be **captured and persisted, never re-derived** — the NAV XML joins the keep-raw-forever tier ([§13.5](#135-durability)). Anything uncaptured is gone permanently.
- The series is **weekday-dense, not trading-day-dense** — a row on all ten US market holidays. See [§9.5](#95-the-calendar-axis--invariant).

**IDX — hand-typed, and deliberately no second parser.** The monthly Statement of Account carries an authoritative printed **`Equity NAB`**, and it self-checks three ways (`Market Value = Quantity × Close`; rows sum to TOTAL; TOTAL equals the box's `Portfolio`). But the SoA is **not in the intake path** — only the daily TC is dropped.

Twelve numbers a year is twelve keystrokes. A monthly-SoA parser is a real maintenance surface for a layout that will drift, and its three-way self-check only pays off if a parser exists to be checked. The map's prefer-derived-over-typed bias is aimed at per-Trade fields, and 12/year does not clear that bar. **The SoA PDF still goes in the keep-forever raw tier**, so a parser stays addable later and re-runnable over history.

`Equity NAB` is the IDX denominator: printed, self-checking, and the **smaller** of the two candidates — so it produces the *higher* risk % and **over-flags rather than under-flags**, the safe direction for a discipline check.

### 9.3 The record

Identity `(book, date)`. `equity` in **native currency with no currency field** — implied by book. Plus `provenance`, source, fetch date, and a raw-document pointer.

Then the **components, book-specific**:

| Book | Components stored beside the total |
| --- | --- |
| US | `cash`, `stock` |
| IDX | `portfolio`, `ledger_balance`, `cash_investor` |

Storing IDX's *components* beats storing two rival totals: both candidates stay derivable, and **it is what makes the deferred `Cash Investor` test a config change rather than a re-read of PDFs** ([§14](#14-handed-to-the-build-effort)). The per-book asymmetry is deliberate, and matches the cost-attribution shape in [§7.0](#70-cost-attribution).

**Provenance earns two tiers, and they do something:**

| Tier | Meaning | Effect |
| --- | --- | --- |
| `stated` | IBKR `total`; IDX typed off a printed figure | Normal |
| `estimated` | Typed from memory, no statement behind it | Still computes and still flags, but **excluded from risk-% aggregates, which report their excluded count** |

Without that exclusion the tier would be decoration and should have been dropped. **Carry-forward is not a third tier** — "most recent snapshot at or before entry" is a lookup rule, not a stored record.

### 9.4 Staleness bound

**IBKR 7 calendar days. IDX 45.**

A snapshot four months old against a book that doubled is a *wrong* number, not a slightly old one — and a wrong denominator silently poisons the above-1% test.

A single global bound is useless: it would have to clear IDX's structural ~31-day cadence, which makes it blind to an IBKR daily series that has silently died. Seven days on a daily series means the job has not run or Flex is failing — a fault that should surface, not quietly produce a number.

Past the bound, Risk % and Exposure % are **null with a marker** (the `insufficient_history` convention, *not* a span-check error), the marker reaches the banner, and the Trade is **excluded from risk-% aggregates with the count reported**.

**Exposure Percentage is identical, with no exceptions** — same lookup, same bound, same null-with-marker, same exclusion-with-count, same inherited provenance. **One denominator asked two questions.** Stated explicitly because any divergence would let a Trade show a live Exposure % against a denominator too stale for Risk % — two different claims about the same account on the same day, and precisely the class of thing that gets silently half-implemented.

**No same-day Risk % is possible on either book** — IBKR lags T-1/T-2, Stockbit up to a month. Recorded so nobody designs a same-day risk-% display that can never work.

### 9.5 The calendar axis — invariant

**`EquitySnapshot` lives on a calendar axis; the bar cache lives on a trading-day axis; they join by date and never by counting rows.** See [ADR 0006](docs/adr/0006-equity-is-dated-on-the-calendar.md).

The two series disagree about which dates exist **in both directions**, and **both disagreements are correct**: equity has rows where there is no bar (a NAV row on every US exchange holiday), and bars exist on hundreds of dates with no snapshot (IDX's monthly cadence). An account genuinely has a value on a day nothing traded; a suspended symbol genuinely has no bar.

**Equity is *not* filtered to the trading-day calendar, and [§4.4](#44-the-bar-pipeline)'s rule does not extend here.** That rule discards zero-volume rows because they corrupt *windows*; **an equity level is not a window over anything.** Filtering would delete true values and manufacture gaps the staleness rule then reads as neglect — an account would look unrecorded on precisely the days it was recorded. There is also no trading-day ordinal to store even if one were wanted: a trading day is a property of a *symbol*, equity is a property of a *Book*, and a book has no suspensions.

Two consequences follow rather than being chosen: the staleness bound is in **calendar** days, and **no count of equity rows may measure anything** — the freeze fuse, the post-exit window and the 60-day counterfactual bound all stay in trading days.

### 9.6 Backdating

**A month-end series, not a snapshot per Trade.** Moot on IBKR (backfill pulls the rolling series for free). On IDX, roughly 13 month-end levels covering the backfill window, typed once off Portfolio Performance — `stated`, since that is the broker's own app and not memory.

Per-Trade snapshots fail three ways: they would be dated at entry dates (a self-referential series), the same month gets retyped when several Trades fall in it, and the gaps between them would make the staleness bound fire arbitrarily. Month-end matches the live SoA cadence, so history and live have **the same shape** — one lookup rule, not two.

**A nulled Risk % at freeze is therefore not permanent the way a missing stop is.** A backdated snapshot can still fill it.

### 9.7 Which door

**A plain write, both books — not the confirm queue.** A snapshot is not a Trade: no cohort to reconcile, no FIFO, no exit to allocate, so there is nothing for a confirm step to *do*. IBKR snapshots are written by the daily job; IDX snapshots are typed into the review surface. **The missing-equity nag lives in the daily job** and surfaces in the banner as a stated fact (`IDX equity: last snapshot 31 Jul`), never as an alarm.

---

## 10. Rule adherence and counterfactuals

Owned by [#8](https://github.com/ajitimur/automatic-trading-journal/issues/8). Identified during charting as the highest-value part of the project.

### 10.1 Adherence is inverted

The obvious design has the trader declare a rule and the engine check obedience. **That is not what this does.** With no Plan ([ADR 0002](docs/adr/0002-no-plan-only-executed-trades.md)), a declared rule would be a third hand-entered field *and* intent reconstructed after the outcome was known.

Instead: **the engine scores every Trade against all six variants and reports which one it best fits.** "Which rule did this follow" is **derived, never asserted.**

**No verdicts, ever.** The engine stores signed deltas and never a boolean `adhered`. With no recorded intent there is no way to separate a considered override from a mistake — that information does not exist in the data. "Violation" and "override" are queries the human runs against thresholds, not labels the engine writes.

### 10.2 The ruleset

**One global, versioned ruleset with effective dates.** A version names which variant is **nominal**; the other five are computed unconditionally regardless.

> **`ruleset_v1`** = *partial 1/3 on days 3–5, then trail MA10*. Effective from the **July 2026** backdating floor, so all current history grades against v1.

A Trade is graded against whichever version was live on its **entry date**, and that version id is stored on the Trade. **A rule change mints v2 and never edits v1**; frozen Trades keep the version they were graded under. Adherence deltas measure against the nominal variant while fit ranges over all six — so *"you have drifted from the rule you wrote down toward the MA20 trail"* stays visible instead of being redefined away by a version bump.

### 10.3 The variant set

Two axes, **six counterfactuals**, plus `actual` as the reference row.

| Axis | Values |
| --- | --- |
| Trail | `ma10`, `ma20` |
| Partial | `none`, `day3`, `day5` |

**MA50 is dropped** — nothing in the actual strategy references an MA50 trail, and `ma_dist_50` already exists descriptively if the question ever arises.

**The partial fraction is fixed at 1/3**, a constant in the versioned ruleset. The trader's real band is 1/3–1/2, but if the counterfactual's fraction tracked whatever the Trade actually did, two counterfactuals would differ in *both* timing and size and neither comparison would isolate anything. Fixing it also lets all six run identically on Trades that took no partial at all.

### 10.4 Day counting

**The entry day is day 1.** So "day 3" is `entry + 2` trading days, and the band days 3–5 spans `entry + 2` through `entry + 4`.

**The partial rule is a band, not two discrete days** — any partial on trading days 3–5 inclusive satisfies it. Discrete days would score a day-4 partial as a miss, which nobody would call a miss.

### 10.5 Simulation

**Every variant carries the Trade's recorded stop as a hard leg.** Without it a variant rides straight through the price at which the trader would have been stopped out, and every counterfactual on a losing trade becomes fiction.

| Event | Price |
| --- | --- |
| Trail signal (close below MA_N) | **next trading day's open** |
| Scheduled partial (day 3 / day 5) | **that day's close** |
| Stop hit (`Low ≤ stop`) | the stop price |
| Stop gapped through (`Open < stop`) | the open |

The stop leg wins over a same-day trail signal.

**Why the trail waits for the next open.** The signal is *a close below MA10* — knowable only after the bell, so the earliest transactable price is the next open. Pricing at the signal close assumes a fill on information that did not yet exist, and the bias is one-directional: a close below MA10 is usually the front edge of continued weakness, so next opens tend to sit below signal closes. Every *"the rule beat you by 0.4R"* finding would otherwise be partly an artifact of the pricing convention.

**Why the scheduled partial does not.** Its trigger is a date on a calendar, known in advance, so no signal-timing constraint exists.

> The two conventions differ **for a principled reason, not an oversight** — the same shape as the entry/exit anchor asymmetry in [§6.3](#63-anchors). Recorded here so nobody "fixes" it later.

No slippage and no commission are modelled on either side; the actual side's real commissions are already stored separately. **The actual side is never repriced** — those are real fills. Both sides end up as prices a human could genuinely have obtained.

**Bound:** the trail signal or **60 trading days**, whichever comes first, recorded as `counterfactual_status: resolved | capped`.

**No-stop Trades** still run, trail-only, flagged `counterfactual_stopless: true`, and are excluded from cross-Trade counterfactual aggregates exactly as they are excluded from R. Their individual counterfactuals stay readable; they just cannot be averaged in.

**Limit-locked legs.** Mark a leg `limit_locked` when the bar shows `open == high == low == close` with `volume > 0`. On IDX essentially only a limit lock produces that signature, so this needs **no band table, no prior close, and no IPO or board exceptions**; direction falls out of the sign against the prior close. (IDX auto-rejection has been asymmetric since 8 April 2025 — ARB flat 15%, ARA tiered 35/25/20% by price band — but those tiers are context, not code.) The pricing rule above is **unchanged**; what changes is that the leg is marked, and the mark nulls `deviation_cost_r` ([§10.8](#108-when-a-number-cannot-be-trusted)).

### 10.6 R, and the three tiers

```
realized_r = (exit_avg_price − entry_avg_price) / (entry_avg_price − stop)
```

Off the stop **as recorded**, never re-derived. Three tiers rather than a single include/exclude:

| Stop | R aggregates | Adherence + chase scoring |
| --- | --- | --- |
| absent | **excluded** (still fully present in % and ADR terms) | excluded |
| `reconstructed` provenance | included | **excluded** |
| recorded | included | included |

**Any aggregate reporting R must report its excluded count beside it**, or the number quietly means something different each week.

### 10.7 What a Trade carries

**Deltas, against the nominal variant:**

| Field | Definition |
| --- | --- |
| `partial_state` | `in_band` / `early` / `late` / `none` / `not_applicable` |
| `partial_timing_delta` | signed trading days to the nearer band edge; null unless `early`/`late` |
| `trail_exit_delta` | signed trading days, actual final exit date − nominal variant's trail exit date (negative = exited early) |
| `deviation_cost` | nominal variant's outcome − actual outcome |
| `actual_partial_fraction` | derived from fills — **descriptive, no delta** |
| `exit_path` | derived from confirmed exit reasons: `stop_hit` / `trail` / `discretionary` / other |
| `ruleset_version` | the version live on the entry date |

> **Absence is not deviation.** A Trade whose final exit precedes the band gets `partial_state = not_applicable` and a null timing delta — **never a number**. A Trade stopped out on day 2 never reached the partial window; scoring it "1 day early" is nonsense, and **collapsing absence into violation is the single easiest way to make every adherence aggregate quietly wrong.**

`actual_partial_fraction` is stored without a delta because the real band is wide and size deviation is second-order next to timing.

**Per variant: raw exit legs, units derived on read.** Persist `date`, `price`, `fraction`, `trigger` ∈ {`partial`, `trail`, `stop`, `cap`}, `limit_locked`, plus `counterfactual_status`.

This pays a specific debt: no-stop Trades are excluded from R, so a `deviation_cost` stored *as* an R number would leave them with no cost at all. Stored as prices, their deviation cost still reads fine in % and ADR.

**Fit — scored by behaviour, in trading days, not by outcome.** Two quite different rules routinely produce near-identical P&L by coincidence, so an outcome-based fit would describe nothing about how the trade was managed. For each variant, sum the absolute trading-day distance between its simulated legs and the actual exits — partial to partial, final to final. **Store the full distance vector across all six, never just the winner:** "best fit" is derived at read time, which keeps the no-verdicts rule intact and lets the review surface say *"1 day off MA10, 4 off MA20"* instead of flattening it to a label. Ties break toward the nominal variant.

### 10.8 When a number cannot be trusted

**If the nominal variant has not fired by the 60-day cap, `trail_exit_delta` and `deviation_cost` are both null**, with `counterfactual_status: capped` carrying the reason. **Never substitute the cap date as a pseudo-exit** — that fabricates an exit the rule never signalled and systematically understates how much ride was left, and once a synthetic number is inside an aggregate it is not recoverable.

**`deviation_cost_r` nulls in two further cases:**

1. **A limit-locked leg.** A cost computed off a fill nobody could have obtained is a lie of exactly the class `capture_ratio` already caught, and it distorts the **tail**, where the learning is.
2. **Mismatched ex-date crossings** between the Trade's window and the nominal variant's. Otherwise the rule reads as outperforming when it merely dodged a dividend.

Nulling is stronger than a flag, because a flag can be read past.

### 10.9 Lifecycle

**Daily job, closed Trades only, never at confirm.** Confirm commits facts; counterfactuals are derived and would only slow the queue. Re-run daily until every variant is `resolved` or `capped`; snapshot at freeze with everything else. Open Trades get nothing — a counterfactual against an unfinished position has no actual to compare against.

**A variant resolving after freeze fills in without counting as `drift`** ([§3.6](#36-drift)).

Section F's `fwd_*` primitives are untouched: *"what the trail would have paid"* is simply the MA10-trail variant's result, not a new field.

---

## 11. The weekly review surface

Owned by [#13](https://github.com/ajitimur/automatic-trading-journal/issues/13). Prototype: [`docs/prototypes/weekly-review-surface.prototype.html`](docs/prototypes/weekly-review-surface.prototype.html) — **variant D is the settled design and the default**; A, B and C are kept as the alternatives that were actually on the table.

**It is a per-Trade exit workbench, not a dashboard** — a reading instrument rather than an analytical one. This surface required **no new enrichment field, no new hand-entered field, and no change to any formula, anchor or as-of date.** It is presentation over primitives that already exist, which is what they were stored as primitives for.

### 11.1 The unit of review is one Trade, on a timeline

The centre of the surface is a single Trade's **day-by-day strip**: entry, each Exit, the MFE and MAE dates, and where the **nominal variant** would have exited, all on the same axis. Underneath it, **each exit leg is graded on its own excursion** — per-Exit MFE to that leg's date, and what it left on the table in ADR.

This is what *"the day-3 partial was early"* actually looks like: not a number and not a distribution, but **the leg's own excursion window drawn against the Trade's**. This is what storing excursion *dates* in [§7.5](#75-e-excursion-and-f-post-exit) was for. Trade-level MFE sits beside it, explicitly answering a different question.

A nominal-variant exit marker renders as limit-locked where applicable. That is UI, not a field.

### 11.2 Aggregates: a thin strip of counts, split by book

One strip above the list: closed, chased, partial in band, trail on signal, stop recorded, net R. Two properties are load-bearing:

**Counts, never rates.** `2 of 2 chased`, not `100%`. A strict week is n=3, and a percentage over three Trades invents precision that is not there. Every count shows its denominator, and `n/a` renders distinctly from a miss.

**Split by book.** A single `2 of 3 stops recorded` over a mixed list *is* an aggregate across books. The list below may mix US and IDX freely — **a list is not an aggregate** — but no number on the page ever combines them.

### 11.2.1 Groupings do not appear at all

The genuinely surprising outcome, and it has a consequence that reaches beyond this surface. There is **no *by setup*, no *by stack_state*, no *by regime*, no *by exit reason*** table anywhere. They were prototyped as variant B, seen, and not chosen.

> **Three of the four learning goals therefore have no home in the interface.** Setup selection, sizing and regime are cross-trade questions, and they are answered **only** by asking an LLM against the export. This makes [§12](#12-the-llm-export) load-bearing rather than complementary.

**The two surfaces divide by unit of analysis, not by consumer** — this surface reads one Trade, the export reasons across many. They are not two presentations of the same aggregates, and they share none.

### 11.3 Scope, and actions

Sectioned, in one mixed-book list with the book as a per-row label:

- **Closed this week** — Trades whose final Exit falls in the review week.
- **Unreviewed from earlier** — any older closed Trade never marked reviewed. This is what stops a skipped week silently losing its Trades, and what gives *Reviewed →* something to drain.
- **Open** — included, using only the four rolling fields from [§7.6](#76-g-live-while-the-trade-is-open). Open Trades are **never part of the week's counts.**

The cadence stays literally weekly. A rolling window was rejected: **low n is handled by refusing to print rates, not by widening the window.**

**Actions write straight through**, and this does not breach the one-door rule in [§5.1](#51-one-door): *add stop*, *add setup*, *add IDX equity snapshot*, *override exit reason*, *edit note*, *acknowledge drift*, *mark reviewed*.

The boundary holds as settled — **the queue is the only thing that commits Trades and ingests broker facts.** It never governed the two hand-entered fields or a post-hoc revision:

- **Stop and setup were never queue-committed** ([§5.5](#55-stop-and-setup-are-chaseable)). Provenance still falls out of when the stop arrived. Freeze still locks it, so *add stop* is unavailable on a frozen Trade and the hole stays permanent.
- **Exit-reason override is the path bulk confirm structurally requires** ([§5.8](#58-bulk-confirm-covers-exit-reasons-and-nothing-else)). Accepting unread guarantees some wrong ones land; a later correction path is not a second door, it is the thing that decision depends on. The queue confirms at import; the review surface **revises** on inspection, once the timeline shows the proposal was wrong.
- **Drift acknowledgement** is unchanged — bar-data drift may only ever be acknowledged.

### 11.4 The attention banner

A banner at the top of the review: no stop before freeze (**with the remaining fuse stated per item**), enrichment held for repair, parked items, unacknowledged drift, last successful run, `IDX intake: last drop 11 Aug (7 trading days ago)`, `IDX equity: last snapshot 31 Jul`.

`insufficient_history` appears as an explicit **no-action** item, visually distinct from a span-check repair. Conflating them is the trap ([§7.8](#78-insufficient-history)), and the surface keeps them apart.

Intake and equity facts are phrased as **stated facts, never alarms**: a genuine no-trade stretch is normal for a swing trader, and a crying-wolf warning is ignored within a month.

> **Recorded as a live risk, not resolved.** The alternative — a separate always-on attention surface — was rejected in favour of one cadence. A 20-trading-day stop fuse gets roughly **three looks** at a weekly cadence. If a stop hole ever freezes shut in practice, this is the decision to revisit, and the fix is promoting attention to always-on rather than redesigning the review.

---

## 12. The LLM export

Owned by [#11](https://github.com/ajitimur/automatic-trading-journal/issues/11), extended by [#14](https://github.com/ajitimur/automatic-trading-journal/issues/14) and [#17](https://github.com/ajitimur/automatic-trading-journal/issues/17). Sample and renderer: [`docs/prototypes/ticket-11-llm-export/`](docs/prototypes/ticket-11-llm-export/) — `python3 render.py` regenerates the whole four-way comparison.

**Curated JSONL, one object per Trade per line, normalized to R and ADR, with a legend header that always ships.**

Per [§11.2.1](#1121-groupings-do-not-appear-at-all), this is the **only** place setup selection, sizing and regime can be answered at all.

### 12.1 Why this shape

**The obvious assumption — that positional CSV is the cheap shape and repeated keys are the extravagance — is wrong for a record with a variable number of children.** Flattening three Exits and six adherence variants forces a **117-column** header; a single-exit Trade leaves 21 cells empty, a sparse one 41. CSV **cannot carry a variable exit count at all** — the prototype's renderer truncates at three, silently. It is not a cheap shape here, it is a lossy one.

**The real cost driver is field count, not key repetition:** the full export is 2.4× the curated one, and the six-variant adherence block is most of the gap.

**Normalization is what makes two books one export.** An IDX row (IDR 7,240 entry) and a US row ($174.30) sit adjacent and compare directly because distances are in ADR and levels in R. In raw units they do not, and the model will sometimes get the currency arithmetic wrong.

### 12.2 What ships

- **JSONL**, one object per Trade per line. Self-describing keys, variable-length `exits` array, rows independently filterable.
- **Exactly two price levels — `entry_avg_price` and `stop`.** Enough to answer a price question and to let the model check its own R; not enough to tempt it into cross-book currency maths. **The equity level never ships** — it would be a third and the largest. Risk % and Exposure % **do** ship, because sizing has no home in the interface.
- **The six-variant table does not ship.** `best_fit_variant`, `best_variant_r`, `partial_state`, `trail_exit_delta_days` and `deviation_cost_r` carry everything the analysis needs. Shipping all six is the largest per-Trade line item and it buys narration, not insight.
- **`capture_ratio`** = `realized_r / mfe_r`, **null unless the Trade both went in favour and finished in profit.** This exception was found by reading prototype output, not by reasoning: a Trade with 0.30R available that lost 1.09R computes to **−3.63**, which reads as a catastrophic *exit*. The exit was correct and immediate — the entry was the mistake. **A ratio that indicts the wrong decision is worse than no ratio.**
- **`deviation_cost_r`** — normalized out of price into R. Inherits the absent-stop exclusion and both null conditions from [§10.8](#108-when-a-number-cannot-be-trusted).
- **`dividend_drag_r`** — **omitted entirely when null**, and **no `_pctile`**. Omitting costs nothing on the ~88% of rows without one, and its absence is unambiguous once the legend says so.
- **`seq` and `book_drawdown_r_at_entry`** — see [§12.3](#123-two-opposite-kinds-of-number).
- **Five within-export percentiles** — `stop_distance_adr`, `entry_ma_dist_10_adr`, `entry_move_63d_pct`, `exposure_pct_of_equity`, `days_held` — each rendered immediately after the field it ranks. `1.18 ADR over MA10` means nothing absolute, and a model derives distributions across 200 rows badly and inconsistently.
- **The free-text note stays on the row.** Segregating it breaks the one-object-per-Trade property that makes the export filterable, and per token it is the highest-signal field in the file.
- **Aggregates ship, with `n` on every figure**, plus the per-book count excluded from the drawdown curve. Withhold the counts and a model will confidently report that `high_tight_flag` underperforms on a sample of three.

The grouping keys the interface dropped all survive here — `setup`, `entry_stack`, `entry_regime`, `chased`, `book`, and `reason` per exit leg.

### 12.3 Two opposite kinds of number

The export carries both, and a model meeting them without warning will read them alike:

| Kind | Fields | Behaviour under slicing |
| --- | --- | --- |
| **Export-relative** | the five `_pctile` fields | Rank *within this export*. Slice differently and the same Trade ranks differently. |
| **Absolute** | everything else, including `book_drawdown_r_at_entry` | Computed against the book's **complete** history. Does not move when the export is sliced. |

`seq` is a third case: absolute, but with **gaps** in a sliced export. **Gaps stay unfilled and uncompacted** — a model that can see rows are missing will hedge, which is correct, where renumbering 1..n would hide the slice and license "nothing was traded between these."

### 12.4 Scope

**One book per export by default**, reinforced rather than merely defaulted: a two-book export would put two incomparable drawdown curves in one column. `book` stays on every row, so a deliberate normalized cross-book export remains legal — but mixed-by-default invites exactly the aggregation error the legend warns against. **Date range is a parameter, not a shape decision.**

### 12.5 The legend

**~760 tokens paid once — well under 1% of a 200-Trade export — and it always ships.** It is the only place the caveats live, and without it a model will cheerfully average R across a reconstructed stop. It must state:

- Exclude `reconstructed` stops from R and chase conclusions ([§10.6](#106-r-and-the-three-tiers)).
- `not_applicable` is **not** a deviation.
- `null` means **could-not-exist**, not missing.
- **The two books never aggregate.**
- **There is no Plan**, so intent cannot be inferred.
- Percentiles are **export-relative**; `seq` and `book_drawdown_r_at_entry` are absolute.
- `seq` gaps mean rows are missing; rows adjacent in the file are not adjacent in time.
- Sequence questions are answerable by ordering on `seq` within `book`; there are **no precomputed prior-trade fields, by design**, and any the reader derives should be stated as derived.
- `book_drawdown_r_at_entry`: `insufficient_history` is **not** a drawdown of zero; the curve understates stretches containing no-stop Trades, with *n* excluded given in the header.
- `dividend_drag_r` and the two `deviation_cost_r` null conditions.
- **Setup-selection conclusions are conditional**: the journal records only Trades taken, never setups passed on.
- Treat **n < 20 as anecdote**.

### 12.6 Cost, stated honestly

The curated shape measured ~64K tokens a year as first prototyped and ~77K as locked — about **+20%**, which puts it slightly **above** the wide CSV. **It is no longer the cheapest shape; it is the most useful per token**, which is the thing being optimized. The percentiles are ~8,000 tokens a year and are the first thing to cut if the budget ever binds.

---

## 13. Runtime and operations

Owned by [#18](https://github.com/ajitimur/automatic-trading-journal/issues/18).

**v1 runs on this machine under `launchd`. No new hardware, no cloud, no mailbox credential anywhere.**

This was framed as a three-way cost/uptime comparison and was not one. Two decisions collapsed the space before the hosting question was put.

### 13.1 Backfill is first-class, so uptime stopped mattering

**Every run is "for each book, advance from `last_processed_trading_date` to the present"**, not "process today". Daily bars are historical, `RegimeSnapshot` is keyed `(book, date)` precisely so it computes for any past date, and backdated enrichment was already required.

So **a missed day is not an error**, and the hosting requirement fell from *must run every day* to *must run eventually* — the single move that put a sleeping, travelling laptop back in contention. Bounded only by the July 2026 floor: **no cap**, because an artificial one would reintroduce the permanent hole this abolishes. A run that catches up several days **records that it did**, so a long gap reads as a fact rather than as silence.

**`launchd` over `cron`** for a concrete reason: a `StartCalendarInterval` job fires **on wake** if its window passed while the machine slept, so the common case self-heals before backfill is even reached.

### 13.2 Intake

**All mailbox access is gone.** Credentialed access to a personal mailbox was declined, including the dedicated-forwarding-address compromise. The Stockbit TC is **hand-dropped**; parsing, enrichment and everything downstream stay automatic. The queue already accepted drops identically to broker imports, so nothing downstream changes shape.

That removed cloud's third leg; backfill had already removed its second (always-on). Its first — sidestepping the DNS interception — was left standing alone, which is a narrow reason to rent a box. Meanwhile cloud *acquired* costs from the same answers: with the review surface local-only and the TC hand-dropped, a cloud host would need a **public authenticated surface** for both the workbench and the file drop. **The always-on box ranks last** — it spends hardware money to solve uptime that backfill dissolved, and inherits the DNS dependency anyway.

**The one real cost of the manual drop:** with mail polling, a missed IDX trade was structurally impossible; now a forgotten drop is invisible, because nothing in the system knows the trade happened. Paid for by **a stated fact in the banner, not an alarm** ([§11.4](#114-the-attention-banner)). The monthly SoA serves as a hand-dropped reconciliation pass — lossy for fills, but sufficient to answer *did I miss a day*.

### 13.3 Schedule and DNS

**One job, one daily run at ~06:00 WIB, two book-scoped passes.** That lands after the US close (03:00–04:00 WIB the same morning) and after the prior IDX close, so a single run has fresh prior-day closes for both books — which is what the regime stamps need. Nothing consumes intraday freshness, so two schedules would double the operational surface to buy hours no field uses. **Each pass still gates on whether that book's prior trading day has actually closed**, rather than trusting the clock.

**DNS is resolved over DoH inside the Flex client, per run, never cached.**

Not a VPN and not the system resolver: those are human-maintained preconditions whose failure is **silent and off-app** — and the failure mode here is an *empty response*, exactly what the confirm queue exists to surface rather than swallow. In-process makes the dependency explicit, version-controlled and testable, and it survives the machine moving to another network.

- **Per host, response-driven** — `SendRequest` and `GetStatement` sit on different hosts, and the second is named in the first's response.
- **Fresh resolution every run**, because the Akamai edge rotated between two lookups minutes apart.
- **Detect interception by mismatch against the DoH answer, never by matching a known bad address** — the ISP's block address has already moved once.
- **The empty-body case keeps its own error branch**, distinct from the 1012/1015/1013 family: one says *fix the network*, the other says *go to the portal*.

### 13.4 Secrets

The job holds **exactly one**: the IBKR Flex token, scoped and rotatable, expiring **2027-07-14**. Resolved through a single indirection rather than a hardcoded path. There is no mailbox credential to place, so the security posture no longer votes in the hosting decision at all.

### 13.5 Durability

Three tiers:

| Tier | Policy | Why |
| --- | --- | --- |
| **Journal DB** | `VACUUM INTO` snapshot at the end of every successful run, timestamped, rolling retention, **at least one copy off this machine** | Irreplaceable — the only copy of hand-entered stops, setups, confirmed exit reasons and frozen snapshots. No broker can reissue any of it. |
| **Raw source documents** | **Kept forever** — Flex XML as fetched (trades *and* NAV), TC PDFs and SoA PDFs as dropped | **The tier worth defending.** It makes the DB reconstructible from scratch and lets a parser fix be **re-run over history** instead of hand-repaired — which matters directly, since real files caught two parser-level premises that were wrong. It is also what makes the rolling-365 NAV window survivable ([§9.2](#92-two-creation-mechanisms)). |
| **Bar cache** | Backed up as a convenience | Rebuildable with caveats — though yfinance will not reliably re-serve a delisted name later. |

**PII caveat**: TC and SoA PDFs hold name, address, NPWP/NIK, phone and account number. The raw archive stays **local or encrypted** and never enters a repo or an unencrypted sync folder.

> **The restore is rehearsed once before this spec is called done.** A backup that has not been restored is a belief, not a backup. See [§14](#14-handed-to-the-build-effort).

### 13.6 Observability, access and shape

**A run record in the DB plus the banner — no push channel.** Every run writes per-book status, dates advanced and errors. Email and push were rejected: each is another credential and another silently-failing dependency in a single-user local app. **Honest cost: you only learn on next open** — which inherits the already-accepted three-looks-per-fuse risk rather than creating a new one. `launchd` keeps a plain log file for the case where the app itself will not start.

**Nothing is resident.** Two independent entry points over one SQLite file: a headless job `launchd` invokes on schedule, and a UI launched on demand serving `localhost`, which exits when you are done. The constraint that the store outlives any UI session needs **a file, not a daemon**. The confirm queue and the drop inbox live in the DB, so a TC dropped while the UI is closed simply waits for the next run or the next session.

**Access is local-only, `localhost`.** A weekly review is a sit-down activity; a public surface would add auth, certificates and an attack surface to a single-user journal. A tunnel bolts on later without redesigning anything.

### 13.7 Hosting does not constrain the data store

**Stated explicitly so it stops shadowing the model.** A single-file SQLite database plus a file-based bar cache runs identically on a laptop, a Pi or a cloud box; the only requirement is a durable volume and a rehearsed restore. **ADR 0001–0006 bend to none of the candidates.**

**v2 portability is three seams, and no more:**

1. The daily job is a plain **idempotent CLI command** that `launchd` merely calls, so any scheduler on any host substitutes without touching the job.
2. Secrets resolve through **one indirection**, so keychain-today/env-var-tomorrow is a config change.
3. **Nothing macOS-specific in the job path**; the UI is already HTTP on localhost, which generalises for free.

Explicitly **not** done: abstracting the store behind an interface, or containerising now. Same discipline as the bar adapter — **keep the cheap seam, refuse the speculative second implementation.**

---

## 14. Handed to the build effort

Everything the spec deliberately did not settle. Each is scoped, and each is designed so that either answer is survivable.

| # | Item | Why it is safe to defer |
| --- | --- | --- |
| 1 | **Rehearse the restore.** Back up per [§13.5](#135-durability), wipe, restore, verify. A backup that has not been restored is a belief. | A requirement, not an aspiration — but it validates a decision already made rather than blocking one. |
| 2 | **Verify Yahoo's `.JK` dividend coverage** against a known IDX distribution. | `dividend_drag_r` is null-with-a-marker, so absent coverage reads as *unknown*, never as *no dividend*. If coverage proves bad the field degrades to permanently null rather than to a wrong number. |
| 3 | **Compare `Equity NAB` against Stockbit's Portfolio Performance** for one month-end date. One screenshot. | `Equity NAB` is the decision and it over-flags rather than under-flags. Because the components are stored ([§9.3](#93-the-record)), a flip is a **config change, not a re-read of PDFs**. Worth doing once: it moves every IDX Risk % by ~20%, straight through the 1% flag. |
| 4 | **IBKR's `cash` margin hazard** is unconfirmed rather than refuted — the verification account simply never used margin. | `total` is the denominator regardless. Storing `cash` and `stock` is what makes the hazard observable if it ever bites. |
| 5 | **The inventory of parser correction rules** — board type, account→book mapping, fee interpretation, date formats, and the scoping/retirement of a rule. | The *mechanism* is fully specified ([§5.4](#54-corrections-a-fact-once-a-rule-forever)). Only writing the parsers can fill the class list, and a guessed inventory inside a locked spec would read as decided. |

---

## Appendix — decision index

Every ticket, with what it owns here. Full reasoning, rejected alternatives and the corrections each made to its own premises live in the ticket's resolution comment.

| Ticket | Owns |
| --- | --- |
| [#2 Obtain sample trade exports](https://github.com/ajitimur/automatic-trading-journal/issues/2) | §4.1, §4.2 — and **corrected the research** on IBKR commissions and the Stockbit TC-vs-SoA choice |
| [#3 Where daily OHLCV comes from](https://github.com/ajitimur/automatic-trading-journal/issues/3) | §4.3 |
| [#4 How trades get out of IBKR](https://github.com/ajitimur/automatic-trading-journal/issues/4) | §4.1 |
| [#5 How trades get out of Stockbit](https://github.com/ajitimur/automatic-trading-journal/issues/5) | §4.2 — retrieval amended by #18 |
| [#6 The shape of a trade record](https://github.com/ajitimur/automatic-trading-journal/issues/6) | §3 — ADR 0001–0003 |
| [#7 The enrichment field list](https://github.com/ajitimur/automatic-trading-journal/issues/7) | §6, §7 |
| [#8 Rule adherence and counterfactual exits](https://github.com/ajitimur/automatic-trading-journal/issues/8) | §10 |
| [#9 How market regime is defined](https://github.com/ajitimur/automatic-trading-journal/issues/9) | §8 |
| [#10 The confirm-and-enrich capture flow](https://github.com/ajitimur/automatic-trading-journal/issues/10) | §5, §3.6 |
| [#11 The LLM export format](https://github.com/ajitimur/automatic-trading-journal/issues/11) | §12 |
| [#12 Which OHLCV source to commit to](https://github.com/ajitimur/automatic-trading-journal/issues/12) | §4.3, §4.4, §6.5 |
| [#13 The weekly review surface](https://github.com/ajitimur/automatic-trading-journal/issues/13) | §11 |
| [#14 Sequence and state-at-entry enrichment](https://github.com/ajitimur/automatic-trading-journal/issues/14) | §7.9, §12.3 — ADR 0004 |
| [#15 What account equity each broker can report](https://github.com/ajitimur/automatic-trading-journal/issues/15) | §9.2 (research) |
| [#16 How an Equity Snapshot gets created](https://github.com/ajitimur/automatic-trading-journal/issues/16) | §9 — ADR 0006 |
| [#17 Corporate actions and market mechanics](https://github.com/ajitimur/automatic-trading-journal/issues/17) | §4.4, §7.7, §10.5, §10.8 — ADR 0005 |
| [#18 Hosting and the daily job](https://github.com/ajitimur/automatic-trading-journal/issues/18) | §13 |
| [#19 Verify what the equity sources return](https://github.com/ajitimur/automatic-trading-journal/issues/19) | §9.2, §9.5, §13.3 — **corrected #15 in four ways** |
