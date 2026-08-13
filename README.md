# automatic-trading-journal

A personal journal for momentum swing trades across two markets — US equities via IBKR and Indonesian equities via Stockbit. It records what was executed, enriches it from daily bars, grades it against a mechanical strategy, and exports it for analysis by an LLM.

**Being built.** The repository holds the locked specification and the research, samples and prototypes it was built from — and now the **walking skeleton**: the thinnest complete path through the system, with no domain logic yet.

## Running the walking skeleton

Two independent entry points over one SQLite file ([SPEC §13.6](SPEC.md#136-observability-access-and-shape)), nothing resident:

```sh
# The daily job — a plain idempotent CLI. Creates the file, writes a run
# record, advances each book. Running it twice is a visible no-op.
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

# Derive Trades from Fills through the one confirm door (SPEC §5.1). Buys group
# into entry-day cohorts (ADR 0001); sells allocate FIFO across open Trades.
# --dry-run shows the proposals and commits nothing; re-confirming is a no-op.
npm run job -- confirm --dry-run
npm run job -- confirm

# The localhost UI — reads the same file, renders "no Trades yet" and the
# latest run record. Ctrl-C to exit.
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
