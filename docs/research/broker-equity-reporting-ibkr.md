# Broker Equity Reporting — IBKR (Flex Web Service)

Research for the per-book **Equity Snapshot**: an account equity level readable at arbitrary past
dates back to the July 2026 backdating floor, used as the denominator of
`risk% = (entry_price − stop) × size ÷ equity`.

Constraint carried in from earlier tickets: retrieval must be **unattended** — no browser, no 2FA,
no interactive login. Hence Flex Web Service (saved Activity Flex Query, `SendRequest` →
`GetStatement`), not the Client Portal Web API and not the TWS API.

Every claim below is tagged **[documented]**, **[inferred]**, or **[needs empirical check]**.
The project has been burned before by trusting IBKR prose over real data (the "commission on first
partial execution only" claim, which real fills contradicted at a 60% undercount), so the tagging is
deliberately conservative: prose that has not been seen against real XML is not treated as settled.

---

## 0. Source reachability note

`interactivebrokers.com` was **not fetchable** during this research — every request returned
`ECONNREFUSED 202.169.44.80:443`, which is the known ISP DNS-interception failure mode (an empty
connection failure, not an HTTP error). `ibkrguides.com` resolved normally and is IBKR's own
documentation host, so it carries the load below.

One consequence: the consolidated error-code table at
`interactivebrokers.com/docs/web-api/flex-web-service/error-codes` could not be read directly. The
error-code facts in §6 come from IBKR's compliance-portal mirror on `ibkrguides.com` and are marked
as partial.

---

## 1. Does an Activity Flex Query expose NAV / account equity at all?

**Yes.** [documented]

The Activity Flex Query Reference lists 46 selectable sections
([Activity Flex Query Reference](https://www.ibkrguides.com/reportingreference/reportguide/activity%20flex%20query%20reference.htm)).
The equity-relevant ones are:

| # | Section name (exactly as IBKR spells it) | Relevance |
|---|---|---|
| 5 | Cash Report | Cash only, period totals |
| 8 | Change in NAV | Period start/end NAV + attribution |
| 25 | Mark-to-Market Performance Summary in Base | Per-instrument, not account equity |
| 27 | Month and Year to Date Performance Summary in Base | Performance, not equity level |
| 28 | **Net Asset Value (NAV) Summary in Base** | **The per-date equity series — see §2** |
| 29 | Net Stock Position Summary | Positions, not equity |
| 41 | Statement of Funds | Cash-movement ledger |

### Important naming correction

**There is no Activity Flex Query section literally named "Equity Summary in Base."** [documented]
The reference's 46-section list does not contain that string. The ticket's prime suspect is real,
but it is an **XML element name, not a section name**: the section you tick in the portal is
**"Net Asset Value (NAV) Summary in Base"**, and what it emits in XML is
`<EquitySummaryInBase>`. [inferred — see the mapping evidence in §2]

That mismatch is worth recording, because searching IBKR's docs for "Equity Summary in Base"
returns nothing and could easily lead someone to conclude the data does not exist.

### 1a. Change in NAV — section 8

Fields, per
[Change in NAV — Flex Statement](https://ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm)
[documented]:

`Account ID`, `Account Alias`, `Model`, `From Date`, `To Date`, **`Starting Value`** ("The NAV at the
start of the period"), `Mark-to-Market`, `Realized`, `Change in Unrealized`, `Deposits/Withdrawals`,
`Internal Cash Transfers`, `Asset Transfers`, `Dividends`, `Withholding Tax`, `871(m) Withholding`,
`Change in Dividend Accruals`, `Interest`, `Change in Interest Accruals`, `Advisor Fees`,
`Client Fees`, `Other Fees`, `Fees Receivable`, `Commissions`, `Commissions Receivable`,
`Forex Commissions`, `Transaction Tax`, `Tax Receivables`, `Sales Tax`, `Soft Dollars`,
`Net Forex Trading`, `Forex Translation`, `Linking Adjustments`, `Other`,
**`Ending Value`** ("The NAV at the end of the period"), `TWR`.

**Shape: two NAV numbers per statement period** (`Starting Value`, `Ending Value`) plus an
attribution breakdown of the delta between them. It is keyed by `From Date`/`To Date`, not by a
report date. **This is not a daily series** and on its own cannot answer "what was equity on
2026-09-14?". [documented]

The section also has a Mark-to-Market vs Realized & Unrealized mode toggle. [documented]

### 1b. Cash Report — section 5

Per [Cash Report — Flex Statement](https://www.ibkrguides.com/reportingreference/reportguide/cash%20reportfq.htm)
[documented]: "This section shows how each period's cash balance changes from one statement period
to the next." Fields run `Starting Cash` … `Ending Cash`, `Ending Settled Cash`, with the usual
fee/dividend/interest/tax line items between.

**Period totals, not a daily series**, and **cash ≠ equity** — it excludes securities market value
entirely. Not a candidate for the denominator (see §5).

Currency handling: cash balances are shown "in your base currency in total" with a Base Currency
Summary preceding a per-currency breakdown. [documented]

### 1c. NAV Summary in Base — section 28

Per
[Net Asset Value (NAV) Summary In Base](https://www.ibkrguides.com/reportingreference/reportguide/net%20asset%20value%20(nav)%20summary%20in%20base.htm)
[documented], the field list is:

`Account ID`, `Account Alias`, `Model`, **`Report Date`** ("The date of the statement."), `Cash`,
`SLB Cash Collateral`, `Stock`, `SLB Direct Securities Borrowed`, `SLB Direct Securities Lent`,
`Options`, `Commodities`, `Bonds`, `Notes`, `Interest Accruals`, `Soft Dollars`,
`Dividend Accruals`, **`Total`** ("The total NAV as of the report date.").

This is the section that matters. Note it is keyed by a singular **`Report Date`**, unlike Change in
NAV's From/To pair — the first structural hint that it is a per-date row.

> **Caution, flagged as a documentation conflict.** IBKR describes this section's *statement*
> rendering as a current-vs-prior-period comparison, showing "the current and prior period, and the
> percent change from the prior to the current period," with one row per asset class. That
> description is about the **rendered PDF/HTML statement layout**, where asset classes are rows and
> periods are columns. The **Flex XML** shape is different — see §2. Do not read the
> comparison-table prose as evidence that Flex only emits two dates.

The sibling page
[Net Asset Value (NAV) In Base Currency](https://www.ibkrguides.com/reportingreference/reportguide/netassetvalueinbasecurrency.htm)
confirms the statement-layout reading: its columns are `Total`, `Long`, `Short`,
`Prior Period Total`, `% Change`, with "a separate row for each asset class in which you hold
positions and for interest accruals and dividend accruals." [documented]

---

## 2. Granularity — is there a daily series?

**Yes: `EquitySummaryInBase` is a repeating, per-date element, keyed by `reportDate`.**

Status: **[inferred]**, converging from three independent directions, with the exact XML unverified
against this account. **[needs empirical check]** — see the one-liner below.

### XML shape

```xml
<EquitySummaryInBase>
  <EquitySummaryByReportDateInBase
      accountId="U1234567" acctAlias="" model="" currency="USD"
      reportDate="2026-09-14"
      cash="…" stock="…" options="…" bonds="…" commodities="…"
      interestAccruals="…" dividendAccruals="…"
      total="…" totalLong="…" totalShort="…" />
  <EquitySummaryByReportDateInBase reportDate="2026-09-15" … />
  <!-- one element per report date in the period -->
</EquitySummaryInBase>
```

### Why the daily-series reading is well-founded

1. **The element name encodes it.** `EquitySummaryByReportDateInBase` — "by report date" — inside a
   plural `EquitySummaryInBase` container.
2. **Field-for-field match with section 28.** The documented NAV Summary in Base field list
   (`Account ID`, `Account Alias`, `Model`, `Report Date`, `Cash`, `SLB Cash Collateral`, `Stock`,
   `SLB Direct Securities Borrowed`, `SLB Direct Securities Lent`, `Options`, `Commodities`,
   `Bonds`, `Notes`, `Interest Accruals`, `Soft Dollars`, `Dividend Accruals`, `Total`) maps 1:1
   onto the attributes of `EquitySummaryByReportDateInBase` as modelled in
   [csingley/ibflex `Types.py`](https://github.com/csingley/ibflex) — `accountId`, `acctAlias`,
   `model`, `reportDate`, `cash`, `slbCashCollateral`, `stock`, `slbDirectSecuritiesBorrowed`,
   `slbDirectSecuritiesLent`, `options`, `commodities`, `bonds`, `notes`, `interestAccruals`,
   `softDollars`, `dividendAccruals`, `total`, each also with `…Long`/`…Short` variants. That is a
   complete correspondence, not a partial one, which is what establishes the section→element
   mapping.
3. **The container is modelled as a sequence.** In `ibflex`, `FlexStatement` declares
   `EquitySummaryInBase: tuple["EquitySummaryByReportDateInBase", ...] = ()` — a variable-length
   tuple, i.e. many rows per statement.

**Source-quality caveat.** Point 1 is IBKR's own naming. Point 2 rests on IBKR's documented field
list on one side and a **third-party parser** on the other. `ibflex` is not a primary source; it is
a schema reverse-engineered from real IBKR XML, and I am using it only as corroboration for element
and attribute *spelling*. The 1:1 field correspondence with IBKR's own documented list is what makes
it credible, but it is not IBKR speaking. IBKR publishes **no XSD** for Flex XML, so there is no
primary artefact that states the cardinality outright.

**Which date field keys the row:** `reportDate` (documented as `Report Date`, "The date of the
statement"). [documented for the field; [inferred] that it is one row per date]

> **Empirical check (do this first — the whole design rests on it):**
> Configure a query with only NAV Summary in Base, period `Last 30 Calendar Days`, run it, and
> confirm the XML contains ~20–22 `<EquitySummaryByReportDateInBase>` elements with distinct
> ascending `reportDate` values (business days only), not 1 or 2.
> `curl -s "…/GetStatement?t=$TOKEN&q=$REF&v=3" | grep -c EquitySummaryByReportDateInBase`

> **Empirical check (calendar coverage):** confirm whether weekends/holidays are absent rows or
> carried-forward duplicate rows. This decides whether the journal must
> as-of-join (take the most recent `reportDate ≤ trade date`) or can do an exact date lookup.
> An as-of-join is the safe implementation either way.

---

## 3. Historical range — retrospective or accumulate-forward?

**Retrospective. A query configured today returns equity for dates months back.** [documented]

> "Saved Flex Queries are available for the four previous calendar years and from the start of the
> current calendar year."
> — [Flex Queries](https://www.ibkrguides.com/clientportal/performanceandstatements/flex.htm), and
> identically at [Flex Queries (org portal)](https://www.ibkrguides.com/orgportal/performanceandstatements/flex.htm)

This is the single most important finding for the ticket. The data is **not** accumulate-forward
from when the section was configured — IBKR generates the statement from its own books at request
time, so adding the NAV section today yields history going back four calendar years plus the current
year. **A July 2026 backdating floor is comfortably inside that window** (today is August 2026;
the floor is ~13 months back, against a ~4.5-year allowance).

### But the period presets constrain what you can ask for on the wire

Available Period options when creating an Activity Flex Query, per
[Create an Activity Flex Query](https://www.ibkrguides.com/orgportal/performanceandstatements/activityflex.htm)
[documented]:

`Last Business Day`, `Last Business Week`, `Last Month`, `Last Quarter`, `Last 30 Calendar Days`,
`Last 365 Calendar Days`, **`Last N Calendar Days`**, `Month to Date`, `Quarter to Date`,
`Year to Date`.

This corroborates the ticket's empirical UI observation: **presets only, no custom date range**, and
the period is baked into the *saved query*, not passed on the wire — `SendRequest` takes only
`t`, `q`, `v` (§6), so there is no date parameter to send.

**Consequence for the backfill design.** `Last N Calendar Days` is the escape hatch: it is the only
preset that takes a number. To reach a July 2026 floor from today, save the NAV query with
`Last N Calendar Days` where N ≈ 420 (covers the floor with margin), pull once to backfill the whole
series, then either leave N large and re-pull idempotently, or keep a second small-N query for the
cheap daily incremental. Leaving N large is simpler and the payload is small — one row per business
day, not per trade.

> **Empirical check:** confirm `Last N Calendar Days` accepts N > 365 (e.g. 420) without the portal
> rejecting it or silently clamping to 365. If it clamps, the backfill needs a temporary query with
> a `Year to Date` / `Last Quarter` ladder, or a one-off manual statement download for the
> pre-clamp tail.

> **Empirical check:** confirm the returned series actually starts at `today − N` and not at the
> account-opening date or the query-creation date.

---

## 4. One query or two?

**Recommendation: a second, separate query id, dedicated to equity.** [inferred — reasoning below]

### On the perturbation question

IBKR's [Trades — Flex Statement](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm)
page lists the Trades Level of Detail options as "Symbol Summary, Executions, Orders, Asset Class,
Closed Lots, Wash Sales" [documented], and — checked explicitly for this ticket — **contains no
warning about selecting multiple levels at once, no mention of duplicate or double-counted rows, and
no mention of a `levelOfDetail` XML attribute.** [documented, as an absence]

So the known double-counting hazard is a hazard *within* the Trades section — it comes from ticking
several Trades levels of detail simultaneously (Executions *and* Orders *and* Symbol Summary all
emitting `<Trade>` elements into one `<Trades>` container). It is **not** documented as being
triggered by adding unrelated sections. Adding NAV Summary in Base emits a sibling
`<EquitySummaryInBase>` container and has no documented interaction with `<Trades>`.

**[inferred]** Adding NAV sections should not perturb Trades output.
**[needs empirical check]** — because the docs' silence here is exactly the kind of silence that
already misled this project once on commissions.

> **Empirical check:** run the existing Trades-only query and a Trades+NAV query over the same
> period and diff the `<Trade>` elements — `xmllint --xpath 'count(//Trade)'` must return the same
> count, and the tradeID sets must be identical.

### Why two queries anyway

Even though one query would probably work, separate query ids are the better shape:

- **Different natural periods.** Trades want a short incremental window; equity wants a long
  backfill window (N ≈ 420). Period is a property of the saved query, so one query cannot serve both
  without over-fetching trades badly.
- **Different cadences and different failure blast radius.** An equity fetch failing should not stop
  trade ingestion, and vice versa. Separate query ids mean separate `SendRequest` calls that fail
  independently.
- **The double-counting hazard stays contained.** The Trades query keeps exactly one level of detail
  (`Executions`) and nobody has a reason to touch it again. Section changes for equity happen in a
  file that has no `<Trades>` in it at all.
- **Smaller blast radius on re-configuration.** Editing a saved query in the portal is a manual,
  unversioned act; keeping the trade-ingestion query frozen is worth a second query id.
- **Cheap.** The cost is one extra `SendRequest`/`GetStatement` round trip against a documented
  budget of 10 requests/minute per token (§6). Two queries is nowhere near the limit.

The parser gains a small benefit too: the equity response contains no `<Trades>`, so the equity code
path never has to know about trade elements.

---

## 5. Which field is the right denominator?

**Use `total` on `EquitySummaryByReportDateInBase` — IBKR's `Total`, documented as "The total NAV as
of the report date."** [documented for the field and its definition; [inferred] that it equals net
liquidation value]

### Distinguishing the candidates

| Candidate | Where | Why not |
|---|---|---|
| `cash` | `EquitySummaryByReportDateInBase`, or Cash Report `Ending Cash` | Cash only. For a margin account holding positions this is wildly wrong as a risk base — it can even be negative on margin while the account has substantial equity. |
| `Ending Settled Cash` | Cash Report | Settlement-lagged cash. Worse: it moves for T+1 reasons unrelated to risk capital. |
| `stock` | `EquitySummaryByReportDateInBase` | Securities market value only — excludes cash, excludes other asset classes. Not equity. |
| `Ending Value` | Change in NAV | Correct *concept* (period-end NAV) but only one value per period — no per-date series (§1a). Useful only as a cross-check. |
| **`total`** | **`EquitySummaryByReportDateInBase`** | **Correct: the sum across cash, stock, options, bonds, commodities, notes and accruals as of that date.** |

### Why `total` is the right risk denominator

The risk-percentage formula asks "what fraction of my risk capital does this trade put at stake?"
The honest denominator is the liquidation value of the whole book on that date — cash plus the
market value of everything held, net of accrued liabilities. That is precisely what `Total` is
defined as, and it is the sum of the sibling components (`cash`, `stock`, `options`, `bonds`,
`commodities`, `notes`, `interestAccruals`, `dividendAccruals`, …) that the section breaks out.

**On "net liquidation value" specifically:** that phrase is TWS/Client-Portal vocabulary
(`NetLiquidation` is an account-summary tag in the TWS API). **There is no field spelled
`netLiquidation` anywhere in the Flex NAV sections** — the Flex spelling is `Total` /
`total`. [documented — it is absent from both IBKR's field list and the full attribute list]
Treating Flex `total` as the equivalent of TWS `NetLiquidation` is **[inferred]**, well-founded on
the definition ("total NAV") but not stated by IBKR in those words.

> **Empirical check:** on one date, confirm
> `total ≈ cash + stock + options + bonds + commodities + notes + interestAccruals + dividendAccruals`
> (within rounding), so the journal knows `total` is a true sum and not a differently-scoped figure.

> **Empirical check:** compare a given date's `total` against the TWS/Client-Portal
> `NetLiquidation` for the same date, or against the `Ending Value` of a Change in NAV section over
> a period ending that date. They should agree. This is the single check that converts the
> net-liq equivalence from inferred to verified.

### Use `total`, not `totalLong`/`totalShort`

Every component carries `…Long` and `…Short` variants. The denominator wants the **net** figure,
`total`. `totalLong` alone would overstate equity for any account holding shorts.

---

## 6. What could silently break an unattended daily fetch

### 6a. Token expiry and the invalidation trap

- **Generating a new token invalidates the existing one.** IBKR is explicit:
  "Note that when you generate a new token, you invalidate the current one."
  — [Configure Flex Web Service](https://www.ibkrguides.com/brokerportal/performanceandstatements/flex3.htm)
  [documented]. Anyone who clicks *Generate* in the portal to "check" the token silently kills the
  running job. Worth a comment in the config.

- **Documentation conflict on lifetime.** IBKR's docs say: "In the Should Expire After list, select
  the amount of time before the token expires. **The token is valid for a 6 hour period by
  default.**" [documented] — but the ticket records that the dropdown empirically offers **exactly
  one option: 1 year**. These disagree. The docs do not enumerate the dropdown's options, so they
  are probably stale relative to the current portal.
  **Trust the observed UI, not the prose** — but treat the 1-year expiry as a hard calendar item:
  an unattended job will fail with error 1012 exactly one year after token creation, and the failure
  looks like a normal error response, not an outage.
  > **Empirical check:** re-open the token dropdown and record the exact options and the token's
  > displayed expiry date; put that date in the journal's config as a comment and set a reminder
  > ~2 weeks before.

### 6b. Error codes

From [Flex Web Service Version 3](https://www.ibkrguides.com/clientportal/flex3.htm) and
[Flex Web Service Version 3 (compliance portal)](https://www.ibkrguides.com/complianceportal/complianceportal/flexweb3.htm)
— IBKR documents **21 error codes, 1001–1021**, "returned in the ErrorCode and ErrorMessage
parameters." Codes confirmed individually [documented]:

| Code | Meaning |
|---|---|
| 1001 | Statement generation in progress / timeout — **retry, do not treat as fatal** |
| 1010 | Legacy queries no longer supported |
| 1012 | "Token has expired" |
| 1015 | "Token is invalid" |
| 1018 | Rate limited — **one request/second, 10 per minute per token** |

**Partial.** The full 1001–1021 table could not be retrieved: the client-portal error page rendered
without its table, the compliance-portal path 404s, and the consolidated
`interactivebrokers.com/docs/web-api/flex-web-service/error-codes` page was unreachable behind the
DNS interception (§0). **[needs empirical check / re-fetch from a clean network]** — pull the full
table before finalising error handling.

**The silent-failure shape that matters most:** errors arrive as **HTTP 200 with an XML error body**,
not as an HTTP error status. A fetcher that only checks the status code will happily hand an error
document to the parser, which will find zero `<EquitySummaryByReportDateInBase>` elements and
conclude the account had no equity — indistinguishable from a genuinely empty period unless you look.
**The fetcher must parse `ErrorCode`/`ErrorMessage` and must treat an unexpectedly empty series as an
error, not as zero equity.** This is the single most likely way this integration breaks silently.

### 6c. The two-step flow and its timing

Endpoints [documented]:

- `https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest?t=TOKEN&q=QUERYID&v=3`
- `https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement?t=TOKEN&q=REFERENCECODE&v=3`

Note the **different hostnames** (`ndcdyn` vs `gdcdyn`) — a copy-paste error here is easy and
produces a confusing failure. `SendRequest` returns a numeric Reference Code plus a response URL;
`GetStatement` exchanges the Reference Code for the data.

IBKR "does not provide specific retry guidance, statement readiness timing, or polling intervals."
[documented, as an absence] Statement generation is asynchronous, so `GetStatement` can legitimately
return 1001 before the statement is ready. **Poll with backoff, respecting the 1 req/sec and
10 req/min limits from 1018** — a tight retry loop will convert a transient 1001 into a hard 1018.

### 6d. Statement cutoff timing

**[needs empirical check]** — not documented anywhere reachable. Neither the Flex Queries pages nor
the reference guide state when a given trading day's row becomes available. IBKR statements are
generally produced overnight after the close, so a job running at, say, 06:00 local may or may not
see yesterday's `reportDate`.

> **Empirical check:** run the NAV query at a few times of day and record the maximum `reportDate`
> returned versus wall-clock; pick a schedule comfortably after the row appears.

Design defence regardless of the answer: **the journal should as-of-join equity (most recent
`reportDate ≤ trade date`) rather than requiring an exact-date row.** That makes a missing latest
row a slightly stale denominator instead of a crash or a divide-by-zero.

### 6e. Base currency in a multi-currency account

The section is "NAV Summary **in Base**" and the element is `EquitySummaryIn**Base**` — all values
are already converted to the account's base currency. [documented] `EquitySummaryByReportDateInBase`
also carries a `currency` attribute. [inferred, from the attribute list]

Two hazards:

1. **The denominator's currency must match the trade's.** If a trade's `entry_price × size` is in USD
   but base currency is EUR, `risk%` is silently wrong by the FX rate. The journal must either store
   equity's currency alongside the value and convert, or assert base == instrument currency and fail
   loudly otherwise.
   > **Empirical check:** read the `currency` attribute on a row and confirm it equals the account's
   > configured base currency.

2. **FX moves the denominator on their own.** For a multi-currency book, base-currency NAV changes
   when FX moves even with no trading. That is arguably correct for risk sizing, but it means `risk%`
   for a historical trade is not reproducible from position data alone — record the equity value used
   at write time rather than recomputing it later.

### 6f. Query mutation and retention

- Saved queries are edited manually in the portal, unversioned. A well-meaning edit (changing the
  period, ticking an extra Trades level of detail) changes the feed with no signal to the journal.
  Recording the query id and a checksum of the expected section set is cheap insurance.
- The four-previous-calendar-years retention (§3) is a rolling window. Not a concern at a July 2026
  floor, but it means very old backdated entries would eventually become unbackfillable — so
  **persist equity snapshots locally once fetched** rather than re-deriving from IBKR on demand.

---

## 7. Verdict

### **Derivable — with caveats.**

Equity at arbitrary past dates back to July 2026 is obtainable unattended from the Flex Web Service.
The two facts that decide it are both documented:

1. The **Net Asset Value (NAV) Summary in Base** section emits a per-report-date equity series with a
   `Total` field defined as "the total NAV as of the report date."
2. Flex data is **retrospective across four previous calendar years plus the current year**, so a
   query created today reaches back well past the July 2026 floor.

No hand entry is required for the IBKR book.

**The caveats that keep this out of "derivable" unqualified:**

- The **daily-series cardinality is inferred, not documented** (§2). IBKR publishes no XSD, and its
  prose for this section describes the *statement layout* (current vs prior period, asset classes as
  rows), which reads as contradicting a per-date series until you notice it is describing the PDF and
  not the XML. Everything downstream rests on this, and it is unverified against this account. **This
  is the one check to run before writing any code.**
- **`total` = net liquidation value is inferred** (§5). The definition supports it; IBKR never says
  those words in the Flex docs, and the TWS-vocabulary field `netLiquidation` does not exist here.
- **Errors return HTTP 200** with an XML error body (§6b), so a naive fetcher can silently record
  "no equity" instead of failing. The empty-series case must be an error.
- **Statement cutoff timing is undocumented** (§6d).
- **Token lifetime documentation conflicts with the observed UI** (§6a), and regenerating a token
  silently kills the running job.
- The **full 1001–1021 error table was not retrievable** on this network (§0, §6b).

### Portal configuration work the owner must do

1. **Create a second Activity Flex Query** (do not modify the existing Trades query — see §4).
2. In it, tick **only** the section **"Net Asset Value (NAV) Summary in Base."** Do not add a Trades
   section to this query.
3. In that section's field pop-up, select at minimum: **Account ID**, **Report Date**, **Total**.
   Recommended additions for cross-checking and currency safety: **Cash**, **Stock**, plus the
   currency field if offered.
4. Set **Period = `Last N Calendar Days`** with **N ≈ 420** (reaches past the July 2026 floor with
   margin; verify N > 365 is accepted — §3).
5. Set **Format = XML**.
6. Save it and **record the new Query ID** — this is the `q` parameter for `SendRequest`.
7. **Reuse the existing Flex token**; do **not** click Generate, which would invalidate the token the
   trades job is using (§6a). Record the token's expiry date and set a reminder ahead of it.

### Then, before implementing

Run the §2 cardinality check and the §5 net-liq cross-check. Those two convert the verdict from
"derivable with caveats" to "derivable," and they are each a single command.

---

## Claim status summary

| Claim | Status |
|---|---|
| Activity Flex Query has NAV/equity sections (46-section list) | documented |
| No section named "Equity Summary in Base"; it is an XML element name | documented (absence) |
| Change in NAV = period start/end only, no daily series | documented |
| Cash Report = period totals, cash only | documented |
| NAV Summary in Base field list incl. `Report Date` and `Total` | documented |
| `Total` = "The total NAV as of the report date" | documented |
| Section 28 emits `<EquitySummaryInBase>` | inferred (1:1 field mapping) |
| One `<EquitySummaryByReportDateInBase>` per date, keyed by `reportDate` | **inferred — needs empirical check** |
| Business-day vs calendar-day row coverage | **needs empirical check** |
| Four previous calendar years + current year of history | documented |
| Period presets only; no custom range; no date param on the wire | documented |
| `Last N Calendar Days` accepts N > 365 | **needs empirical check** |
| Trades LOD options; no documented multi-LOD warning | documented (absence) |
| Adding NAV sections does not perturb Trades output | **inferred — needs empirical check** |
| `total` ≡ net liquidation value | **inferred — needs empirical check** |
| Endpoint URLs, `t`/`q`/`v` params, two-step flow | documented |
| Error codes 1001, 1010, 1012, 1015, 1018 | documented |
| Full 1001–1021 table | **not retrieved — re-fetch on a clean network** |
| New token invalidates the current one | documented |
| Token lifetime (6h docs vs 1yr UI) | **conflict — trust the UI** |
| Statement cutoff timing | **not documented — needs empirical check** |
| Values pre-converted to base currency | documented |

## Sources

- [Activity Flex Query Reference](https://www.ibkrguides.com/reportingreference/reportguide/activity%20flex%20query%20reference.htm)
- [Change in NAV — Flex Statement](https://ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm)
- [Net Asset Value (NAV) Summary In Base](https://www.ibkrguides.com/reportingreference/reportguide/net%20asset%20value%20(nav)%20summary%20in%20base.htm)
- [Net Asset Value (NAV) In Base Currency](https://www.ibkrguides.com/reportingreference/reportguide/netassetvalueinbasecurrency.htm)
- [Cash Report — Flex Statement](https://www.ibkrguides.com/reportingreference/reportguide/cash%20reportfq.htm)
- [Trades — Flex Statement](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm)
- [Create an Activity Flex Query](https://www.ibkrguides.com/orgportal/performanceandstatements/activityflex.htm)
- [Flex Queries (client portal)](https://www.ibkrguides.com/clientportal/performanceandstatements/flex.htm)
- [Flex Queries (org portal)](https://www.ibkrguides.com/orgportal/performanceandstatements/flex.htm)
- [Flex Web Service Version 3](https://www.ibkrguides.com/clientportal/flex3.htm)
- [Flex Web Service Version 3 (compliance portal)](https://www.ibkrguides.com/complianceportal/complianceportal/flexweb3.htm)
- [Configure Flex Web Service](https://www.ibkrguides.com/brokerportal/performanceandstatements/flex3.htm)
- [csingley/ibflex](https://github.com/csingley/ibflex) — **third-party**, used only to corroborate XML element/attribute spelling
