# IBKR Flex — sample findings

Empirical answers to the open items in `docs/research/ibkr-trade-export.md`, from one real
Activity Flex Query run on 2026-08-12: `Last365CalendarDays`, `Level of Detail = Executions`,
audit-trail fields on, XML. **422 fills, 292 orders, all `STK`.**

The raw statement is **not** in this repo — it is a full year of real trading and this
repository is public. `ibkr-flex-schema-fixture.xml` alongside this file is an anonymised
5-trade fixture with the same schema: symbols renamed, prices rounded, dates and P&L
neutralised. Quantities, commissions and exec ids are preserved because they carry the
findings below.

---

## The correction: commission is on EVERY fill, not the first

`docs/research/ibkr-trade-export.md` §5.1 quotes IBKR's own documentation — *"The commission
is displayed on the first partial execution only"* — and concludes that commission must be
modelled at the order level and that **"a per-fill commission is not meaningful."**

**The real data says otherwise.** Of 92 multi-fill orders:

| | orders |
|---|---|
| commission present on **every** fill | **89** |
| commission on the first fill only | 3 |

And it is allocated **pro rata by quantity**, exactly. From the fixture (order `5237074970`):

| fill | quantity | ibCommission | per share |
|---|---|---|---|
| 1 | -100 | -0.72907365 | 0.0072907365 |
| 2 | -1 | -0.007290736 | 0.0072907360 |
| 3 | -1 | -0.007290737 | 0.0072907370 |
| 4 | -147 | -1.071738265 | 0.0072907365 |

Identical to nine decimal places. The three exceptions are explained by IBKR's **$0.35
per-order minimum**: when the minimum binds, it is not divisible pro rata, so it lands
unevenly across the fills. The fixture's `SYM2` shows the minimum on a single fill — 25 shares
charged 0.35525725, far above the per-share rate.

### Why this matters

The research's guidance was to take the order's single commission and ignore per-fill values.
Applied to this data that would **understate costs on 89 of 92 multi-fill orders**, because it
would keep only the first fill's share and discard the rest. On the fixture order it would
record 0.73 instead of 1.82 — a 60% undercount.

**Correct rule: sum `ibCommission` across the fills of an `ibOrderID`.** Do not take the
first. A per-fill commission *is* meaningful here, and per-fill net price is computable
directly.

This also **weakens the symmetry with Stockbit** claimed in `export-findings.md`. IBKR gives a
genuine per-fill cost; Stockbit does not (day + side only). They are not the same shape, and
the Fill ledger in #6 should let cost attach at fill level where the broker provides it rather
than forcing both to the coarser model.

> Caveat worth keeping: this is one account, one asset class (`STK`), one commission plan.
> The documented "first fill only" behaviour may still be real for other plans or products.
> The importer should not *assume* either layout — summing per-fill commission is correct
> under both, since a zero on fills 2..n sums to the first fill's value anyway. **Summing is
> the safe rule regardless.**

## Open item 1 — timezone: **US Eastern**, settled

No timezone attribute exists anywhere in the file. But the distribution settles it:

- Format is `YYYYMMDD;HHMMSS` — e.g. `20260407;102415`. Semicolon separator, no offset.
- Of 236 fills in the `09:xx` hour, **zero occur before `093000`**, and the earliest is exactly
  `093000`.
- 129 of those 236 fall in `09:30–09:45`.
- Range across all 422 fills is `050553`–`155827`; only 6 sit outside `09:30–16:00`.

A hard floor at exactly 09:30:00 with a heavy opening-15-minutes cluster is the US regular
session in **exchange local time (America/New_York)**. UTC would place the open at 13:30, WIB
at 20:30. Neither matches.

So: parse as `America/New_York`, and **derive the offset per date** — the sample spans April to
August, which crosses no DST boundary here, but a year-long backfill will. Do not store a
fixed offset.

## Open item 3 — exec id format: confirmed `<base>.<seq>`

- Sample: `00015e71.69d54d2e.01.01` — four dot-separated parts.
- **422 of 422** match `<base>.<seq>`.
- All 422 are distinct, and **no base appears more than once**.

The TWS `<base>.<seq>` convention holds for Flex `ibExecID`. The dedupe rule from the research
stands: logical execution = everything up to the last `.`, version = the digits after it,
highest version wins.

**But no corrections appear in this sample** (no repeated base), so the *supersede* path is
untested. `origTradeID`, `origOrderID`, `origTradePrice`, `changeInPrice` and
`changeInQuantity` all exist as attributes and are all empty here. Which mechanism IBKR
actually uses for a correction remains unobserved — keep both paths in mind until one is seen.

## Open item 2 — token expiry: only **1 year** offered

The "Should Expire After" dropdown offers a single option: **1 year**. Chosen; expires
**2027-07-14 10:57:05 EDT**.

Better than the research feared — the 6-hour default is escapable in one click. But it is a
hard annual cliff, so put **2027-07-14 in a calendar** now. Expiry surfaces as error `1012`,
which the daily job must escalate to a human, never retry.

## Open item 5 — custom date range: **no**

The period dropdown offers presets only. The research's recommendation stands: a rolling
`Last N Calendar Days` query with overlap plus idempotent dedupe, and a separate
`Last365CalendarDays` query for backfill. 365 days is the deepest reachable window.

## Confirmed in passing

- **`levelOfDetail` is `EXECUTION` on all 422 rows** — a single level, so no double-counting
  risk in this query. The importer should still filter on it: the field exists precisely
  because a query *can* mix levels.
- **Audit-trail fields are present** (`brokerageOrderID`, `orderReference`,
  `volatilityOrderLink`, `orderTime`), confirming the checkbox did what the research said.
- `openDateTime` is **absent** from the Trade element despite being listed in the field
  reference. Do not depend on it.
- Execution venues are mostly `IBKRATS` (205) and `DARK` (137), not lit exchanges — worth
  knowing before any slippage analysis treats `exchange` as a venue quality signal.

## Still open

- **Open item 4** — are trade/exec ids byte-stable across reruns of the same query? Needs a
  second run on another day, then a diff. Save this run's ids to compare.
- **Open item 6** — does Trade Confirmation Flex land earlier in the day than Activity Flex?
  Only matters if same-day capture is ever wanted.
- **The correction path** — unobserved, as above.
