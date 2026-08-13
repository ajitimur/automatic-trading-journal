# The walking skeleton: a Python job and a TypeScript UI over one SQLite file

The build stands up the thinnest complete path through the whole system before any domain logic: a **Python job**, a **TypeScript localhost UI**, and **one SQLite file** they both read. The spec deliberately left the stack open (SPEC §14); this ADR records the choice the build made, and the three v2 seams it establishes while they are free.

Two independent entry points over one file (SPEC §13.6): the job is a plain `journal run` command `launchd` invokes on schedule, and the UI is launched on demand and exits when you are done. **Nothing is resident** — the store outlives any UI session because it is a file, not a daemon.

## Why

**Python is forced, not chosen, by the job.** yfinance is the permanent bar source for both books (SPEC §4.3), it is Python-only, and its cookie/crumb session handling is load-bearing — a hand-rolled HTTP client against Yahoo returns 429 on the first request. The bar-cache ticket (#24) lands in this job, so the job is Python from the start.

**The UI is a separate TypeScript app reading the same file.** The review surface is a localhost page (SPEC §13.6); HTTP on localhost generalises for free and needs no shared runtime with the job. Coupling the two through the SQLite file — not through a process or an API — is what keeps each entry point independent and non-resident.

**One SQLite file, no interface over it.** A single-file database plus a file-based bar cache runs identically on a laptop, a Pi or a cloud box (SPEC §13.7); the only requirement is a durable volume and a rehearsed restore. The store is deliberately *not* abstracted behind an interface, and nothing is containerised — that discipline is explicit. Both entry points speak raw SQLite; the UI reflects whatever the job last wrote.

## The three v2 seams, and no more

Established now, while they are free (SPEC §13.7):

1. **The job is a plain idempotent CLI command.** `launchd` merely calls `journal run`; any scheduler on any host substitutes without touching the job. Idempotency is real, not nominal: every run advances each book from its cursor to the present, so a second run the same day is a visible no-op and a missed day is not an error (SPEC §13.1).
2. **Secrets resolve through one indirection.** The job holds exactly one secret, the IBKR Flex token (SPEC §13.4). Every caller goes through one resolver; keychain-today/env-var-tomorrow is a config change, not a code change.
3. **Nothing macOS-specific sits in the job path.** No keychain call, no launchd-only assumption; the store path itself resolves through configuration rather than a hardcoded location.

## Alternatives considered

**One language for both entry points.** Rejected in both directions. A pure-Python UI would still not remove Python from the job (yfinance forces it) and would trade a familiar HTTP-on-localhost surface for less. A pure-TypeScript job cannot use yfinance's load-bearing session handling. Two languages, coupled only through the file, is the smaller commitment.

**Abstract the store behind an interface now.** Explicitly refused (SPEC §13.7), the same discipline as the bar adapter: keep the cheap seam, refuse the speculative second implementation. The store seam is already cheap — it is a file path — so no interface buys anything until a second store actually exists.

**Containerise now.** Refused for the same reason. SQLite plus a file cache already runs identically across hosts; a container adds operational surface to a single-user local journal without moving the portability needle.

## Consequences

**The schema lives in the job and the UI reads it raw.** The job applies the schema idempotently on every open (`CREATE TABLE IF NOT EXISTS`), so `journal run` creates the file if absent and later tickets add to it. The empty `trade` table is what lets the UI render "no Trades yet" today; the append-only `fill` ledger (ADR 0003) is where the IBKR parser (#22) and the bar cache (#24) land.

**Errors are recorded, not raised.** A book that fails is written to its run record with `status='error'` and the job still exits zero (SPEC §13.6). A `launchd` log file covers the one case the run record cannot: the app itself failing to start, so nothing reached the DB.

**Access is local-only.** The UI binds `127.0.0.1` only — a weekly review is a sit-down activity, and a public surface would add auth, certificates and an attack surface to a single-user journal. A tunnel bolts on later without redesigning anything.
