# automatic-trading-journal

A personal journal for momentum swing trades across two markets — US equities via IBKR and Indonesian equities via Stockbit. It records what was executed, enriches it from daily bars, grades it against a mechanical strategy, and exports it for analysis by an LLM.

**Not built yet.** This repository currently holds the locked specification and the research, samples and prototypes it was built from.

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
