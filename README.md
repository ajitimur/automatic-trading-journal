# automatic-trading-journal

A personal journal for momentum swing trades across two markets — US equities via IBKR and Indonesian equities via Stockbit. It records what was executed, enriches it from daily bars, grades it against a mechanical strategy, and exports it for analysis by an LLM.

**Being built.** The repository holds the locked specification and the research, samples and prototypes it was built from — and now the **walking skeleton**: the thinnest complete path through the system, with no domain logic yet.

## Running the walking skeleton

Two independent entry points over one SQLite file ([SPEC §13.6](SPEC.md#136-observability-access-and-shape)), nothing resident:

```sh
# The daily job — a plain idempotent CLI. Creates the file, writes a run
# record, advances each book from its cursor to the present (backfill is
# first-class, so a missed day is caught up, not an error — SPEC §13.1). Per
# book it runs the enrichment passes — regime, counterfactual, freeze — each
# gating on whether that book's prior trading day has actually closed (§13.3),
# and carries the nags (missing stop/setup, IDX equity, IDX intake — §11.4) as
# stated facts. It enriches but never commits a Trade. Running it twice is a
# visible no-op.
npm run job -- run                 # or: job/bin/journal run

# Import an IBKR Flex XML file into the append-only Fill ledger. One Fill per
# execution row; re-dropping the same file is idempotent (SPEC §4.1).
npm run job -- import docs/samples/ibkr-flex-schema-fixture.xml

# Drop a hand-dropped Stockbit Trade Confirmation (PDF or its pdftotext -layout
# text) into the ledger. One Fill per execution row, shares canonical from the
# Quantity column. The fee identity is a document-level gate: a shifted column
# quarantines the whole document with zero fills committed (SPEC §4.2, §5.6).
npm run job -- drop docs/samples/stockbit-tc-fixture.txt

# Fetch the same query unattended over the wire — DNS resolved over DoH per
# host, interception caught by mismatch, error bodies surfaced not swallowed
# (SPEC §13.3). Needs JOURNAL_SECRET_IBKR_FLEX_TOKEN and network.
npm run job -- fetch <activity-flex-query-id>

# Derive Trades from Fills through the one confirm door (SPEC §5). Every failure
# is one of eight proposal kinds, never an exception (new-trade · add-fills ·
# exit-allocation · restatement · quarantine · orphan-exit · enrichment-repair ·
# drift). Blocked items (orphan exits, unfillable sells) park and confirm skips
# them, so one bad item never stalls the batch. Re-confirming re-derives, so a
# parked orphan clears itself once its missing Trade is entered by hand (RECHECK).
npm run job -- confirm --dry-run
npm run job -- confirm

# Bulk-confirm exit reasons only (SPEC §5.8): a week of statements is mostly
# agreeing with the proposed reasons. New Trades stay one-at-a-time; parked
# items are left alone.
npm run job -- bulk-confirm

# A fact once, a rule forever (SPEC §5.4): a wrong symbol is a parser *rule* —
# remembered, applied to every future statement before it reaches the queue, and
# it repairs Trades already committed under the wrong symbol. (A wrong quantity
# is a *fact* about one fill, corrected in place and never remembered.)
npm run job -- remember-symbol stockbit MEDC MEDCX

# Chase the two hand-entered fields (SPEC §3.2/§5.5) — the only typed values in
# the system. Confirm demands neither; a Trade commits without them. The stop's
# provenance (recorded | reconstructed) is derived from whether it arrived
# before the Trade's first Exit, never typed. Both lock at freeze.
npm run job -- stop  <trade-id> <price>
npm run job -- setup <trade-id> base_breakout   # | high_tight_flag | other

# Capture IBKR NAV as EquitySnapshots — the risk/exposure denominator, not the
# equity curve (SPEC §9). A *second* Flex query (NAV Summary in Base): every
# reportDate row becomes a snapshot with `total` the denominator, and the raw
# XML joins the keep-forever tier so the rolling-365 window can't take history.
npm run job -- import-nav docs/samples/ibkr-nav-flex-schema-fixture.xml
npm run job -- fetch-nav <nav-flex-query-id>

# Hand-enter IDX snapshots — no SoA parser (SPEC §9.2). Components stored beside
# the total so switching the denominator is a config change (SPEC §9.3). One
# entry, or a month-end backfill series from CSV in one sitting (SPEC §9.6).
npm run job -- equity-idx --date 2026-07-31 --portfolio 800 --ledger-balance 200
npm run job -- equity-idx --file month-end-series.csv

# Risk % and Exposure % — one denominator, two questions (SPEC §9.4). Both read
# the most recent snapshot at or before entry, under one calendar-day staleness
# bound (IBKR 7, IDX 45): past it both are null with a marker that reaches the
# banner and the Trade leaves the risk-% aggregate with its count reported. An
# `estimated` snapshot still computes and flags but is excluded too.
npm run job -- risk               # every book; --book US|IDX to limit

# The counterfactual and adherence engine (SPEC §10). Adherence is inverted: the
# engine scores every *closed* Trade against all six variants (trail {ma10,ma20}
# × partial {none,day3,day5}) with the recorded stop as a hard leg, and stores
# signed deltas against the nominal variant — never a verdict. Best fit derives
# at read time from the stored six-way trading-day distance vector; a Trade
# stopped out before the band is not_applicable, the 60-day cap nulls its deltas
# without fabricating an exit, and no-stop Trades run trail-only and are flagged.
# A leg is marked limit_locked on the OHLC-equal, volume-positive bar (§10.5), and
# that mark — like a mismatched ex-date crossing — nulls deviation_cost_r (§10.8),
# a null being stronger than a flag. dividend_drag_r (§7.7) sits *beside* Realized
# R, computed from dividends shipped with the bars; it is Trade-level only, and
# null — not zero — where the window crossed no ex-date, so absent coverage reads
# as unknown.
npm run job -- counterfactual     # every book; --book US|IDX to limit

# Durability (SPEC §13.5). Every successful `run` leaves a timestamped
# `VACUUM INTO` snapshot under rolling retention, plus an off-machine copy when
# $JOURNAL_OFFSITE_DIR is set (at least one copy off this machine). Raw source
# documents (Flex XML, TC/SoA PDFs) are archived verbatim, kept forever, and —
# being PII-bearing — never enter the repo. Rehearse the restore end to end:
JOURNAL_OFFSITE_DIR=/Volumes/encrypted/atj npm run job -- run   # snapshot + off-site
npm run job -- restore-check          # restore newest snapshot to scratch, verify it opens
# See docs/durability-restore-rehearsal.md for the written-down rehearsal record.

# The localhost UI — `/` is the weekly review surface (SPEC §11): a per-Trade
# exit workbench with the attention banner, a by-book strip of counts (never
# rates), the strict week plus unreviewed stragglers plus open Trades, and
# actions that write straight through the CLI door (add stop/setup, add IDX
# equity, override exit reason, edit note, mark reviewed). `/raw` keeps the
# diagnostic skeleton (Trades/Fills/latest run). Ctrl-C to exit.
JOURNAL_DB=job/journal.db npm run ui

npm run typecheck && npm test      # TypeScript UI + Python job
```

See [`docs/adr/0007-walking-skeleton-stack.md`](docs/adr/0007-walking-skeleton-stack.md) for the stack decision and [`deploy/`](deploy/) for the `launchd` job.

## Start here

| | |
| --- | --- |
| **[`SPEC.md`](SPEC.md)** | The specification. Locked — read this first. |
| [`CONTEXT.md`](CONTEXT.md) | The glossary: the project's ubiquitous language. |
| [`docs/adr/`](docs/adr/) | Six hard-to-reverse decisions, with reasoning and rejected alternatives. |

## Supporting material

| | |
| --- | --- |
| [`docs/research/`](docs/research/) | What each data source offers, from primary sources. |
| [`docs/samples/`](docs/samples/) | What the sources actually return, verified against real files. Several findings here corrected the research. |
| [`docs/prototypes/`](docs/prototypes/) | The capture flow and review surface as clickable HTML; the LLM export as a runnable renderer. |
| [`scripts/`](scripts/) | Wizards for the manual broker steps the agent cannot perform. |

## How the spec was produced

Charted and worked as a wayfinding map on the issue tracker: [Map: Automatic trading journal spec](https://github.com/ajitimur/automatic-trading-journal/issues/1), across 18 decision tickets. Each ticket's resolution comment holds the full reasoning, the alternatives rejected, and — for several — the corrections it made to its own premises. `SPEC.md`'s appendix indexes them.
